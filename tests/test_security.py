import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Isolate import-time application initialization; never read a real account pool.
_home = tempfile.TemporaryDirectory()
os.environ.update(MUSE2API_HOME=_home.name, MUSE2API_PROFILE_ROOT=_home.name,
                  MUSE2API_KEY="a"*40, MUSE2API_ADMIN_KEY="b"*40)
import app
import security
from engine import MuseEngine, MuseGenerationError


@pytest.fixture
def client(tmp_path, monkeypatch):
    ring = security.Keyring(tmp_path / "auth.json", "a"*40, "b"*40)
    monkeypatch.setattr(app, "KEYRING", ring)
    monkeypatch.setattr(app.CFG, "base_dir", str(tmp_path))
    monkeypatch.setattr(app.CFG, "public_base", "")
    (tmp_path / "data" / "media").mkdir(parents=True)
    (tmp_path / "data" / "media" / "fixture.mp4").write_bytes(b"fixture")
    (tmp_path / "data" / "accounts.json").write_text('SECRET COOKIE')
    (tmp_path / "install.conf").write_text('SECRET KEY')
    # Do not start browser/keepalive background tasks.
    return TestClient(app.app)


API = {"Authorization": "Bearer " + "a"*40}
ADMIN = {"Authorization": "Bearer " + "b"*40}


@pytest.mark.parametrize("path", ["/install.conf", "/docker-compose.yml", "/compose.yml", "/data/accounts.json",
                                  "/data/auth.json", "/.git/config", "/.env", "/web/../config.py", "/backups/data.tar.gz"])
def test_secrets_not_served(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert "SECRET" not in response.text


def test_authentication_and_admin_separation(client):
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers=API).status_code == 200
    assert client.get("/admin/accounts", headers=API).status_code == 403
    assert client.get("/admin/accounts", headers=ADMIN).status_code == 200
    assert client.post("/admin/apikey/rotate", headers=API).status_code == 403
    assert client.post("/admin/adminkey/rotate", headers=API).status_code == 403
    assert client.get("/admin/apikey?key="+"b"*40).status_code == 401
    assert client.get("/v1/videos").status_code == 401
    assert client.post("/admin/unknown").status_code == 401


def test_no_code_update_endpoints(client):
    for path in ("/admin/update/upgrade", "/admin/repo/pull", "/admin/repo/push"):
        assert client.post(path, headers=ADMIN, json={}).status_code == 404
    assert client.get("/admin/update/check", headers=ADMIN).status_code == 404


def test_media_auth_and_scoped_cookie(client):
    path = "/v1/media/fixture.mp4"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=API).content == b"fixture"
    r = client.post("/v1/media/session", headers=API)
    assert r.status_code == 200
    assert "HttpOnly" in r.headers["set-cookie"]
    assert "SameSite=strict" in r.headers["set-cookie"]
    assert "Path=/v1/media/" in r.headers["set-cookie"]
    assert client.get(path).content == b"fixture"
    assert client.get("/admin/apikey").status_code == 401
    assert client.get("/v1/models").status_code == 401
    assert client.get(path, headers={"Range": "bytes=0-2"}).content == b"fix"
    client.delete("/v1/media/session")
    assert client.get(path).status_code == 401


def test_https_media_cookie(client, monkeypatch):
    monkeypatch.setattr(app.CFG, "public_base", "https://video.example.com")
    assert "Secure" in client.post("/v1/media/session", headers=API).headers["set-cookie"]


def test_rotation_survives_restart_and_revokes_media(client):
    client.post("/v1/media/session", headers=API)
    old_path = app.KEYRING.path
    new_key = client.post("/admin/apikey/rotate", headers=ADMIN).json()["api_key"]
    assert client.get("/v1/models", headers=API).status_code == 401
    assert client.get("/v1/media/fixture.mp4").status_code == 401
    restarted = security.Keyring(old_path, "a"*40, "b"*40)
    assert restarted.role("Bearer " + new_key) == "api"
    assert restarted.role(API["Authorization"]) is None
    assert restarted.role(ADMIN["Authorization"]) == "admin"
    newer = client.post("/admin/adminkey/rotate", headers=ADMIN).json()["admin_key"]
    assert client.get("/admin/apikey", headers=ADMIN).status_code == 401
    assert client.get("/admin/apikey", headers={"Authorization":"Bearer "+newer}).status_code == 200


