"""Local preview of site/public with coordinator paths proxied to a running coordinator.

Usage: python3 -I site/tools/preview.py site/public 8790 8765   (add --offline to simulate a sleeping coordinator)
"""
import http.server, sys, urllib.request, functools
ROOT, PORT, UPSTREAM, OFFLINE = sys.argv[1], int(sys.argv[2]), sys.argv[3], "--offline" in sys.argv
class H(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith(("/api/", "/mcp", "/health")):
            if OFFLINE:
                self.send_response(503); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(b'{"error":"offline"}'); return
            with urllib.request.urlopen("http://127.0.0.1:" + UPSTREAM + self.path) as r:
                body = r.read(); self.send_response(r.status)
                self.send_header("Content-Type", r.headers["Content-Type"]); self.end_headers(); self.wfile.write(body)
            return
        super().do_GET()
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(("127.0.0.1", PORT), functools.partial(H, directory=ROOT)).serve_forever()
