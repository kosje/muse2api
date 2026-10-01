"""Persistent credentials and bounded, DNS-pinned remote image downloads."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import tempfile
import threading
import time
from urllib.parse import urlsplit


class Keyring:
    """The persisted file is authoritative, including after container recreation.

    Environment credentials bootstrap a new installation only. Never silently
    replace a corrupt existing credential file or allow empty-key authentication.
    Run one application worker; rotation is atomic within that process.
    """

    def __init__(self, path, api_key="", admin_key=""):
        self.path = Path(path)
        self.lock = threading.RLock()
        if self.path.exists():
            self.keys = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.keys = {"api": api_key or self.new_key(),
                         "admin": admin_key or self.new_key()}
            self._validate(self.keys)
            self._persist(self.keys)
        self._validate(self.keys)

    @staticmethod
    def new_key():
        return "m2a_" + secrets.token_hex(32)

    @staticmethod
    def _validate(keys):
        if (set(keys) != {"api", "admin"}
                or any(not isinstance(k, str) or len(k) < 32 for k in keys.values())
                or keys["api"] == keys["admin"]):
            raise ValueError("Credentials must be distinct strings of at least 32 characters")

    def _persist(self, keys):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(keys, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def role(self, authorization):
        if not isinstance(authorization, str):
            return None
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        with self.lock:
            for role, key in self.keys.items():
                if hmac.compare_digest(parts[1].encode(), key.encode()):
                    return role
        return None

    def rotate(self, role):
        if role not in ("api", "admin"):
            raise ValueError("Unknown credential role")
        with self.lock:
            updated = {**self.keys, role: self.new_key()}
            self._persist(updated)  # Do not invalidate the old key on write failure.
            self.keys = updated
            return updated[role]

    def media_ticket(self, role, lifetime=3600):
        payload = f"{role}.{int(time.time()) + lifetime}"
        with self.lock:
            signature = hmac.new(self.keys[role].encode(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def valid_media_ticket(self, ticket):
        try:
            role, expiry, signature = ticket.split(".")
            if role not in self.keys or int(expiry) <= time.time():
                return False
            payload = f"{role}.{expiry}"
            with self.lock:
                expected = hmac.new(self.keys[role].encode(), payload.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, signature)
        except (ValueError, TypeError, AttributeError):
            return False


MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"}


def public_target(url):
    """Resolve exactly once; reject mixed public/private answers and special IPs."""
    parts = urlsplit(url)
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.fragment or "\\" in url or any(ord(c) < 33 for c in url)):
        raise ValueError("Invalid remote image URL")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port != (443 if parts.scheme == "https" else 80):
        raise ValueError("Remote images require standard HTTP(S) ports")
    host = parts.hostname.encode("idna").decode("ascii")
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = [a[4][0] for a in answers]
    if not addresses:
        raise ValueError("No public address")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        # Disallow IPv6 translation/tunnel ranges as well as mapped private IPv4.
        if (not ip.is_global or ip.is_multicast or ip.is_reserved
                or (ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo
                    or ip in ipaddress.ip_network("64:ff9b::/96")))):
            raise ValueError("Remote images may only use public addresses")
    return parts, host, port, addresses[0]


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address, tls):
        super().__init__(host, port, timeout=5)
        self.address, self.tls = address, tls

    def connect(self):
        # Use only the validated numeric address. Host header, TLS SNI and
        # certificate verification still use the original hostname.
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        if self.tls:
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)


def download_image(url):
    parts, host, port, address = public_target(url)
    conn = _PinnedHTTP(host, port, address, parts.scheme == "https")
    deadline = time.monotonic() + 20
    try:
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        conn.request("GET", target, headers={"Accept": "image/*", "Accept-Encoding": "identity"})
        response = conn.getresponse()
        # Reject every redirect: never follow a redirect to a different trust boundary.
        if response.status != 200:
            raise ValueError("Remote image must return HTTP 200 without redirects")
        mime = response.getheader("Content-Type", "").split(";", 1)[0].lower().strip()
        if mime not in IMAGE_TYPES or response.getheader("Content-Encoding", "identity") != "identity":
            raise ValueError("Unsupported image response")
        size = response.getheader("Content-Length")
        if size is not None and (int(size) < 0 or int(size) > MAX_IMAGE_BYTES):
            raise ValueError("Remote image too large")
        data = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise ValueError("Remote image deadline exceeded")
            # read1 returns after one underlying read; a trickling body cannot
            # keep a single buffered read alive indefinitely.
            chunk = response.read1(min(65536, MAX_IMAGE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("Remote image too large")
        if not data:
            raise ValueError("Empty remote image")
        return bytes(data), mime
    finally:
        conn.close()
