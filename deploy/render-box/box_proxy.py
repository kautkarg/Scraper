"""Single-port reverse proxy for the Render free discovery box.

Render routes all public traffic to one $PORT, but the box runs two
servers (SearXNG on 8080, `crw serve` on 3001). This proxy splits by path:

    /searxng/*  -> SearXNG (prefix stripped), e.g. /searxng/search -> /search
    everything else -> crw serve

stdlib only — the box image has no package manager access at runtime and
this matches the repo's zero-dependency style (see deploy/discovery/llm_proxy.py).
Responses are relayed verbatim with `Connection: close` (one-shot requests;
the only client is the Render app, so keep-alive complexity isn't worth it).
"""

from __future__ import annotations

import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CRW = ("127.0.0.1", int(os.environ.get("CRW_PORT", "3001")))
SEARX = ("127.0.0.1", int(os.environ.get("GRANIAN_PORT", "8080")))
PREFIX = "/searxng"
_CONNECT_TIMEOUT = 30
_IO_TIMEOUT = 600  # crw crawls/scrapes can be slow; the app caps waits anyway
_BUF = 65536
# Headers that must not be forwarded verbatim (hop-by-hop / framing).
_SKIP = {
    "host", "connection", "content-length", "transfer-encoding",
    "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "upgrade",
}


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: N802
        sys.stderr.write("[box-proxy] %s\n" % (fmt % args))

    def _forward(self) -> None:
        if self.path == PREFIX or self.path.startswith(PREFIX + "/"):
            upstream, path = SEARX, (self.path[len(PREFIX):] or "/")
        else:
            upstream, path = CRW, self.path
        try:
            conn = socket.create_connection(upstream, timeout=_CONNECT_TIMEOUT)
        except OSError as exc:
            self.send_error(502, f"upstream {upstream[0]}:{upstream[1]} down: {exc}")
            return
        conn.settimeout(_IO_TIMEOUT)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            head = [
                f"{self.command} {path} HTTP/1.1",
                f"Host: {upstream[0]}:{upstream[1]}",
                "Connection: close",
            ]
            for key, value in self.headers.items():
                if key.lower() not in _SKIP:
                    head.append(f"{key}: {value}")
            if body:
                head.append(f"Content-Length: {len(body)}")
            conn.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + body)
            self.close_connection = True
            while True:  # relay the response verbatim (status + headers + body)
                chunk = conn.recv(_BUF)
                if not chunk:
                    break
                self.connection.sendall(chunk)
        except OSError as exc:
            sys.stderr.write(f"[box-proxy] relay error: {exc}\n")
            self.close_connection = True
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = _forward


def main() -> None:
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), ProxyHandler)
    sys.stderr.write(f"[box-proxy] :{port} -> crw {CRW[0]}:{CRW[1]}, "
                     f"{PREFIX}/* -> searxng {SEARX[0]}:{SEARX[1]}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