def test_keyring_fails_closed(tmp_path, monkeypatch):
    file = tmp_path / "auth.json"
    file.write_text('broken')
    with pytest.raises(ValueError): security.Keyring(file)
    file.unlink()
    with pytest.raises(ValueError): security.Keyring(file, "short", "b"*40)
    ring = security.Keyring(file, "a"*40, "b"*40)
    monkeypatch.setattr(ring, "_persist", lambda _: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError): ring.rotate("api")
    assert ring.role(API["Authorization"]) == "api"
    assert not ring.valid_media_ticket(ring.media_ticket("api", -1))
    assert not ring.valid_media_ticket("admin.9999999999.forged")


def test_cors_and_security_headers(client):
    response = client.options("/v1/models", headers={"Origin":"https://evil.example", "Access-Control-Request-Method":"GET"})
    assert "access-control-allow-origin" not in response.headers
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "SECRET" not in response.text


def answers(*ips):
    return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip,443)) for ip in ips]


@pytest.mark.parametrize("ip", ["127.0.0.1","10.0.0.1","169.254.169.254","192.168.0.1","172.16.0.1","0.0.0.0","100.64.0.1","224.0.0.1","::1","fc00::1","fe80::1","::ffff:127.0.0.1","64:ff9b::7f00:1"])
def test_ssrf_blocks_private_addresses(monkeypatch, ip):
    monkeypatch.setattr(security.socket,"getaddrinfo",lambda *a,**kw:answers(ip))
    with pytest.raises(ValueError): security.public_target("https://image.example/test.png")


@pytest.mark.parametrize("url", ["file:///etc/passwd","ftp://example.com/a","http://example.com:19210/a","https://u:p@example.com/a","https://example.com/a\r\nX:a"])
def test_ssrf_rejects_scheme_port_and_credentials(url):
    with pytest.raises(ValueError): security.public_target(url)


def test_dns_pinning_and_mixed_answers(monkeypatch):
    monkeypatch.setattr(security.socket,"getaddrinfo",lambda *a,**kw:answers("8.8.8.8","10.0.0.1"))
    with pytest.raises(ValueError): security.public_target("https://image.example/a.png")
    monkeypatch.setattr(security.socket,"getaddrinfo",lambda *a,**kw:answers("8.8.8.8"))
    parts,host,port,ip = security.public_target("https://image.example/a.png")
    assert (host, port, ip) == ("image.example",443,"8.8.8.8")
    calls=[]
    monkeypatch.setattr(security.socket,"create_connection",lambda target,timeout: calls.append(target) or object())
    conn=security._PinnedHTTP(host,80,ip,False);conn.connect()
    assert calls==[("8.8.8.8",80)]


@pytest.mark.parametrize("status,mime,length,body", [(302,"image/png","1",b"x"),(200,"text/html","1",b"x"),(200,"image/png",str(security.MAX_IMAGE_BYTES+1),b"x"),(200,"image/png",None,b"x"*65)])
def test_download_rejects_redirect_mime_and_size(monkeypatch,status,mime,length,body):
    monkeypatch.setattr(security,"public_target",lambda _: (__import__('urllib.parse',fromlist=['urlsplit']).urlsplit('https://image.example/a'),"image.example",443,"8.8.8.8"))
    monkeypatch.setattr(security,"MAX_IMAGE_BYTES",64)
    class Response:
        def __init__(self): self.status=status;self.body=body
        def getheader(self,k,default=None): return {"Content-Type":mime,"Content-Length":length}.get(k,default)
        def read1(self,n): result,self.body=self.body[:n],self.body[n:];return result
    class Connection:
        def __init__(self,*a): pass
        def request(self,*a,**kw): pass
        def getresponse(self): return Response()
        def close(self): pass
        def abort(self): pass
    monkeypatch.setattr(security,"_PinnedHTTP",Connection)
    with pytest.raises(ValueError): security.download_image("https://image.example/a")


def test_no_local_file_reference(tmp_path):
    f=tmp_path/'secret.png';f.write_bytes(b'secret')
    with pytest.raises(MuseGenerationError): MuseEngine._normalize_image(str(f))


def test_cookie_helper_transport():
    spec=importlib.util.spec_from_file_location('cookie_helper',ROOT/'tools/get_muse_cookie.py')
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    helper.validate_transport('http://127.0.0.1:18610')
    helper.validate_transport('https://video.example.com')
    for url in ('http://example.com','http://localhost.evil.example','http://10.0.0.1','https://u:p@example.com'):
        with pytest.raises(RuntimeError): helper.validate_transport(url)
    with pytest.raises(RuntimeError): helper.NoRedirect().redirect_request(None,None,302,None,None,'https://evil.example')
