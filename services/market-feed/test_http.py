"""Actual loopback HTTP/SSE integration: catches handler/framework collisions."""
import json
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from service import Feed, Server


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.feed = Feed({})
        self.feed.publish({'schema_version': '2', 'chain_id': 'xeris-mainnet-v4', 'status': 'ready',
                           'expires_at_ms': str(int(time.time() * 1000) + 20000),
                           'market': {'market_cap_quote_raw': '10101886'}, 'coverage': {'complete': True}})
        self.server = Server(('127.0.0.1', 0), self.feed)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.client = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.feed.stop.set()
        with self.feed.condition:
            self.feed.condition.notify_all()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_real_get_and_preflight_serve_json_without_request_header_collision(self):
        with self.client.open(self.base + '/market/supply', timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers['Access-Control-Allow-Origin'], '*')
            self.assertEqual(json.load(response)['market']['market_cap_quote_raw'], '10101886')
        with self.client.open(Request(self.base + '/market/supply', method='OPTIONS'), timeout=2) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.read(), b'')

    def test_real_stream_delivers_snapshot_and_expiry_event(self):
        with self.client.open(self.base + '/market/stream', timeout=2) as response:
            self.assertEqual(response.headers.get_content_type(), 'text/event-stream')
            def snapshot():
                for _ in range(12):
                    line = response.readline()
                    if line.startswith(b'data: '):
                        return json.loads(line[6:])
                self.fail('No SSE snapshot')
            self.assertEqual(snapshot()['status'], 'ready')
            self.feed.publish({'schema_version': '2', 'chain_id': 'xeris-mainnet-v4', 'status': 'unavailable'})
            self.assertEqual(snapshot()['status'], 'unavailable')

    def test_real_write_and_expired_reads_are_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.client.open(Request(self.base + '/market/supply', data=b'{}', method='POST'), timeout=2)
        self.assertEqual(error.exception.code, 405)
        error.exception.close()
        self.feed.snapshot['expires_at_ms'] = '1'
        with self.assertRaises(HTTPError) as error:
            self.client.open(self.base + '/market/supply', timeout=2)
        self.assertEqual(error.exception.code, 503)
        self.assertEqual(json.load(error.exception)['status'], 'unavailable')
        error.exception.close()


if __name__ == '__main__':
    unittest.main()
