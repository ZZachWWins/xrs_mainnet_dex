"""Loopback settlement API. Admission requires a healthy authorized executor.

The default executable serves paused capabilities only. No private keys are
loaded here. The journal verifies intent ownership; the worker independently
verifies chain execution. Tokens grant status reads, never fund movement.
"""
import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from journal import Journal, JournalError, NETWORK, ROUTES, canonical, identifier, raw_amount
from public_rpc import PublicReadClient, strict_json
from solana_verify import associated_token_address, b58decode
from xeris_purchase import quote_purchase
from xeris_redemption import quote_redemption

PREFIX = '/settlement/v1'
ORIGINS = frozenset(('https://dex.xerisweb.com', 'http://127.0.0.1:8769', 'http://localhost:8769'))
QUOTE_FIELDS = frozenset(('client_request_id', 'action', 'route', 'amount_raw',
    'source_owner', 'destination_owner', 'solana_token_account', 'slippage_bps'))
FLAT_FIELDS = ('client_request_id', 'quote_id', 'action', 'route', 'network',
    'source_owner', 'destination_owner', 'solana_token_account', 'amount_raw', 'min_output_raw')
STATES = frozenset(('awaiting_funding', 'processing', 'completed', 'failed',
                    'cancelled', 'manual_review', 'refunded'))


