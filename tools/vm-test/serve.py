#!/usr/bin/env python3
"""Local release/result server for the B-50 VM test.

Serves the repo tarball and a signed release from a directory over HTTP, and appends the guest's
POSTed result to result.log. Test-only; binds loopback/all-interfaces so a QEMU guest on a NAT or
user network can reach it. Never used in production.
"""
import http.server
import os
import socketserver

ROOT = os.environ.get("VM_TEST_ROOT", ".")
PORT = int(os.environ.get("VM_TEST_PORT", "8000"))


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        with open(os.path.join(ROOT, "result.log"), "ab") as f:
            f.write(body + b"\n===END===\n")
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
    httpd.serve_forever()
