"""A tiny web app for end-to-end tests: logs in, lists users, opens one user's orders.

`version` changes what the API answers, to produce drift: version 2 drops
`email` from users and turns order totals into strings.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = b"""<!doctype html><html><head><title>Demo shop</title></head><body>
<h1>Demo shop</h1><img src="/logo.png">
<script>
(async () => {
  navigator.sendBeacon('/collect', 'event=view');
  const login = await fetch('/api/login', {method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({email: 'me@example.com', password: 'hunter2'})}).then(r => r.json());
  const auth = {Authorization: 'Bearer ' + login.access_token};
  const users = await fetch('/api/v1/users?limit=20', {headers: auth}).then(r => r.json());
  const id = users.users[0].id;
  await fetch('/api/v1/users/' + id + '/orders', {headers: auth}).then(r => r.json());
  await fetch('/api/v1/cart', {method: 'PUT', headers: {...auth, 'content-type': 'application/json'},
    body: JSON.stringify({user_id: id, items: [{sku: 'SKU-100234', qty: 1}]})});
  document.title = 'done';
})();
</script></body></html>"""

TOKEN = "tok_9f8e7d6c5b4a39281706"


class DemoSite:
    def __init__(self) -> None:
        self.version = 1
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/":
                    return self._send(200, PAGE, "text/html")
                if path == "/logo.png":
                    return self._send(200, b"\x89PNG\r\n\x1a\n", "image/png")
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._send(401, {"error": "unauthorized"})
                if path == "/api/v1/users":
                    users = [
                        {"id": 48213, "name": "Ada", "email": "ada@example.com"},
                        {"id": 48214, "name": "Linus", "email": "l@example.com"},
                    ]
                    if site.version == 2:
                        users = [{"id": u["id"], "name": u["name"]} for u in users]
                    return self._send(200, {"users": users, "next": None})
                if path.startswith("/api/v1/users/") and path.endswith("/orders"):
                    total = 12.5 if site.version == 1 else "12.50"
                    return self._send(
                        200, {"orders": [{"order_id": "ord_7d1c2b3a4f5e6d7c8b9a", "total": total}]}
                    )
                return self._send(404, {"error": "not found"})

            def do_PUT(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                return self._send(200, {"ok": True, "cart_id": "cart_5b6c7d8e9f0a1b2c3d"})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/collect":
                    return self._send(204, b"", "text/plain")
                if self.path == "/api/login":
                    return self._send(200, {"access_token": TOKEN, "user": {"id": 48213}})
                return self._send(404, {"error": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
