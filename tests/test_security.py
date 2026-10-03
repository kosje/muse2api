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
    monkeypatch.setattr(app, "store", app.Store(app.CFG))
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


def test_import_prefixed_cookie_and_explicit_label(client):
    values={name:'fixture-'+name for name in app.ESSENTIAL_COOKIES}
    raw='export-label | '+ '; '.join(name+'='+value for name,value in values.items())
    response=client.post('/admin/accounts',headers=ADMIN,json={'label':'UI label','cookie_header':raw})
    assert response.status_code==200
    added=response.json()['added'][0]
    account=app.store.get_account(added['id'])
    assert account['label']=='UI label'
    assert account['cookies']==values
    assert not any(value in response.text for value in values.values())
    response=client.post('/admin/accounts',headers=ADMIN,json={'cookie_header':raw})
    assert response.json()['added'][0]['label']=='export-label'


def test_import_missing_cookies_never_writes_partial_accounts(client):
    raw='hatch_sess=do-not-echo-this-secret'
    response=client.post('/admin/accounts',headers=ADMIN,json={'cookie_header':raw})
    assert response.status_code==400
    assert 'hatch_gw' in response.json()['detail']
    assert 'do-not-echo-this-secret' not in response.text
    valid='; '.join(name+'=fixture' for name in app.ESSENTIAL_COOKIES)
    response=client.post('/admin/accounts',headers=ADMIN,json={'batch':'valid | '+valid+'\ninvalid | '+raw})
    assert response.status_code==400
    assert app.store.list_accounts()==[]


def test_import_raw_cookie_and_helper_payload(client):
    values={name:'fixture' for name in app.ESSENTIAL_COOKIES}
    raw='Cookie: '+ '; '.join(name+'='+value for name,value in values.items())
    assert client.post('/admin/accounts',headers=ADMIN,json={'cookie_header':raw}).status_code==200
    assert client.post('/admin/accounts',headers=ADMIN,json={'cookies':values}).status_code==200


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


@pytest.mark.parametrize('extension,mime', [('webp','image/webp'),('png','image/png'),('jpg','image/jpeg'),('mp4','video/mp4')])
def test_media_type_without_system_mime_database(client, extension, mime, monkeypatch):
    import mimetypes
    monkeypatch.setattr(mimetypes,'guess_type',lambda *args,**kwargs:(None,None))
    path=Path(app.CFG.media_dir)/('image-format-fixture.'+extension)
    path.write_bytes(b'fixture bytes')
    response=client.get('/v1/media/'+path.name,headers=API)
    assert response.status_code==200
    assert response.headers['content-type']==mime


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


