"""Bounded public read API and SSE broadcaster; collector reads localhost only."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import signal
from socketserver import ThreadingMixIn
import threading
import time

from collector import CHAIN, TransientCollectionError, collect


class Feed:
    def __init__(self, policy, collector=collect):
        self.policy = policy
        self.collector = collector
        self.condition = threading.Condition()
        self.version = 0
        self.snapshot = {'schema_version': '2', 'status': 'unavailable', 'chain_id': CHAIN,
                         'reason': 'Mainnet census is initializing.'}
        self.stop = threading.Event()

    def publish(self, snapshot):
        with self.condition:
            self.snapshot = snapshot
            self.version += 1
            self.condition.notify_all()

    def current(self):
        with self.condition:
            snapshot = self.snapshot
            if snapshot['status'] == 'ready' and int(snapshot['expires_at_ms']) <= int(time.time() * 1000):
                snapshot = {'schema_version': '2', 'status': 'unavailable', 'chain_id': CHAIN,
                            'reason': 'Mainnet census has expired.'}
                self.snapshot = snapshot
                self.version += 1
                self.condition.notify_all()
            return self.version, snapshot

    def run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.publish(self.collector(self.policy))
            except TransientCollectionError:
                # Keep only an already verified snapshot within its ORIGINAL
                # expiry. Do not relabel, republish or extend it after a race.
                with self.condition:
                    if self.snapshot['status'] != 'ready' or int(self.snapshot['expires_at_ms']) <= int(time.time() * 1000):
                        self.publish({'schema_version': '2', 'status': 'unavailable', 'chain_id': CHAIN,
                                      'reason': 'A complete, consistent mainnet census is temporarily unavailable.'})
            except Exception:
                # No stale value is advertised as live and no node response or
                # filesystem detail is returned to public clients on failure.
                self.publish({'schema_version': '2', 'status': 'unavailable', 'chain_id': CHAIN,
                              'reason': 'A complete, consistent mainnet census is temporarily unavailable.'})
            self.stop.wait(max(1, 5 - (time.monotonic() - started)))


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, address, feed):
        self.feed = feed
        self.workers = threading.BoundedSemaphore(32)
        self.streams = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.workers.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.workers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.workers.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'XerisMarketFeed/2'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass

    def send_headers(self, code, content_type, length=None):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.end_headers()

    def json(self, data, code=200):
        body = json.dumps(data, separators=(',', ':'), ensure_ascii=True).encode()
        self.send_headers(code, 'application/json; charset=utf-8', len(body))
        self.wfile.write(body)

    def do_OPTIONS(self):
        if self.path not in ('/market/supply', '/market/status', '/market/stream'):
            self.json({'error': 'Not found.'}, 404)
            return
        self.send_headers(204, 'application/json', 0)

    def do_POST(self):
        self.close_connection = True
        self.json({'error': 'Read-only endpoint.'}, 405)

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def do_GET(self):
        if self.path == '/market/stream':
            self.stream()
            return
        if self.path not in ('/market/supply', '/market/status'):
            self.json({'error': 'Not found.'}, 404)
            return
        _, snapshot = self.server.feed.current()
        if self.path == '/market/status':
            snapshot = {key: snapshot[key] for key in ('schema_version', 'chain_id', 'status', 'reason',
                        'observed_at', 'observed_at_ms', 'expires_at_ms', 'as_of', 'coverage') if key in snapshot}
        self.json(snapshot, 200 if snapshot['status'] == 'ready' else 503)

    def stream(self):
        if not self.server.streams.acquire(blocking=False):
            self.json({'error': 'Stream capacity reached.'}, 429)
            return
        self.close_connection = True
        try:
            self.send_headers(200, 'text/event-stream; charset=utf-8')
            self.wfile.write(b'retry: 5000\n\n')
            self.wfile.flush()
            version = -1
            # Finite lifetime bounds idle clients; EventSource reconnects.
            deadline = time.monotonic() + 300
            while not self.server.feed.stop.is_set() and time.monotonic() < deadline:
                current, snapshot = self.server.feed.current()
                if current != version:
                    payload = json.dumps(snapshot, separators=(',', ':'), ensure_ascii=True)
                    self.wfile.write(f'event: snapshot\nid: {current}\ndata: {payload}\n\n'.encode())
                    version = current
                else:
                    self.wfile.write(b': heartbeat\n\n')
                self.wfile.flush()
                with self.server.feed.condition:
                    self.server.feed.condition.wait_for(lambda: self.server.feed.version != version or self.server.feed.stop.is_set(), timeout=10)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.server.streams.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1'])
    parser.add_argument('--port', default=56100, type=int)
    parser.add_argument('--policy', required=True)
    args = parser.parse_args()
    policy = json.loads(Path(args.policy).read_text())
    if policy.get('chain_id') != CHAIN:
        raise ValueError('Wrong market policy chain.')
    feed = Feed(policy)
    server = Server((args.host, args.port), feed)
    def stop(*_):
        feed.stop.set()
        with feed.condition:
            feed.condition.notify_all()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threading.Thread(target=feed.run, daemon=True).start()
    server.serve_forever(poll_interval=0.5)
    server.server_close()


if __name__ == '__main__':
    main()
