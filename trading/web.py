"""Small loopback-only HTTP boundary. There are no broker order routes."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import socket
from pathlib import Path
import tempfile

MAX_BODY = 2_000_000


def create_server(engine, host='127.0.0.1', port=8765):
    if host not in ('127.0.0.1', 'localhost'):
        raise ValueError('Dashboard must bind to 127.0.0.1 or localhost')

    class Handler(BaseHTTPRequestHandler):
        server_version = 'PaperWorkbench'

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass

        def reply(self, status, payload, html=False):
            body = payload if html else json.dumps(payload, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)
            if not getattr(self, '_body_read', False) and self.headers.get('Content-Length', '0') != '0':
                # Half-close the response before a bounded drain. This preserves
                # error responses on Windows when a client is still uploading.
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_WR)
                self.connection.settimeout(0.25)
                try:
                    remaining = min(max(int(self.headers.get('Content-Length', '0')), 0), MAX_BODY + 65536)
                    while remaining:
                        chunk = self.rfile.read(min(remaining, 65536))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                except (OSError, ValueError):
                    pass

        def trusted(self):
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            hosts = self.headers.get_all('Host', [])
            origins = self.headers.get_all('Origin', [])
            if len(hosts) != 1 or hosts[0] not in allowed:
                return False
            if origins and (len(origins) != 1 or origins[0] != 'http://' + hosts[0]):
                return False
            return True

        def do_GET(self):
            if not self.trusted():
                return self.reply(403, {'error': 'Untrusted Host or Origin'})
            if self.path == '/':
                return self.reply(200, Path(__file__).with_name('static').joinpath('index.html').read_bytes(), True)
            if self.path == '/api/state':
                try:
                    return self.reply(200, engine.snapshot())
                except Exception:
                    return self.reply(500, {'error': 'Unable to read local paper state'})
            self.reply(404, {'error': 'Route not found'})

        def do_POST(self):
            if not self.trusted():
                return self.reply(403, {'error': 'Untrusted Host or Origin'})
            if self.path not in ('/api/demo', '/api/pause', '/api/deposit', '/api/import'):
                return self.reply(404, {'error': 'Route not found'})
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get('Transfer-Encoding') or len(lengths) != 1:
                return self.reply(400, {'error': 'A single Content-Length is required'})
            try:
                size = int(lengths[0])
            except ValueError:
                return self.reply(400, {'error': 'Invalid content length'})
            if size > MAX_BODY:
                return self.reply(413, {'error': 'Upload exceeds 2 MB limit'})
            if size < 0 or self.headers.get_content_type() != 'application/json':
                return self.reply(400, {'error': 'Expected a JSON request'})
            try:
                raw = self.rfile.read(size)
                self._body_read = True
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError('Expected a JSON object')
                if self.path == '/api/demo':
                    if data:
                        raise ValueError('Demo does not accept arguments')
                    result = engine.demo()
                elif self.path == '/api/pause':
                    if set(data) != {'paused'} or not isinstance(data['paused'], bool):
                        raise ValueError('paused must be true or false')
                    result = engine.set_paused(data['paused'])
                elif self.path == '/api/deposit':
                    amount, reference = data.get('amount'), data.get('reference')
                    if set(data) != {'amount', 'reference'} or type(amount) not in (int, float) or not math.isfinite(amount) or not 0 < amount <= 1_000_000:
                        raise ValueError('Enter a positive deposit of at most GBP 1,000,000')
                    if not isinstance(reference, str) or not reference.strip() or len(reference) > 120:
                        raise ValueError('A reference of 1–120 characters is required')
                    result = engine.deposit(amount, reference.strip())
                else:
                    if set(data) != {'csv'} or not isinstance(data['csv'], str) or not data['csv'].strip():
                        raise ValueError('Supply CSV text in the csv field')
                    with tempfile.TemporaryDirectory(prefix='paper-import-') as directory:
                        path = Path(directory) / 'import.csv'
                        path.write_text(data['csv'], encoding='utf-8')
                        result = engine.process_csv(str(path))
                self.reply(200, {'ok': True, 'result': result})
            except (ValueError, TypeError, UnicodeError) as exc:
                self.reply(400, {'error': str(exc)[:300]})
            except Exception:
                self.reply(500, {'error': 'Local operation failed; check your data and try again'})

    return HTTPServer(('127.0.0.1', port), Handler)
