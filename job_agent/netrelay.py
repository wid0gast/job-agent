"""Local proxy for the hosted browser.

The class platform's outbound proxy needs a login (which Chromium can't send to it)
and hangs up on Chromium's TLS handshake, while Python's handshake gets through.
So the browser talks to this relay, the relay re-opens each HTTPS connection from
Python (verifying the real site's certificate), and passes the plain bytes between
them. The browser↔relay hop uses a throwaway loopback certificate, which is why the
browser is launched with certificate errors ignored.
"""

import base64
import os
import socket
import ssl
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit



def loopback_certificate() -> tuple[str, str]:
    """A throwaway self-signed certificate for the browser-to-relay hop, made fresh per process."""
    import datetime
    import tempfile

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "job-agent-relay")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=7)).sign(key, hashes.SHA256()))
    folder = Path(tempfile.mkdtemp(prefix="relay-"))
    cert_path, key_path = folder / "cert", folder / "key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    key_path.chmod(0o600)
    return str(cert_path), str(key_path)


def upstream_proxy() -> dict | None:
    url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not url:
        return None
    u = urlsplit(url)
    login = base64.b64encode(f"{unquote(u.username or '')}:{unquote(u.password or '')}".encode()).decode()
    return {"host": u.hostname, "port": u.port or 80, "auth": f"Basic {login}" if u.username else ""}


def read_head(sock) -> bytes:
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            break
        head += chunk
    return head


def copy(src, dst) -> None:
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.close()
            except OSError:
                pass


class Relay(threading.Thread):
    """HTTP proxy on 127.0.0.1 that forwards through the platform proxy, if there is one."""

    def __init__(self):
        super().__init__(daemon=True)
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.up = upstream_proxy()
        self.errors: list[str] = []
        self.tunnels = 0
        self.serve_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.serve_tls.load_cert_chain(*loopback_certificate())
        self.serve_tls.set_alpn_protocols(["http/1.1"])
        self.site_tls = ssl.create_default_context()
        self.site_tls.set_alpn_protocols(["http/1.1"])
        # Classic key exchange only: keeps the handshake small (~500 bytes, vs ~1.5 KB with post-quantum keys).
        self.site_tls.set_ecdh_curve("X25519")
        self.fallback_tls = ssl.create_default_context()  # For the rare site that refuses X25519-only.
        self.fallback_tls.set_alpn_protocols(["http/1.1"])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def run(self) -> None:
        while True:
            client, _ = self.server.accept()
            threading.Thread(target=self.handle, args=(client,), daemon=True).start()

    def open_tunnel(self, target: str):
        host, _, port = target.rpartition(":")
        if not self.up:
            return socket.create_connection((host, int(port)), timeout=30)
        sock = socket.create_connection((self.up["host"], self.up["port"]), timeout=30)
        auth = f"Proxy-Authorization: {self.up['auth']}\r\n" if self.up["auth"] else ""
        sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n{auth}\r\n".encode())
        reply = read_head(sock)
        if b" 200 " not in reply.split(b"\r\n", 1)[0]:
            sock.close()
            raise OSError(f"proxy refused {target}: {reply.split(b'\r\n', 1)[0].decode(errors='replace')[:80]}")
        return sock

    def handle(self, client) -> None:
        try:
            head = read_head(client)
            method, target = head.split(b" ", 2)[:2]
            if method == b"CONNECT":
                target = target.decode()
                host = target.rpartition(":")[0]
                try:
                    site = self.site_tls.wrap_socket(self.open_tunnel(target), server_hostname=host)
                except ssl.SSLError:
                    site = self.fallback_tls.wrap_socket(self.open_tunnel(target), server_hostname=host)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                browser = self.serve_tls.wrap_socket(client, server_side=True)
                site.settimeout(None)
                self.tunnels += 1
            else:  # Plain http://: hand the request to the platform proxy with the login added.
                if self.up:
                    site = socket.create_connection((self.up["host"], self.up["port"]), timeout=30)
                    if self.up["auth"]:
                        head = head.replace(b"\r\n", f"\r\nProxy-Authorization: {self.up['auth']}\r\n".encode(), 1)
                else:
                    u = urlsplit(target.decode())
                    site = socket.create_connection((u.hostname, u.port or 80), timeout=30)
                    head = head.replace(target, (u.path or "/").encode() + (b"?" + u.query.encode() if u.query else b""), 1)
                site.settimeout(None)
                site.sendall(head)
                browser = client
            threading.Thread(target=copy, args=(browser, site), daemon=True).start()
            copy(site, browser)
        except Exception as error:  # noqa: BLE001
            if len(self.errors) < 50:
                self.errors.append(f"{type(error).__name__}: {error}"[:200])
            try:
                client.close()
            except OSError:
                pass


_relay: Relay | None = None


def browser_proxy() -> dict | None:
    """Playwright `proxy=` setting for the hosted browser; None when no platform proxy is set."""
    global _relay
    if not upstream_proxy():
        return None
    if _relay is None:
        _relay = Relay()
        _relay.start()
    return {"server": _relay.url}


# ----- LaTeX packages --------------------------------------------------------
# Tectonic downloads packages over its own TLS, which the platform proxy drops
# like Chromium's. So Tectonic reads from this local plain-HTTP mirror, and the
# mirror fetches from the real bundle server in Python (ranges included).

BUNDLE_SERVER = "https://relay.fullyjustified.net"
BUNDLE_NAME = "default_bundle_v33.tar"  # Tectonic 0.17's bundle.


class BundleMirror(threading.Thread):
    def __init__(self):
        import httpx
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        super().__init__(daemon=True)
        # Python's default TLS: proven through the platform proxy for this server (and it
        # refuses the X25519-only handshake Relay prefers).
        client = httpx.Client(timeout=httpx.Timeout(120, connect=20))
        resolved: dict[str, str] = {}
        self.errors: list[str] = []
        errors = self.errors

        def upstream(path: str) -> str:
            """The bundle server redirects each name to its storage; resolve once per name."""
            if path not in resolved:
                head = client.head(BUNDLE_SERVER + path, follow_redirects=True)
                resolved[path] = str(head.url)
            return resolved[path]

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def relay(self, method: str):
                try:
                    headers = {"Range": self.headers["Range"]} if self.headers.get("Range") else {}
                    with client.stream(method, upstream(self.path), headers=headers) as response:
                        self.send_response(response.status_code)
                        for name in ("Content-Length", "Content-Range", "Accept-Ranges", "Content-Type",
                                     "ETag", "Last-Modified"):
                            if name in response.headers:
                                self.send_header(name, response.headers[name])
                        self.end_headers()
                        if method == "GET":
                            for chunk in response.iter_bytes(65536):
                                self.wfile.write(chunk)
                except Exception as error:  # noqa: BLE001
                    if len(errors) < 50:
                        errors.append(f"{self.path}: {type(error).__name__}: {error}"[:200])
                    try:
                        self.send_error(502)
                    except OSError:
                        pass

            def do_GET(self):
                self.relay("GET")

            def do_HEAD(self):
                self.relay("HEAD")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/{BUNDLE_NAME}"

    def run(self) -> None:
        self.server.serve_forever()


_mirror: BundleMirror | None = None


def bundle_url() -> str:
    global _mirror
    if _mirror is None:
        _mirror = BundleMirror()
        _mirror.start()
    return _mirror.url
