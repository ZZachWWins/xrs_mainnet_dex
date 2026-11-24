import io
import json
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from service import Feed, Handler, Server


def ready(expires='2000000'):
    return {'schema_version': '2', 'chain_id': 'xeris-mainnet-v4', 'status': 'ready',
            'expires_at_ms': expires, 'market': {'market_cap_quote_raw': '123'},
            'coverage': {'complete': True}}


def handler(feed, path='/market/supply'):
    instance = Handler.__new__(Handler)
    instance.server = SimpleNamespace(feed=feed, streams=threading.BoundedSemaphore(16))
    instance.path = path
    instance.wfile = io.BytesIO()
    instance.send_headers = Mock()
    return instance


class ServiceTests(unittest.TestCase):
    def test_expiry_advances_version_once_so_sse_clients_receive_unavailable(self):
        feed = Feed({})
        feed.publish(ready())
        with patch('service.time.time', return_value=1999):
            self.assertEqual(feed.current()[0], 1)
        with patch('service.time.time', return_value=2000):
            version, snapshot = feed.current()
            self.assertEqual(version, 2)
            self.assertEqual(snapshot['status'], 'unavailable')
            self.assertNotIn('market', snapshot)
            self.assertEqual(feed.current()[0], 2)

    def test_failure_removes_previous_live_value_and_does_not_expose_error_details(self):
        feed = Feed({})
        feed.publish(ready())
        def fail(_):
            feed.stop.set()
            raise RuntimeError('sensitive local implementation detail')
        feed.collector = fail
        feed.run()
        snapshot = feed.current()[1]
        self.assertEqual(snapshot['status'], 'unavailable')
        self.assertNotIn('market', snapshot)
        self.assertNotIn('sensitive', json.dumps(snapshot))

    def test_read_api_returns_503_when_unavailable_and_status_omits_market(self):
        feed = Feed({})
        h = handler(feed)
        h.do_GET()
        self.assertEqual(h.send_headers.call_args.args[0], 503)
        feed.publish(ready(expires='9999999999999'))
        h = handler(feed, '/market/status')
        h.do_GET()
        self.assertEqual(h.send_headers.call_args.args[0], 200)
        self.assertNotIn('market', json.loads(h.wfile.getvalue()))

    def test_route_matching_and_methods_are_exact(self):
        feed = Feed({})
        for path in ['/market/supply?x=1', '/market/supply/', '/other']:
            h = handler(feed, path)
            h.do_GET()
            self.assertEqual(h.send_headers.call_args.args[0], 404)
        h = handler(feed)
        h.do_POST()
        self.assertEqual(h.send_headers.call_args.args[0], 405)
        self.assertTrue(h.close_connection)
        h = handler(feed)
        h.do_OPTIONS()
        self.assertEqual(h.send_headers.call_args.args[0], 204)

    def test_sse_capacity_is_bounded_and_does_not_consume_worker_forever(self):
        h = handler(Feed({}), '/market/stream')
        for _ in range(16):
            self.assertTrue(h.server.streams.acquire(blocking=False))
        h.stream()
        self.assertEqual(h.send_headers.call_args.args[0], 429)
        self.assertFalse(h.server.streams.acquire(blocking=False))

    def test_disconnected_sse_releases_stream_semaphore(self):
        class Disconnected:
            def write(self, _):
                raise BrokenPipeError()
        h = handler(Feed({}), '/market/stream')
        h.wfile = Disconnected()
        h.stream()
        for _ in range(16):
            self.assertTrue(h.server.streams.acquire(blocking=False))
        self.assertFalse(h.server.streams.acquire(blocking=False))

    def test_sse_emits_named_snapshot_and_retry_without_network(self):
        feed = Feed({})
        h = handler(feed, '/market/stream')
        class StopAfterSnapshot(io.BytesIO):
            def write(self, value):
                result = super().write(value)
                if b'event: snapshot' in value:
                    feed.stop.set()
                return result
        h.wfile = StopAfterSnapshot()
        h.stream()
        body = h.wfile.getvalue()
        self.assertIn(b'retry: 5000\n\n', body)
        self.assertIn(b'event: snapshot\nid: 0\ndata:', body)
        self.assertIn(b'"status":"unavailable"', body)

    def test_worker_bound_rejects_socket_without_starting_thread(self):
        server = Server.__new__(Server)
        server.workers = threading.BoundedSemaphore(1)
        server.workers.acquire()
        request = Mock()
        with patch('service.ThreadingMixIn.process_request') as start:
            server.process_request(request, ('127.0.0.1', 1))
        request.close.assert_called_once()
        start.assert_not_called()

    def test_thread_creation_failure_releases_worker_capacity(self):
        server = Server.__new__(Server)
        server.workers = threading.BoundedSemaphore(1)
        with patch('service.ThreadingMixIn.process_request', side_effect=RuntimeError('cannot start')):
            with self.assertRaises(RuntimeError):
                server.process_request(Mock(), ('127.0.0.1', 1))
        self.assertTrue(server.workers.acquire(blocking=False))


if __name__ == '__main__':
    unittest.main()