class ApiError(ValueError):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def exact(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ApiError(400, 'invalid_fields')


class ApiStore:
    """Immutable public quotes plus hashed read-access tokens; separate from funds."""
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ApiError(500, 'invalid_store')
        existed = path.exists() and path.stat().st_size > 0
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        if existed:
            tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'settlement_api_metadata' not in tables:
                self.db.close()
                raise ApiError(500, 'unrelated_store')
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS settlement_api_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS api_quotes(client_id TEXT PRIMARY KEY,quote_id TEXT UNIQUE NOT NULL,
            request_json TEXT NOT NULL,quote_json TEXT NOT NULL,expires_at INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS api_access(client_id TEXT PRIMARY KEY,intent_id TEXT UNIQUE NOT NULL,
            token_hash TEXT NOT NULL);
        ''')
        binding = canonical({'version': 1, 'network': NETWORK})
        self.db.execute('INSERT OR IGNORE INTO settlement_api_metadata VALUES(?,?)', ('binding', binding))
        if self.db.execute("SELECT value FROM settlement_api_metadata WHERE key='binding'").fetchone()[0] != binding:
            self.db.close()
            raise ApiError(500, 'store_network_mismatch')
        path.chmod(0o600)

    def close(self):
        self.db.close()

    def quote(self, client_id):
        return self.db.execute('SELECT * FROM api_quotes WHERE client_id=?', (client_id,)).fetchone()

    def put_quote(self, request, quote):
        if self.db.execute('SELECT COUNT(*) FROM api_quotes').fetchone()[0] >= 10000:
            raise ApiError(503, 'quote_capacity_reached')
        try:
            self.db.execute('INSERT INTO api_quotes VALUES(?,?,?,?,?)',
                (quote['client_request_id'], quote['quote_id'], canonical(request), canonical(quote), quote['expires_at']))
        except sqlite3.IntegrityError:
            previous = self.quote(quote['client_request_id'])
            if previous is None or previous['request_json'] != canonical(request):
                raise ApiError(409, 'client_request_conflict')
            return json.loads(previous['quote_json'])
        return quote

    def prune_quotes(self, now, accepted_clients):
        # Never prune a funded/accepted request's quote, including the narrow
        # crash window before a status token was issued in this separate store.
        accepted = set(accepted_clients)
        for row in self.db.execute('SELECT client_id FROM api_quotes WHERE expires_at<=? LIMIT 256', (now,)).fetchall():
            if row[0] not in accepted:
                self.db.execute('DELETE FROM api_quotes WHERE client_id=? AND NOT EXISTS(SELECT 1 FROM api_access WHERE client_id=?)', (row[0], row[0]))

    def issue_access(self, status):
        token = secrets.token_hex(32)
        client_id, intent_id = status['client_request_id'], status['id']
        previous = self.db.execute('SELECT intent_id FROM api_access WHERE client_id=?', (client_id,)).fetchone()
        if previous and previous['intent_id'] != intent_id:
            raise ApiError(409, 'client_request_conflict')
        self.db.execute('INSERT INTO api_access VALUES(?,?,?) ON CONFLICT(client_id) DO UPDATE SET token_hash=excluded.token_hash',
                        (client_id, intent_id, hashlib.sha256(token.encode()).hexdigest()))
        return token

    def authorize(self, client_id, token):
        if type(token) is not str or re.fullmatch('[0-9a-f]{64}', token) is None:
            raise ApiError(401, 'access_required')
        row = self.db.execute('SELECT * FROM api_access WHERE client_id=?', (client_id,)).fetchone()
        if not row or not hmac.compare_digest(row['token_hash'], hashlib.sha256(token.encode()).hexdigest()):
            raise ApiError(401, 'access_required')
        return row['intent_id']


class SettlementService:
    def __init__(self, journal, store, reader=None, executor=None, clock=None):
        self.journal, self.store = journal, store
        self.reader, self.executor = reader or PublicReadClient(), executor
        self.clock = clock or (lambda: int(time.time()))

    def capabilities(self):
        now = self.clock()
        enabled = {route: False for route in ROUTES.values()}
        if self.executor is not None and not self.journal.processing_state()['paused']:
            try:
                health = self.executor.readiness()
                # The executor must revalidate signer policy, backing, fees,
                # archive completeness and recovery before asserting readiness.
                if health.get('can_accept_funds') is True and type(health.get('checked_at')) is int and 0 <= now-health['checked_at'] <= 10:
                    enabled = {route: health.get('routes', {}).get(route) is True for route in enabled}
            except Exception:
                pass
        return {'schema_version': 1, 'network': dict(NETWORK), 'can_accept_funds': any(enabled.values()),
            'routes': {route: {'enabled': value} for route, value in enabled.items()},
            'issued_at': now, 'expires_at': now + 15}

    def _admit(self, route):
        caps = self.capabilities()
        if not caps['can_accept_funds'] or not caps['routes'].get(route, {}).get('enabled'):
            raise ApiError(503, 'settlement_unavailable')

    def quote(self, request):
        exact(request, QUOTE_FIELDS)
        action = request['action']
        if action not in ROUTES or request['route'] != ROUTES[action] or type(request['slippage_bps']) is not int or request['slippage_bps'] != 50:
            raise ApiError(400, 'invalid_route_or_slippage')
        self._admit(request['route'])
        if hasattr(self.executor, 'admit_new_request'):
            self.executor.admit_new_request()
        identifier(request['client_request_id'])
        amount = raw_amount(request['amount_raw'])
        for field in ('source_owner', 'destination_owner', 'solana_token_account'):
            b58decode(request[field], 32)
        owner = request['source_owner'] if action == 'buy' else request['destination_owner']
        if associated_token_address(owner) != request['solana_token_account']:
            raise ApiError(400, 'wrong_usdc_account')
        maximum = self.journal.policy.maximum_native_raw if action == 'sell' else self.journal.policy.maximum_usdc_raw
        if amount > maximum:
            raise ApiError(400, 'amount_exceeds_limit')
        accepted_clients = [json.loads(r[0])['client_request_id'] for r in self.journal.db.execute('SELECT intent_json FROM settlement_intents')]
        self.store.prune_quotes(self.clock(), accepted_clients)
        self.journal.db.execute('DELETE FROM settlement_challenges WHERE accepted_id IS NULL AND expires_at<=?', (self.clock(),))
        previous = self.store.quote(request['client_request_id'])
        if previous:
            if previous['request_json'] != canonical(request):
                raise ApiError(409, 'client_request_conflict')
            if previous['expires_at'] <= self.clock():
                raise ApiError(409, 'quote_expired')
            return json.loads(previous['quote_json'])
        if action == 'redeem':
            expected = minimum = amount
        else:
            pool = self.reader.pool()
            if action == 'buy':
                q = quote_purchase(pool['reserve_a'], pool['reserve_b'], amount)
                expected, minimum = q['expected_xrs_raw'], q['minimum_xrs_raw']
            else:
                q = quote_redemption(pool['reserve_a'], pool['reserve_b'], amount)
                expected, minimum = q['expected_xusdc_raw'], q['minimum_xusdc_raw']
                if expected > self.journal.policy.maximum_usdc_raw:
                    raise ApiError(400, 'amount_exceeds_limit')
        now = self.clock()
        quote = {key: request[key] for key in QUOTE_FIELDS - {'slippage_bps'}}
        quote.update(schema_version=1, quote_id=secrets.token_hex(16), network=dict(NETWORK),
                     min_output_raw=str(minimum), expected_output_raw=str(expected), created_at=now, expires_at=now+60)
        return self.store.put_quote(request, quote)

    def challenge(self, body):
        exact(body, ('quote_id', 'client_request_id'))
        record = self.store.quote(identifier(body['client_request_id']))
        if not record or record['quote_id'] != body['quote_id']:
            raise ApiError(404, 'quote_not_found')
        quote = json.loads(record['quote_json'])
        self._admit(quote['route'])
        if quote['expires_at'] <= self.clock():
            raise ApiError(409, 'quote_expired')
        fields = {key: quote[key] for key in ('action', 'source_owner', 'destination_owner',
            'solana_token_account', 'amount_raw', 'min_output_raw', 'client_request_id', 'quote_id', 'expires_at')}
        return {'schema_version': 1, **self.journal.issue_challenge(**fields)}

    def _flat(self, status):
        intent, stages = status['intent'], status['stages']
        source_stage = 'deposit' if intent['action'] == 'buy' else 'burn'
        destination_stage = 'deliver' if intent['action'] == 'buy' else 'payout'
        tx = {stage['stage']: stage['transaction_id'] for stage in stages}
        amounts = {stage['stage']: stage['amount_raw'] for stage in stages if stage['state'] == 'confirmed'}
        state = status['status'] if status['status'] in STATES else 'manual_review'
        return {**{key: intent[key] for key in FLAT_FIELDS}, 'status': state,
            'updated_at': max([intent['created_at']] + [stage['updated_at'] for stage in stages]),
            'source_txid': tx.get(source_stage), 'destination_txid': tx.get('refund') or tx.get(destination_stage),
            'actual_output_raw': amounts.get(destination_stage) if state == 'completed' else None,
            'refund_amount_raw': amounts.get('refund') if state == 'refunded' else None,
            'refund_asset': 'solana_usdc' if state == 'refunded' and 'refund' in amounts else None}

    def accept(self, body, idempotency_key):
        exact(body, ('intent', 'signature'))
        intent = body['intent']
        if type(intent) is not dict or idempotency_key != intent.get('client_request_id'):
            raise ApiError(400, 'idempotency_key_required')
        # Lost acknowledgement must remain recoverable even after admission is
        # paused or the quote expires. This path only authenticates an existing
        # immutable record; it never creates a new intent or source transaction.
        previous = self.journal.db.execute('SELECT id FROM settlement_intents WHERE intent_json=?', (canonical(intent),)).fetchone()
        if previous:
            status = self.journal.verify_existing_intent(intent, body['signature'])
            return {'schema_version': 1, 'request': self._flat(status), 'status_token': self.store.issue_access(status)}
        self._admit(intent.get('route'))
        record = self.store.quote(identifier(intent.get('client_request_id')))
        if not record or record['quote_id'] != intent.get('quote_id'):
            raise ApiError(404, 'quote_not_found')
        quote = json.loads(record['quote_json'])
        if any(canonical(intent.get(key)) != canonical(quote[key]) for key in FLAT_FIELDS):
            raise ApiError(409, 'intent_quote_mismatch')
        if quote['expires_at'] <= self.clock() or intent.get('expires_at') != quote['expires_at']:
            raise ApiError(409, 'quote_expired')
        status = self.journal.accept_intent(intent, body['signature'])
        return {'schema_version': 1, 'request': self._flat(status), 'status_token': self.store.issue_access(status)}

    def restore_access(self, client_id, body):
        exact(body, ('intent', 'signature'))
        if type(body['intent']) is not dict or body['intent'].get('client_request_id') != client_id:
            raise ApiError(400, 'request_mismatch')
        status = self.journal.verify_existing_intent(body['intent'], body['signature'])
        return {'schema_version': 1, 'request': self._flat(status), 'status_token': self.store.issue_access(status)}

    def status(self, client_id, authorization):
        token = authorization[7:] if isinstance(authorization, str) and authorization.startswith('Bearer ') else ''
        intent_id = self.store.authorize(client_id, token)
        status = self.executor.status(intent_id) if self.executor is not None else self.journal.status(intent_id)
        return {'schema_version': 1, 'request': self._flat(status)}

    def source(self, client_id, operation, body, authorization):
        token = authorization[7:] if isinstance(authorization, str) and authorization.startswith('Bearer ') else ''
        intent_id = self.store.authorize(client_id, token)
        if self.executor is None:
            raise ApiError(503, 'settlement_unavailable')
        if operation == 'prepare-source':
            exact(body, ())
            return {'schema_version': 1, 'source': self.executor.prepare_source(intent_id)}
        exact(body, ('transaction_base64',))
        # Do not run global admission here: a paused/expired original must still
        # be recoverable. Executor gates new broadcasts, not durable observation.
        status = self.executor.submit_source(intent_id, body['transaction_base64'])
        return {'schema_version': 1, 'request': self._flat(status)}

    def dispatch(self, method, path, body=None, headers=None):
        headers = headers or {}
        if method == 'GET' and path == PREFIX+'/capabilities':
            return self.capabilities()
        if method == 'POST' and path == PREFIX+'/quotes':
            return self.quote(body)
        if method == 'POST' and path == PREFIX+'/challenges':
            return self.challenge(body)
        if method == 'POST' and path == PREFIX+'/requests':
            return self.accept(body, headers.get('Idempotency-Key'))
        match = re.fullmatch(re.escape(PREFIX)+r'/requests/([A-Za-z0-9_-]{1,128})(/(access|prepare-source|submit-source))?', path)
        if match and method == 'GET' and not match[2]:
            return self.status(match[1], headers.get('Authorization'))
        if match and method == 'POST' and match[3] == 'access':
            return self.restore_access(match[1], body)
        if match and method == 'POST' and match[3] in ('prepare-source', 'submit-source'):
            return self.source(match[1], match[3], body, headers.get('Authorization'))
        raise ApiError(404, 'not_found')


class LimitedServer(HTTPServer):
    request_queue_size = 16
    def get_request(self):
        socket, address = super().get_request()
        socket.settimeout(5)
        return socket, address


def handler_for(service):
    buckets = {}
    class Handler(BaseHTTPRequestHandler):
        server_version = 'XerisSettlement/1'
        sys_version = ''
        protocol_version = 'HTTP/1.0'
        def log_message(self, *_):
            pass  # Never log ownership proofs, request bodies or bearer tokens.
        def _origin(self):
            origin = self.headers.get('Origin')
            if origin is not None and origin not in ORIGINS:
                raise ApiError(403, 'origin_rejected')
            return origin
        def _limit(self):
            now = time.monotonic()
            peer = self.client_address[0]
            try:
                if ipaddress.ip_address(peer).is_loopback and self.headers.get('X-Real-IP'):
                    peer = str(ipaddress.ip_address(self.headers['X-Real-IP']))
            except ValueError:
                raise ApiError(400, 'invalid_proxy_identity')
            if len(buckets) >= 2048:
                for key, value in list(buckets.items()):
                    if now-value[0] > 60:
                        del buckets[key]
                if peer not in buckets and len(buckets) >= 2048:
                    raise ApiError(429, 'rate_limited')
            start, count = buckets.get(peer, (now, 0))
            if now-start >= 60:
                start, count = now, 0
            buckets[peer] = (start, count+1)
            if count >= 90:
                raise ApiError(429, 'rate_limited')
        def _reply(self, status, result, origin=None):
            data = json.dumps(result, separators=(',', ':'), allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Vary', 'Origin')
            if origin in ORIGINS:
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, Idempotency-Key')
            if status == 429:
                self.send_header('Retry-After', '60')
            self.end_headers()
            self.wfile.write(data)
        def _run(self):
            origin = None
            try:
                origin = self._origin()
                self._limit()
                if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Encoding'):
                    raise ApiError(400, 'encoding_rejected')
                for name in ('Content-Length', 'Content-Type', 'Authorization', 'Origin', 'Idempotency-Key', 'X-Real-IP'):
                    if len(self.headers.get_all(name, [])) > 1:
                        raise ApiError(400, 'duplicate_header')
                if self.command == 'OPTIONS':
                    if origin is None:
                        raise ApiError(403, 'origin_required')
                    self._reply(200, {}, origin)
                    return
                if len(self.path) > 512 or urlsplit(self.path).query:
                    raise ApiError(400, 'invalid_path')
                body = None
                if self.command == 'POST':
                    if origin is None:
                        raise ApiError(403, 'origin_required')
                    if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                        raise ApiError(415, 'json_required')
                    length = self.headers.get('Content-Length', '')
                    if not re.fullmatch(r'[0-9]{1,5}', length) or not 1 <= int(length) <= 16384:
                        raise ApiError(413, 'body_size_rejected')
                    data = self.rfile.read(int(length))
                    if len(data) != int(length):
                        raise ApiError(400, 'truncated_body')
                    body = strict_json(data)
                result = service.dispatch(self.command, self.path, body, self.headers)
                self._reply(200, result, origin)
            except ApiError as error:
                self._reply(error.status, {'error': error.code}, origin)
            except (ValueError, TypeError, KeyError, OverflowError, JournalError):
                self._reply(400, {'error': 'request_rejected'}, origin)
            except Exception:
                self._reply(503, {'error': 'service_unavailable'}, origin)
        do_GET = do_POST = do_OPTIONS = _run
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-directory', required=True)
    parser.add_argument('--port', type=int, default=8791)
    parser.add_argument('--openssl')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('A loopback high port is required')
    directory = Path(args.state_directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    journal = Journal(directory/'settlement.sqlite3', openssl=args.openssl)
    store = ApiStore(directory/'settlement-api.sqlite3')
    service = SettlementService(journal, store)  # No executor: always unavailable.
    server = LimitedServer(('127.0.0.1', args.port), handler_for(service))
    try:
        server.serve_forever()
    finally:
        server.server_close(); journal.close(); store.close()


if __name__ == '__main__':
    main()
