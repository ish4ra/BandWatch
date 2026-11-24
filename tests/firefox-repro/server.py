"""Minimal standalone HTTP server for the Firefox stall reproduction (issue #2).

Standard library only, no BandWatch code: a page that keeps ordinary
``fetch`` requests in flight, optionally holds a Server-Sent Events stream
and optionally registers a network-first service worker (the same pattern
as the BandWatch dashboard). ``--close`` answers every request with
``Connection: close`` instead of HTTP/1.1 keep-alive.

    python3 tests/firefox-repro/server.py --port 18950 [--close]
"""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><title>repro</title>
<script src="/app.js"></script></head><body><p id="state">loading</p></body></html>"""

APP = b"""
const q = new URLSearchParams(location.search);
let n = 0;
async function loop() {
  for (;;) {
    try { await (await fetch('/api/poll?n=' + (n++))).json(); } catch (e) { return; }
    document.body && document.body.setAttribute('data-ready', '1');
  }
}
if (q.get('sw') === '1' && navigator.serviceWorker) navigator.serviceWorker.register('/sw.js').catch(() => {});
if (q.get('sse') === '1' && window.EventSource) { const es = new EventSource('/api/stream'); es.onmessage = () => {}; }
window.addEventListener('DOMContentLoaded', () => { loop(); loop(); });
"""

SW = b"""
const CACHE = 'repro-v1';
self.addEventListener('install', (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(['/', '/app.js'])).then(() => self.skipWaiting())); });
self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;
  e.respondWith(fetch(e.request).then((res) => {
    if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
    return res;
  }).catch(() => caches.match(e.request)));
});
"""


def make_handler(close):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 12

        def log_message(self, *a):
            pass

        def _send(self, body, ctype, cache="no-store"):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            try:
                if path == "/":
                    return self._send(PAGE, "text/html; charset=utf-8", "no-cache")
                if path == "/app.js":
                    return self._send(APP, "text/javascript", "no-cache")
                if path == "/sw.js":
                    return self._send(SW, "text/javascript", "no-cache")
                if path == "/api/poll":
                    time.sleep(0.25)  # keep requests in flight when the page goes away
                    return self._send(json.dumps({"t": time.time()}).encode(), "application/json")
                if path == "/api/stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    for _ in range(600):
                        self.wfile.write(b"data: {}\n\n")
                        self.wfile.flush()
                        time.sleep(1.0)
                    return None
                self.send_error(404)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return None

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--close", action="store_true")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.close))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("ready", a.port, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
