"""Local proxy for everything the hosted app fetches from the web.

The class platform's outbound proxy (services/worker/src/proxy/server.ts) needs a
Basic login, but its 407 reply carries no Proxy-Authenticate challenge, so Chromium
never sends the credentials. The relay opens each tunnel on the platform proxy with
the login added. The TLS inside the tunnel is the client's own, end to end with the
site: the relay never decrypts it.

The proxy admits four connections per visit, model calls included. start() points
HTTP(S)_PROXY at the relay, so the browser, the job feeds and the LaTeX mirror all
share its MAX_TUNNELS slots; it closes idle connections when a new request needs a
slot, and waits and retries when the proxy still answers 429.
"""

import base64
import os
import socket
import threading
import time
from urllib.parse import unquote, urlsplit

MAX_TUNNELS = 3  # Of the proxy's four per visit; one is left for model calls.
WAIT_SECONDS = 60
RETRIES = 8  # On 429 from the proxy, waiting 0.5, 1, 2, 4, 4... seconds.
# The proxy ends a tunnel 90 s after it opens or once it has carried 16 MiB, busy or not.
# Browser keep-alive tunnels are retired between requests well before either, so a
# request never starts on one that is about to be cut.
RETIRE_SECONDS, RETIRE_BYTES = 60, 12 * 2**20


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


class Tunnel:
    def __init__(self, client):
        self.client, self.site, self.last = client, None, time.monotonic()
        self.started, self.received = self.last, 0
        self.closing = False
        self.reclaimable = False  # Browser keep-alive connections; the app's requests wait for their reply.

    def copy(self, src, dst) -> None:
        try:
            while data := src.recv(65536):
                self.last = time.monotonic()
                if src is self.site:
                    self.received += len(data)
                dst.sendall(data)
        except OSError:
            pass
        finally:
            self.close()

    def close(self) -> None:
        for s in (self.client, self.site):
            if s is None:
                continue
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass


class Relay(threading.Thread):
    """HTTP proxy on 127.0.0.1 that forwards through the platform proxy, if there is one."""

    def __init__(self):
        super().__init__(daemon=True)
        # One port for the browser, one for the app's own requests; both share the slots.
        self.browser_server = socket.create_server(("127.0.0.1", 0))
        self.app_server = socket.create_server(("127.0.0.1", 0))
        self.up = upstream_proxy()
        self.errors: list[str] = []
        self.open: list[Tunnel] = []
        self.slots = threading.Condition()

    @property
    def browser_url(self) -> str:
        return f"http://127.0.0.1:{self.browser_server.getsockname()[1]}"

    @property
    def app_url(self) -> str:
        return f"http://127.0.0.1:{self.app_server.getsockname()[1]}"

    def note(self, error: str) -> None:
        if len(self.errors) < 50:
            self.errors.append(error[:200])
            try:
                with open(error_log(), "a", encoding="utf-8") as log:
                    log.write(error[:200].replace("\n", " ") + "\n")
            except OSError:
                pass

    def run(self) -> None:
        threading.Thread(target=self.serve, args=(self.app_server, False), daemon=True).start()
        threading.Thread(target=self.retire, daemon=True).start()
        self.serve(self.browser_server, True)

    def retire(self) -> None:
        while True:
            time.sleep(1)
            now = time.monotonic()
            with self.slots:
                for t in self.open:
                    if (t.reclaimable and t.site is not None and not t.closing and now - t.last > 1
                            and (now - t.started > RETIRE_SECONDS or t.received > RETIRE_BYTES)):
                        t.closing = True
                        t.close()

    def serve(self, server, reclaimable: bool) -> None:
        while True:
            client, _ = server.accept()
            threading.Thread(target=self.handle, args=(client, reclaimable), daemon=True).start()

    def acquire(self, client, reclaimable: bool) -> Tunnel | None:
        """Claim a tunnel slot; an idle keep-alive connection gives way to a waiting request."""
        deadline = time.monotonic() + WAIT_SECONDS
        with self.slots:
            while len(self.open) >= MAX_TUNNELS:
                now = time.monotonic()
                idle = [t for t in self.open if t.reclaimable and t.site is not None and not t.closing
                        and now - t.last > 1]
                if idle:
                    oldest = min(idle, key=lambda t: t.last)
                    oldest.closing = True
                    oldest.close()  # Its handler frees the slot; the browser reconnects if it needs to.
                if now > deadline:
                    return None
                self.slots.wait(0.25)
            tunnel = Tunnel(client)
            tunnel.reclaimable = reclaimable
            self.open.append(tunnel)
            return tunnel

    def release(self, tunnel: Tunnel) -> None:
        with self.slots:
            if tunnel in self.open:
                self.open.remove(tunnel)
            self.slots.notify()

    def connect_upstream(self, head: bytes):
        """Open this request on the platform proxy (or straight out, on a laptop) with the login added.

        Returns the upstream socket and the bytes the client gets first."""
        method, target = head.split(b" ", 2)[:2]
        if not self.up:
            if method == b"CONNECT":
                host, _, port = target.decode().rpartition(":")
                return socket.create_connection((host, int(port)), timeout=30), ESTABLISHED
            u = urlsplit(target.decode())
            site = socket.create_connection((u.hostname, u.port or 80), timeout=30)
            site.sendall(head.replace(target, (u.path or "/").encode() + (b"?" + u.query.encode() if u.query else b""), 1))
            return site, b""
        if method == b"CONNECT":
            # One exact authority, Host equal to it and no early payload, as the platform requires.
            auth = f"Proxy-Authorization: {self.up['auth']}\r\n" if self.up["auth"] else ""
            head = f"CONNECT {target.decode()} HTTP/1.1\r\nHost: {target.decode()}\r\n{auth}\r\n".encode()
        elif self.up["auth"]:
            head = head.replace(b"\r\n", f"\r\nProxy-Authorization: {self.up['auth']}\r\n".encode(), 1)
        for attempt in range(RETRIES):
            site = socket.create_connection((self.up["host"], self.up["port"]), timeout=30)
            site.sendall(head)
            if method == b"CONNECT":
                reply = read_head(site)
                status = reply.split(b"\r\n", 1)[0]
                if b" 200 " in status + b" ":
                    return site, ESTABLISHED
            else:
                # The proxy refuses before reading a body, so a 429 shows up at once; anything
                # else (or nothing yet) stays unread for the client.
                site.settimeout(1)
                try:
                    status = site.recv(16, socket.MSG_PEEK)
                except TimeoutError:
                    status = b""
                if b" 429" not in status:
                    return site, b""
                reply = read_head(site)
            site.close()
            if b" 429" not in status or attempt == RETRIES - 1:
                raise ConnectionRefusedError(reply.decode(errors="replace")[:300])
            time.sleep(min(0.5 * 2**attempt, 4))  # The platform marks this retryable.
        raise AssertionError("unreachable")

    def handle(self, client, reclaimable: bool) -> None:
        tunnel = None
        try:
            head = read_head(client)
            if not head:
                client.close()
                return
            method, target = head.split(b" ", 2)[:2]
            if method == b"CONNECT" and not target.endswith((b":443", b":80")):
                # The platform proxy only tunnels to ports 80 and 443 (Chromium's push service uses 5228).
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                client.close()
                return
            tunnel = self.acquire(client, reclaimable)
            if tunnel is None:
                self.note(f"no free connection for {target.decode(errors='replace')}")
                client.sendall(b"HTTP/1.1 503 Busy\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                client.close()
                return
            try:
                site, first = self.connect_upstream(head)
            except ConnectionRefusedError as refused:
                reply = str(refused).split("\r\n", 1)[0] or "HTTP/1.1 502 Bad Gateway"
                self.note(f"proxy refused {target.decode(errors='replace')}: {reply}")
                client.sendall(f"{reply}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
                client.close()
                return
            tunnel.site, tunnel.last = site, time.monotonic()
            site.settimeout(None)
            if first:
                client.sendall(first)
            threading.Thread(target=tunnel.copy, args=(client, site), daemon=True).start()
            tunnel.copy(site, client)
        except Exception as error:  # noqa: BLE001
            self.note(f"{type(error).__name__}: {error}")
            try:
                client.close()
            except OSError:
                pass
        finally:
            if tunnel:
                time.sleep(0.2)  # Let the platform proxy count this connection as closed first.
                self.release(tunnel)


ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"
_relay: Relay | None = None


def error_log() -> str:
    return os.path.join(os.environ.get("JOB_AGENT_HOME") or "/tmp", "relay-errors.log")


def recent_errors(count: int = 3) -> list[str]:
    try:
        with open(error_log(), encoding="utf-8") as log:
            return log.read().splitlines()[-count:]
    except OSError:
        return []


def start() -> None:
    """Hosted: run the relay in this long-lived process and send this process's and its
    children's web requests through it. The platform proxy's login stays here."""
    global _relay
    if _relay is not None or not upstream_proxy():
        return
    _relay = Relay()
    _relay.start()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ[name] = _relay.app_url
    os.environ["JOB_AGENT_RELAY"] = _relay.browser_url


def browser_proxy() -> dict | None:
    """Playwright `proxy=` setting for the hosted browser; None when there is no relay (a laptop)."""
    url = os.environ.get("JOB_AGENT_RELAY")
    return {"server": url} if url else None


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
        # One connection per request: the class proxy cuts a reused tunnel after 16 MiB or 90 s.
        client = httpx.Client(timeout=httpx.Timeout(120, connect=20), limits=httpx.Limits(max_keepalive_connections=0))
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