def test_slow_http_headers_have_total_deadline(monkeypatch):
    import socketserver
    import threading
    import time
    from urllib.parse import urlsplit
    class Slow(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(4096)
            try:
                self.request.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                for _ in range(100):
                    self.request.sendall(b"a")
                    time.sleep(.02)
            except OSError:
                pass
    server=socketserver.ThreadingTCPServer(('127.0.0.1',0),Slow)
    server.daemon_threads=True
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    real_timer=threading.Timer
    monkeypatch.setattr(security.threading,'Timer',lambda seconds,fn:real_timer(.15,fn))
    port=server.server_address[1]
    monkeypatch.setattr(security,'public_target',lambda url:(urlsplit(url),'127.0.0.1',port,'127.0.0.1'))
    started=time.monotonic()
    try:
        with pytest.raises((ValueError,OSError,security.http.client.HTTPException)):
            security.download_image('http://fixture.example/image')
        assert time.monotonic()-started < 1.5
    finally:
        server.shutdown();server.server_close()


def test_cookie_helper_transport():
    spec=importlib.util.spec_from_file_location('cookie_helper',ROOT/'tools/get_muse_cookie.py')
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    helper.validate_transport('http://127.0.0.1:18610')
    helper.validate_transport('https://video.example.com')
    for url in ('http://example.com','http://localhost.evil.example','http://10.0.0.1','https://u:p@example.com'):
        with pytest.raises(RuntimeError): helper.validate_transport(url)
    with pytest.raises(RuntimeError): helper.NoRedirect().redirect_request(None,None,302,None,None,'https://evil.example')


@pytest.mark.parametrize('name,kind', [('delete-fixture.webp','image'),('delete-fixture.mp4','video')])
def test_admin_deletion_removes_file_and_task_links(client,name,kind):
    path=Path(app.CFG.media_dir)/name
    path.write_bytes(b'fixture contents')
    keep=Path(app.CFG.media_dir)/'keep.png';keep.write_bytes(b'keep')
    task=app.store.create_task(kind,'fixture')
    url='/v1/media/'+name
    app.store.update_task(task['id'],status='completed',url=url,
        result={'filename':name,'size':path.stat().st_size,'kind':kind,'url':url},video={'url':url})
    assert client.head(url,headers=API).status_code==200
    before=client.get('/admin/media',headers=ADMIN).json()['total_bytes']
    deleted=client.delete('/admin/media/'+name,headers=ADMIN)
    assert deleted.status_code==200 and deleted.json()['freed_bytes']==16
    assert not path.exists() and keep.read_bytes()==b'keep'
    assert client.get(url,headers=API).status_code==404
    assert client.head(url,headers=API).status_code==404
    assert client.head(url).status_code==401
    listing=client.get('/admin/media',headers=ADMIN).json()
    assert all(m['name']!=name for m in listing['media'])
    assert listing['total_bytes']==before-16
    route='/v1/images/tasks/' if kind=='image' else '/v1/videos/'
    record=client.get(route+task['id'],headers=API).json()
    assert record['status']=='deleted'
    assert not any(record.get(k) for k in ['result','url','video','data'])
    restored=app.Store(app.CFG)
    assert restored.get_task(task['id'])['status']=='deleted'
    repeat=client.delete('/admin/media/'+name,headers=ADMIN)
    assert repeat.status_code==200 and repeat.json()['already_missing'] and repeat.json()['freed_bytes']==0


def test_media_deletion_requires_admin_not_bearer_or_cookie(client):
    path=Path(app.CFG.media_dir)/'fixture.mp4'
    assert client.delete('/admin/media/fixture.mp4').status_code==401
    assert client.delete('/admin/media/fixture.mp4',headers=API).status_code==403
    client.post('/v1/media/session',headers=ADMIN)
    assert client.delete('/admin/media/fixture.mp4').status_code==401
    assert path.exists()


@pytest.mark.parametrize('name',['auth.json','..%5Cauth.json','%2E%2E%2Fauth.json','.env','fixture.mp4%00','file.txt'])
def test_delete_rejects_paths_and_nonmedia(client,name):
    before=(Path(app.CFG.data_dir)/'accounts.json').read_bytes()
    response=client.delete('/admin/media/'+name,headers=ADMIN)
    assert response.status_code in (400,404)
    assert (Path(app.CFG.data_dir)/'accounts.json').read_bytes()==before
    assert (Path(app.CFG.media_dir)/'fixture.mp4').exists()


def test_delete_rejects_symlinks_and_directories(client):
    root=Path(app.CFG.media_dir)
    (root/'folder.png').mkdir()
    assert client.delete('/admin/media/folder.png',headers=ADMIN).status_code==400
    target=Path(app.CFG.data_dir)/'accounts.json'
    try:(root/'link.png').symlink_to(target)
    except OSError:pytest.skip('Symlink creation unavailable')
    assert client.delete('/admin/media/link.png',headers=ADMIN).status_code==400
    assert target.exists() and (root/'link.png').is_symlink()
    assert all(m['name']!='link.png' for m in client.get('/admin/media',headers=ADMIN).json()['media'])


def test_delete_waits_for_generation(client):
    with app.GEN_LOCK:
        assert client.delete('/admin/media/fixture.mp4',headers=ADMIN).status_code==409
    assert (Path(app.CFG.media_dir)/'fixture.mp4').exists()


def test_delete_metadata_failure_preserves_file(client,monkeypatch):
    import store as storage
    task=app.store.create_task('video','fixture')
    app.store.update_task(task['id'],status='completed',result={'filename':'fixture.mp4'})
    def fail(*args,**kwargs):raise OSError('fixture disk failure')
    monkeypatch.setattr(storage,'_write',fail)
    assert client.delete('/admin/media/fixture.mp4',headers=ADMIN).status_code==500
    assert (Path(app.CFG.media_dir)/'fixture.mp4').exists()
    assert app.store.get_task(task['id'])['status']=='completed'


def test_delete_unlink_failure_restores_links(client,monkeypatch):
    import store as storage
    path=Path(app.CFG.media_dir)/'fixture.mp4'
    task=app.store.create_task('video','fixture')
    app.store.update_task(task['id'],status='completed',result={'filename':path.name})
    original_unlink=storage.os.unlink
    def fail_only_media(p,*args,**kwargs):
        if str(p)==str(path):raise PermissionError('fixture in use')
        return original_unlink(p,*args,**kwargs)
    monkeypatch.setattr(storage.os,'unlink',fail_only_media)
    assert client.delete('/admin/media/'+path.name,headers=ADMIN).status_code==500
    assert path.exists()
    assert app.store.get_task(task['id'])['status']=='completed'
    assert app.Store(app.CFG).get_task(task['id'])['status']=='completed'
