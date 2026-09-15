import http.client
import importlib
import json
from pathlib import Path
import threading
import unittest


class FakeEngine:
    def __init__(self):
        self.paused = False
        self.imported = None

    def snapshot(self):
        return {'mode': 'local-paper', 'paused': self.paused, 'cash': 1000,
                'account': 'DU-PRIVATE', 'settings': {'secret': 'PRIVATE'}}

    def demo(self):
        return {'sessions': 250}

    def deposit(self, amount, reference):
        return {'amount': amount}

    def set_paused(self, paused):
        self.paused = paused

    def process_csv(self, path):
        self.imported = Path(path).read_text(encoding='utf-8')
        if self.imported == 'invalid':
            raise ValueError('Invalid CSV')
        return {'imported': True}


class WebTests(unittest.TestCase):
    def setUp(self):
        try:
            self.web = importlib.import_module('trading.web')
        except ModuleNotFoundError:
            self.fail('Dashboard HTTP implementation does not exist yet')
        self.engine = FakeEngine()
        self.server = self.web.create_server(self.engine, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        if hasattr(self, 'server'):
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()

    def request(self, method, route, data=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        body = json.dumps(data) if data is not None else None
        merged = {'Content-Type': 'application/json'}
        merged.update(headers or {})
        conn.request(method, route, body, merged)
        response = conn.getresponse()
        result = (response.status, response.read())
        conn.close()
        return result

    def test_state_and_page(self):
        status, body = self.request('GET', '/api/state')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['mode'], 'local-paper')
        self.assertEqual(self.request('GET', '/')[0], 200)

    def test_demo_pause_and_import(self):
        self.assertEqual(self.request('POST', '/api/demo', {})[0], 200)
        self.assertEqual(self.request('POST', '/api/pause', {'paused': True})[0], 200)
        self.assertTrue(self.engine.paused)
        self.assertEqual(self.request('POST', '/api/import', {'csv': 'date,symbol'})[0], 200)
        self.assertEqual(self.engine.imported, 'date,symbol')

    def test_invalid_input(self):
        for value in [-1, 0, True, '1', float('inf')]:
            self.assertEqual(self.request('POST', '/api/deposit', {'amount': value, 'reference': 'x'})[0], 400)
        self.assertEqual(self.request('POST', '/api/pause', {'paused': 'false'})[0], 400)
        self.assertEqual(self.request('POST', '/api/import', {'path': 'private.csv'})[0], 400)
        self.assertEqual(self.request('POST', '/api/import', {'csv': 'invalid'})[0], 400)
        self.assertEqual(self.request('POST', '/api/import', {'csv': 'a' * (self.web.MAX_BODY + 1)})[0], 413)

    def test_rejects_foreign_host_and_origin_on_all_routes(self):
        for route, method in [('/', 'GET'), ('/api/state', 'GET'), ('/api/demo', 'POST')]:
            for headers in [{'Host': 'attacker.example'}, {'Origin': 'https://attacker.example'}, {'Origin': 'null'}]:
                self.assertEqual(self.request(method, route, {}, headers)[0], 403)
        self.assertEqual(self.request('GET', '/api/state', headers={'Origin': f'http://127.0.0.1:{self.port}'})[0], 200)

    def test_bind_and_export_protection(self):
        with self.assertRaises(ValueError):
            self.web.create_server(self.engine, host='0.0.0.0', port=0)
        main = importlib.import_module('trading.__main__')
        snapshot = self.engine.snapshot()
        snapshot['unsettled_cash'] = 123.45
        public = main.public_snapshot(snapshot)
        self.assertEqual(public['unsettled_cash'], 123.45)
        exported = json.dumps(public)
        self.assertNotIn('PRIVATE', exported)
        self.assertNotIn('secret', exported)


if __name__ == '__main__':
    unittest.main()
