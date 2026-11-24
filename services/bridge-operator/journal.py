"""Durable public-data settlement journal; no signer, RPC, or funds acceptance.

Only a trusted worker may call claim_source/record_outcome after independently
verifying chain evidence. A digest is an audit locator, NOT a chain proof.
mark_submitted must commit BEFORE submitting the stored original transaction.
The journal never permits replacement IDs, even after an uncertain outcome.
Fencing protects database writes; adapters must also fence their signer and
network writes. A paused journal still records already-arrived funds/outcomes.
"""
from contextlib import contextmanager
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import time

SCHEMA_VERSION = 2
DOMAIN = 'xeris-mainnet-settlement-intent-v2'
AUDIENCE = 'https://mainnetrpc.xerisweb.com/settlement/v1'
NETWORK = {
    'solana_genesis': '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d',
    'xeris_chain_id': 'xeris-mainnet-v4',
    'usdc_mint': 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',
    'reserve_token_account': 'BeM8R5hvdKGwiV5jKdug3kXoe6RaFaREjihKFNSjeBD6',
    'xeris_token_id': 'xeris_usdc', 'wrapped_token_id': 'xrs_native',
    'pool_id': 'xrs-xusdc-mainnet-v1',
}
SOLANA = 'solana:mainnet'
XERIS = NETWORK['xeris_chain_id']
ROUTES = {'buy': 'solana_usdc_to_native_xrs', 'redeem': 'xusdc_to_solana_usdc',
          'sell': 'native_xrs_to_solana_usdc'}
STAGES = {'buy': ('deposit', 'mint', 'buy', 'unwrap', 'deliver'),
          'redeem': ('burn', 'payout'), 'sell': ('wrap', 'sell', 'burn', 'payout')}
INTENT_FIELDS = frozenset(('schema_version', 'domain', 'audience', 'action', 'route',
    'nonce', 'created_at', 'expires_at', 'network', 'amount_raw', 'min_output_raw',
    'source_owner', 'destination_owner', 'solana_token_account', 'quote_id', 'client_request_id'))
B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
MAX_RAW = (1 << 64) - 1
SPKI_PREFIX = bytes.fromhex('302a300506032b6570032100')


class JournalError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise JournalError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def decode58(value, size):
    require(type(value) is str and 1 <= len(value) <= 100, 'Invalid base58 length')
    number = 0
    for char in value:
        require(char in B58, 'Invalid base58 character')
        number = number * 58 + B58.index(char)
    result = bytes(len(value) - len(value.lstrip('1'))) + number.to_bytes((number.bit_length() + 7) // 8, 'big')
    require(len(result) == size and result != bytes(size), 'Invalid or zero public identity')
    return result


def raw_amount(value):
    require(type(value) is str and re.fullmatch(r'[1-9][0-9]{0,19}', value),
            'Raw amounts must be positive canonical decimal strings')
    require(int(value) <= MAX_RAW, 'Raw amount exceeds u64')
    return int(value)


def identifier(value):
    require(type(value) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value), 'Invalid identifier')
    return value


def evidence_hash(value):
    require(type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) and value != '0' * 64,
            'Expected nonzero SHA256 digest')
    return value


def intent_message(intent):
    return ('Xeris settlement ownership proof\n' + DOMAIN + '\n' + canonical(intent)).encode('utf-8')


def verify_ed25519(owner, signature, message, executable=None):
    """Detached public verification; temporary files contain no secret material."""
    public = decode58(owner, 32)
    signature_bytes = decode58(signature, 64)
    require(type(message) is bytes and 1 <= len(message) <= 8192, 'Invalid proof message')
    binary = executable or shutil.which('openssl')
    require(binary is not None, 'OpenSSL Ed25519 verifier is required')
    with tempfile.TemporaryDirectory(prefix='xeris-public-proof-') as folder:
        base = Path(folder)
        (base / 'public.der').write_bytes(SPKI_PREFIX + public)
        (base / 'signature.bin').write_bytes(signature_bytes)
        (base / 'message.bin').write_bytes(message)
        try:
            result = subprocess.run([binary, 'pkeyutl', '-verify', '-pubin', '-keyform', 'DER',
                '-inkey', str(base / 'public.der'), '-rawin', '-in', str(base / 'message.bin'),
                '-sigfile', str(base / 'signature.bin')], capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise JournalError('Public ownership verifier unavailable') from error
    require(result.returncode == 0, 'Ownership signature verification failed')


@dataclass(frozen=True)
class Policy:
    maximum_usdc_raw: int = 500_000
    maximum_native_raw: int = 100_000_000_000
    challenge_lifetime_seconds: int = 600

    def validate(self):
        for value in (self.maximum_usdc_raw, self.maximum_native_raw):
            require(type(value) is int and 1 <= value <= MAX_RAW, 'Invalid policy maximum')
        require(type(self.challenge_lifetime_seconds) is int and
                30 <= self.challenge_lifetime_seconds <= 900, 'Invalid challenge lifetime')

    def public(self):
        return {'maximum_usdc_raw': str(self.maximum_usdc_raw),
                'maximum_native_raw': str(self.maximum_native_raw),
                'challenge_lifetime_seconds': self.challenge_lifetime_seconds}


class Journal:
    def __init__(self, path, policy=None, clock=None, openssl=None):
        self.path = Path(path)
        require(self.path.name not in {'pilot.sqlite3', 'pilot.db'}, 'Never open a pilot journal')
        self.policy = policy or Policy()
        self.policy.validate()
        self.clock = clock or (lambda: int(time.time()))
        self.openssl = openssl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        require(not self.path.is_symlink(), 'Journal must not be a symlink')
        # Exclusive creation fixes permissions before any data is written.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        existed = self.path.stat().st_size > 0
        self.db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            if existed:
                tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                require('settlement_metadata' in tables, 'Existing database is not this settlement journal')
                row = self.db.execute("SELECT value FROM settlement_metadata WHERE key='bindings'").fetchone()
                require(row is not None and row[0] == canonical(self._bindings()), 'Journal policy/network binding differs')
            self.db.execute('PRAGMA foreign_keys=ON')
            require(self.db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal', 'WAL is required')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS settlement_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS settlement_challenges(
                nonce TEXT PRIMARY KEY, action TEXT NOT NULL, source_owner TEXT NOT NULL,
                client_request_id TEXT NOT NULL, intent_json TEXT NOT NULL UNIQUE,
                request_fingerprint TEXT NOT NULL, expires_at INTEGER NOT NULL, accepted_id TEXT UNIQUE,
                UNIQUE(action,source_owner,client_request_id));
              CREATE TABLE IF NOT EXISTS settlement_intents(
                id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE REFERENCES settlement_challenges(nonce),
                intent_json TEXT NOT NULL UNIQUE, proof_signature TEXT NOT NULL, accepted_at INTEGER NOT NULL,
                route_mode TEXT NOT NULL DEFAULT 'normal', terminal TEXT);
              CREATE TABLE IF NOT EXISTS settlement_stages(
                intent_id TEXT NOT NULL REFERENCES settlement_intents(id), stage TEXT NOT NULL,
                chain TEXT NOT NULL, transaction_id TEXT NOT NULL, signed_payload_digest TEXT,
                state TEXT NOT NULL, evidence_digest TEXT, amount_raw TEXT,
                prepared_at INTEGER NOT NULL, submitted_at INTEGER, updated_at INTEGER NOT NULL,
                PRIMARY KEY(intent_id,stage), UNIQUE(chain,transaction_id));
              CREATE TABLE IF NOT EXISTS settlement_claims(
                chain TEXT NOT NULL, transaction_id TEXT NOT NULL, intent_id TEXT NOT NULL UNIQUE
                REFERENCES settlement_intents(id), evidence_digest TEXT NOT NULL, amount_raw TEXT NOT NULL,
                PRIMARY KEY(chain,transaction_id));
              CREATE TABLE IF NOT EXISTS settlement_payloads(
                intent_id TEXT NOT NULL,stage TEXT NOT NULL,transaction_id TEXT NOT NULL,
                transaction_base64 TEXT NOT NULL,
                PRIMARY KEY(intent_id,stage),
                FOREIGN KEY(intent_id,stage) REFERENCES settlement_stages(intent_id,stage));
              CREATE TABLE IF NOT EXISTS settlement_obligations(
                intent_id TEXT PRIMARY KEY REFERENCES settlement_intents(id), kind TEXT NOT NULL,
                asset TEXT NOT NULL, amount_raw TEXT NOT NULL, updated_at INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS settlement_route(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), intent_id TEXT,
                worker_id TEXT, fence INTEGER NOT NULL, expires_at INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS settlement_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL,
                intent_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL);
            ''')
            with self._atomic():
                self.db.execute('INSERT OR IGNORE INTO settlement_metadata VALUES(?,?)', ('bindings', canonical(self._bindings())))
                require(self.db.execute("SELECT value FROM settlement_metadata WHERE key='bindings'").fetchone()[0] == canonical(self._bindings()),
                        'Concurrent initialization used different policy/network bindings')
                self.db.execute('INSERT OR IGNORE INTO settlement_metadata VALUES(?,?)', ('paused', canonical({'paused': True, 'reason': 'Initial offline preparation'})))
                self.db.execute('INSERT OR IGNORE INTO settlement_route VALUES(1,NULL,NULL,0,0)')
            self.path.chmod(0o600)
        except Exception:
            self.db.close()
            raise

    def _bindings(self):
        return {'schema_version': SCHEMA_VERSION, 'domain': DOMAIN, 'audience': AUDIENCE,
                'network': NETWORK, 'policy': self.policy.public()}

    def close(self):
        self.db.close()

    @contextmanager
    def _atomic(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _now(self):
        value = self.clock()
        require(type(value) is int and 0 <= value < (1 << 63), 'Clock must return Unix integer seconds')
        return value

    @contextmanager
    def _snapshot(self):
        self.db.execute('BEGIN')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _event(self, kind, intent_id, payload):
        self.db.execute('INSERT INTO settlement_events(at,intent_id,kind,payload) VALUES(?,?,?,?)',
                        (self._now(), intent_id, kind, canonical(payload)))

    def _validate(self, intent):
        require(type(intent) is dict and set(intent) == INTENT_FIELDS, 'Unknown or missing intent fields')
        require(type(intent['schema_version']) is int and intent['schema_version'] == SCHEMA_VERSION, 'Wrong schema')
        require(intent['domain'] == DOMAIN and intent['audience'] == AUDIENCE, 'Wrong proof domain/audience')
        require(type(intent['network']) is dict and canonical(intent['network']) == canonical(NETWORK), 'Wrong network binding')
        action = intent['action']
        require(type(action) is str and action in ROUTES and intent['route'] == ROUTES[action], 'Wrong action/route')
        require(type(intent['nonce']) is str and re.fullmatch('[0-9a-f]{64}', intent['nonce']), 'Invalid nonce')
        require(type(intent['created_at']) is int and 0 <= intent['created_at'] < (1 << 63) and
                type(intent['expires_at']) is int and 1 <= intent['expires_at'] - intent['created_at'] <= self.policy.challenge_lifetime_seconds,
                'Invalid challenge times')
        maximum = self.policy.maximum_native_raw if action == 'sell' else self.policy.maximum_usdc_raw
        require(raw_amount(intent['amount_raw']) <= maximum, 'Amount exceeds policy maximum')
        raw_amount(intent['min_output_raw'])
        if action == 'redeem':
            require(int(intent['min_output_raw']) <= int(intent['amount_raw']), 'Redemption minimum exceeds input')
        for field in ('source_owner', 'destination_owner', 'solana_token_account'):
            decode58(intent[field], 32)
        for field in ('quote_id', 'client_request_id'):
            identifier(intent[field])

    def issue_challenge(self, *, action, source_owner, destination_owner, solana_token_account,
                        amount_raw, min_output_raw, client_request_id, quote_id, expires_at=None):
        now = self._now()
        require(type(action) is str and action in ROUTES, 'Unknown action')
        if expires_at is None:
            expires_at = now + self.policy.challenge_lifetime_seconds
        intent = {'schema_version': SCHEMA_VERSION, 'domain': DOMAIN, 'audience': AUDIENCE,
            'action': action, 'route': ROUTES.get(action), 'nonce': secrets.token_hex(32),
            'created_at': now, 'expires_at': expires_at,
            'network': dict(NETWORK), 'amount_raw': amount_raw, 'min_output_raw': min_output_raw,
            'source_owner': source_owner, 'destination_owner': destination_owner,
            'solana_token_account': solana_token_account, 'client_request_id': client_request_id, 'quote_id': quote_id}
        self._validate(intent)
        fingerprint = canonical({k: v for k, v in intent.items() if k not in ('nonce', 'created_at', 'expires_at')})
        with self._atomic():
            existing = self.db.execute('SELECT * FROM settlement_challenges WHERE action=? AND source_owner=? AND client_request_id=?',
                                      (action, source_owner, client_request_id)).fetchone()
            if existing:
                require(existing['request_fingerprint'] == fingerprint, 'Client request ID already binds different intent')
                intent = json.loads(existing['intent_json'])
            else:
                self.db.execute('INSERT INTO settlement_challenges VALUES(?,?,?,?,?,?,?,NULL)',
                    (intent['nonce'], action, source_owner, client_request_id, canonical(intent), fingerprint, intent['expires_at']))
                self._event('challenge_issued', None, {'nonce': intent['nonce']})
        return {'intent': intent, 'signing_message_utf8': intent_message(intent).decode('utf-8')}

    def accept_intent(self, intent, signature):
        self._validate(intent)
        verify_ed25519(intent['source_owner'], signature, intent_message(intent), self.openssl)
        encoded = canonical(intent)
        with self._atomic():
            row = self.db.execute('SELECT * FROM settlement_challenges WHERE nonce=?', (intent['nonce'],)).fetchone()
            require(row is not None and row['intent_json'] == encoded, 'Challenge is not the server-issued intent')
            if row['accepted_id']:
                accepted_id = row['accepted_id']
            else:
                require(intent['created_at'] <= self._now() < intent['expires_at'], 'Challenge expired or not yet valid')
                accepted_id = 'xsi_' + secrets.token_hex(24)
                self.db.execute('INSERT INTO settlement_intents(id,nonce,intent_json,proof_signature,accepted_at) VALUES(?,?,?,?,?)',
                    (accepted_id, intent['nonce'], encoded, signature, self._now()))
                self.db.execute('UPDATE settlement_challenges SET accepted_id=? WHERE nonce=?', (accepted_id, intent['nonce']))
                self._event('intent_accepted', accepted_id, {'intent_digest': digest(encoded.encode())})
        return self.status(accepted_id)

    def verify_existing_intent(self, intent, signature):
        self._validate(intent)
        verify_ed25519(intent['source_owner'], signature, intent_message(intent), self.openssl)
        row = self.db.execute('SELECT id FROM settlement_intents WHERE intent_json=?', (canonical(intent),)).fetchone()
        require(row is not None, 'No identical accepted intent')
        return self.status(row['id'])

    def _intent(self, intent_id):
        identifier(intent_id)
        row = self.db.execute('SELECT * FROM settlement_intents WHERE id=?', (intent_id,)).fetchone()
        require(row is not None, 'Unknown intent')
        return row, json.loads(row['intent_json'])

    def _stage(self, intent_id, stage):
        return self.db.execute('SELECT * FROM settlement_stages WHERE intent_id=? AND stage=?', (intent_id, stage)).fetchone()

    def _order(self, row, intent):
        return ('deposit', 'refund') if row['route_mode'] == 'refund' else STAGES[intent['action']]

    def _previous(self, row, intent, stage):
        order = self._order(row, intent)
        require(stage in order, 'Stage is not in the active route')
        for prior in order[:order.index(stage)]:
            found = self._stage(row['id'], prior)
            require(found is not None and found['state'] == 'confirmed', 'Previous stage is not confirmed')

    def _expected_chain(self, stage):
        return SOLANA if stage in ('deposit', 'payout', 'refund') else XERIS

    def _amount(self, intent_id, stage):
        found = self._stage(intent_id, stage)
        require(found is not None and found['state'] == 'confirmed' and found['amount_raw'] is not None,
                'Missing confirmed upstream amount')
        return found['amount_raw']

    def _validate_effect(self, intent_id, intent, stage, amount_raw):
        amount = raw_amount(amount_raw)
        if stage in ('buy', 'sell'):
            require(amount >= int(intent['min_output_raw']), 'Verified output is below the signed minimum')
            return
        if stage in ('unwrap', 'deliver'):
            expected = self._amount(intent_id, 'buy')
        elif stage in ('payout',):
            expected = self._amount(intent_id, 'burn')
        elif stage == 'burn' and intent['action'] == 'sell':
            expected = self._amount(intent_id, 'sell')
        else:
            expected = intent['amount_raw']
        require(amount_raw == expected, 'Verified stage amount differs from the bound amount')

    def _obligation(self, intent_id, kind, asset, amount):
        self.db.execute('INSERT INTO settlement_obligations VALUES(?,?,?,?,?) ON CONFLICT(intent_id) DO UPDATE SET kind=excluded.kind,asset=excluded.asset,amount_raw=excluded.amount_raw,updated_at=excluded.updated_at',
                        (intent_id, kind, asset, amount, self._now()))

    def _confirmed(self, intent_id, intent, stage, amount):
        if stage == 'deposit':
            self._obligation(intent_id, 'pending_issuance', 'USDC', amount)
        elif stage == 'mint':
            self._obligation(intent_id, 'purchase_settlement', 'xUSDC', amount)
        elif stage == 'buy':
            self._obligation(intent_id, 'purchase_delivery', 'XRS', amount)
        elif stage == 'burn':
            self._obligation(intent_id, 'unpaid_redemption', 'USDC', amount)
        elif stage in ('deliver', 'payout', 'refund'):
            self.db.execute('DELETE FROM settlement_obligations WHERE intent_id=?', (intent_id,))
            terminal = 'refunded' if stage == 'refund' else 'completed'
            self.db.execute('UPDATE settlement_intents SET terminal=? WHERE id=?', (terminal, intent_id))

    def claim_source(self, intent_id, chain, transaction_id, evidence_digest, amount_raw, *, refund_required=False):
        """Trusted verified observation, never client-asserted deposit/burn evidence."""
        decode58(transaction_id, 64)
        evidence_hash(evidence_digest)
        raw_amount(amount_raw)
        require(type(refund_required) is bool, 'Invalid source refund decision')
        with self._atomic():
            row, intent = self._intent(intent_id)
            stage = 'deposit' if intent['action'] == 'buy' else 'burn'
            require(chain == self._expected_chain(stage), 'Wrong source chain')
            existing = self.db.execute('SELECT * FROM settlement_claims WHERE chain=? AND transaction_id=?', (chain, transaction_id)).fetchone()
            if existing:
                require(existing['intent_id'] == intent_id and existing['amount_raw'] == amount_raw and existing['evidence_digest'] == evidence_digest,
                        'Source transaction already claimed or evidence differs')
                return self.status(intent_id)
            require(row['terminal'] in (None, 'cancelled') and row['route_mode'] == 'normal', 'Route no longer accepts a source claim')
            self._previous(row, intent, stage)
            self._validate_effect(intent_id, intent, stage, amount_raw)
            found = self._stage(intent_id, stage)
            if found:
                require(found['chain'] == chain and found['transaction_id'] == transaction_id and found['state'] in ('prepared', 'submitted', 'unknown'),
                        'Source stage has a different or terminal original transaction')
                self.db.execute("UPDATE settlement_stages SET state='confirmed',evidence_digest=?,amount_raw=?,updated_at=? WHERE intent_id=? AND stage=?",
                    (evidence_digest, amount_raw, self._now(), intent_id, stage))
            else:
                self.db.execute('INSERT INTO settlement_stages VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (intent_id, stage, chain, transaction_id, None, 'confirmed', evidence_digest, amount_raw, self._now(), None, self._now()))
            self.db.execute('INSERT INTO settlement_claims VALUES(?,?,?,?,?)', (chain, transaction_id, intent_id, evidence_digest, amount_raw))
            self._confirmed(intent_id, intent, stage, amount_raw)
            if row['terminal'] == 'cancelled' or refund_required:
                # Cancellation is not evidence that an external wallet never
                # sent funds. A late verified observation restores liability.
                self.db.execute('UPDATE settlement_intents SET terminal=NULL WHERE id=?', (intent_id,))
                if stage == 'deposit':
                    self.db.execute("UPDATE settlement_intents SET route_mode='refund' WHERE id=?", (intent_id,))
                    self._obligation(intent_id, 'refund_due', 'USDC', amount_raw)
            self._event('source_verified', intent_id, {'stage': stage, 'chain': chain, 'transaction_id': transaction_id, 'amount_raw': amount_raw, 'evidence_digest': evidence_digest})
        return self.status(intent_id)

    def processing_state(self):
        return json.loads(self.db.execute("SELECT value FROM settlement_metadata WHERE key='paused'").fetchone()[0])

    def _set_paused(self, paused, reason):
        require(type(reason) is str and 1 <= len(reason) <= 256 and reason.isprintable(), 'Reason required')
        with self._atomic():
            state = {'paused': paused, 'reason': reason}
            self.db.execute("UPDATE settlement_metadata SET value=? WHERE key='paused'", (canonical(state),))
            self._event('processing_paused' if paused else 'processing_resumed', None, state)
        return {**state, 'can_accept_funds': False}

    def pause(self, reason):
        return self._set_paused(True, reason)

    def resume(self, reason):
        """Worker preparation gate only; NEVER enables public acceptance by itself."""
        return self._set_paused(False, reason)

    def acquire_route(self, intent_id, worker_id, lease_seconds=30):
        identifier(worker_id)
        require(type(lease_seconds) is int and 5 <= lease_seconds <= 300, 'Invalid lease duration')
        with self._atomic():
            row, _ = self._intent(intent_id)
            active = self.db.execute('SELECT * FROM settlement_route WHERE singleton=1').fetchone()
            # A worker can crash after final confirmation commits but before
            # releasing the singleton. Recover that same terminal route so its
            # lease can be released; do not start a new terminal route.
            require(row['terminal'] is None or active['intent_id'] == intent_id, 'Route already terminal')
            require(active['intent_id'] in (None, intent_id), 'Another unresolved route owns shared settlement capacity')
            require(active['intent_id'] is None or active['expires_at'] <= self._now() or active['worker_id'] == worker_id,
                    'Route has an unexpired worker lease')
            fence, expires = active['fence'] + 1, self._now() + lease_seconds
            self.db.execute('UPDATE settlement_route SET intent_id=?,worker_id=?,fence=?,expires_at=? WHERE singleton=1',
                            (intent_id, worker_id, fence, expires))
            lease = {'intent_id': intent_id, 'worker_id': worker_id, 'fence': fence, 'expires_at': expires}
            self._event('route_acquired', intent_id, lease)
        return lease

    def _lease(self, intent_id, lease):
        require(type(lease) is dict and set(lease) == {'intent_id', 'worker_id', 'fence', 'expires_at'}, 'Worker lease required')
        current = dict(self.db.execute('SELECT intent_id,worker_id,fence,expires_at FROM settlement_route WHERE singleton=1').fetchone())
        require(canonical(current) == canonical(lease) and lease['intent_id'] == intent_id and self._now() < lease['expires_at'], 'Stale or expired fencing lease')

    def renew_route(self, intent_id, *, lease, lease_seconds=30):
        require(type(lease_seconds) is int and 5 <= lease_seconds <= 300, 'Invalid lease duration')
        with self._atomic():
            self._lease(intent_id, lease)
            renewed = {**lease, 'expires_at': self._now() + lease_seconds}
            self.db.execute('UPDATE settlement_route SET expires_at=? WHERE singleton=1', (renewed['expires_at'],))
        return renewed

    def release_route(self, intent_id, *, lease):
        with self._atomic():
            self._lease(intent_id, lease)
            row, _ = self._intent(intent_id)
            require(row['terminal'] in ('completed', 'refunded', 'cancelled'), 'Unresolved route cannot be released')
            require(self.db.execute('SELECT 1 FROM settlement_obligations WHERE intent_id=?', (intent_id,)).fetchone() is None, 'Open obligation prevents release')
            self.db.execute('UPDATE settlement_route SET intent_id=NULL,worker_id=NULL,expires_at=0 WHERE singleton=1')
            self._event('route_released', intent_id, {})

    def cancel_unfunded(self, intent_id, reason, *, lease):
        """Explicitly close an unexecuted route; cannot erase any monetary obligation.

        This does NOT prevent a late real deposit. claim_source restores the
        liability for a subsequently verified observation (buy becomes refund).
        Funds outside the signed window need separate operator quarantine.
        """
        require(type(reason) is str and 1 <= len(reason) <= 256 and reason.isprintable(), 'Cancellation reason required')
        with self._atomic():
            self._lease(intent_id, lease)
            row, _ = self._intent(intent_id)
            require(row['terminal'] is None, 'Route already terminal')
            require(self.db.execute('SELECT 1 FROM settlement_obligations WHERE intent_id=?', (intent_id,)).fetchone() is None,
                    'Funded obligation cannot be cancelled')
            require(self.db.execute("SELECT 1 FROM settlement_stages WHERE intent_id=? AND state!='failed'", (intent_id,)).fetchone() is None,
                    'Executed or uncertain stage cannot be cancelled')
            self.db.execute("UPDATE settlement_intents SET terminal='cancelled' WHERE id=?", (intent_id,))
            self._event('unfunded_cancelled', intent_id, {'reason': reason})
        return self.status(intent_id)

    def prepare_stage(self, intent_id, stage, chain, transaction_id, signed_payload_digest, *, lease,
                      signed_payload_base64=None):
        """Commit original signed ID/digest before a submitter may send its bytes."""
        decode58(transaction_id, 64)
        evidence_hash(signed_payload_digest)
        if signed_payload_base64 is not None:
            require(type(signed_payload_base64) is str and len(signed_payload_base64) <= 16384,
                    'Signed payload exceeds bound')
            try:
                payload = base64.b64decode(signed_payload_base64, validate=True)
            except Exception as error:
                raise JournalError('Malformed signed payload') from error
            require(base64.b64encode(payload).decode() == signed_payload_base64 and
                    digest(payload) == signed_payload_digest, 'Signed payload differs from digest')
        with self._atomic():
            self._lease(intent_id, lease)
            row, intent = self._intent(intent_id)
            require(chain == self._expected_chain(stage), 'Wrong stage chain')
            found = self._stage(intent_id, stage)
            if found:
                require(found['chain'] == chain and found['transaction_id'] == transaction_id and found['signed_payload_digest'] == signed_payload_digest,
                        'Original signed transaction is immutable; replacement forbidden')
                if signed_payload_base64 is not None:
                    previous = self.db.execute('SELECT transaction_id,transaction_base64 FROM settlement_payloads WHERE intent_id=? AND stage=?', (intent_id, stage)).fetchone()
                    require(previous is not None and previous['transaction_id'] == transaction_id and
                            previous['transaction_base64'] == signed_payload_base64, 'Original payload is immutable or missing')
                return dict(found)
            require(not self.processing_state()['paused'], 'Processing is paused')
            require(row['terminal'] is None, 'Route already terminal')
            self._previous(row, intent, stage)
            self.db.execute('INSERT INTO settlement_stages VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (intent_id, stage, chain, transaction_id, signed_payload_digest, 'prepared', None, None, self._now(), None, self._now()))
            if signed_payload_base64 is not None:
                self.db.execute('INSERT INTO settlement_payloads VALUES(?,?,?,?)',
                    (intent_id, stage, transaction_id, signed_payload_base64))
            self._event('original_prepared', intent_id, {'stage': stage, 'chain': chain, 'transaction_id': transaction_id, 'signed_payload_digest': signed_payload_digest})
        return dict(self._stage(intent_id, stage))

    def mark_submitted(self, intent_id, stage, transaction_id, *, lease):
        """Commit BEFORE network write. first_submission=False means reconcile, do not send."""
        with self._atomic():
            self._lease(intent_id, lease)
            found = self._stage(intent_id, stage)
            require(found is not None and found['transaction_id'] == transaction_id and found['signed_payload_digest'] is not None,
                    'Signed original must be prepared first')
            first = found['state'] == 'prepared'
            if first:
                require(not self.processing_state()['paused'], 'Processing is paused')
                self.db.execute("UPDATE settlement_stages SET state='submitted',submitted_at=?,updated_at=? WHERE intent_id=? AND stage=?", (self._now(), self._now(), intent_id, stage))
                self._event('submission_may_have_occurred', intent_id, {'stage': stage, 'transaction_id': transaction_id})
        return {**dict(self._stage(intent_id, stage)), 'first_submission': first}

    def record_outcome(self, intent_id, stage, transaction_id, outcome, evidence_digest, amount_raw=None, *, lease):
        """Trusted independently verified receipt. Unknown can resolve; terminal cannot flip."""
        require(outcome in ('confirmed', 'failed', 'unknown'), 'Invalid outcome')
        evidence_hash(evidence_digest)
        require(outcome == 'confirmed' or amount_raw is None, 'Non-confirmed outcome must not claim an effect')
        with self._atomic():
            self._lease(intent_id, lease)
            row, intent = self._intent(intent_id)
            require(outcome != 'confirmed' or stage not in ('deposit', 'burn'), 'Source confirmations must use claim_source for global deduplication')
            found = self._stage(intent_id, stage)
            require(found is not None and found['transaction_id'] == transaction_id, 'Outcome is not for original transaction')
            if found['state'] in ('confirmed', 'failed'):
                require(found['state'] == outcome and found['evidence_digest'] == evidence_digest and found['amount_raw'] == amount_raw,
                        'Terminal outcome is immutable')
                return self.status(intent_id)
            if outcome == 'confirmed':
                self._previous(row, intent, stage)
                self._validate_effect(intent_id, intent, stage, amount_raw)
            self.db.execute('UPDATE settlement_stages SET state=?,evidence_digest=?,amount_raw=?,updated_at=? WHERE intent_id=? AND stage=?',
                            (outcome, evidence_digest, amount_raw, self._now(), intent_id, stage))
            if outcome == 'confirmed':
                self._confirmed(intent_id, intent, stage, amount_raw)
            self._event('outcome_recorded', intent_id, {'stage': stage, 'transaction_id': transaction_id, 'outcome': outcome, 'evidence_digest': evidence_digest, 'amount_raw': amount_raw})
        return self.status(intent_id)

    def mark_refund_due(self, intent_id, reason, *, lease):
        require(type(reason) is str and 1 <= len(reason) <= 256 and reason.isprintable(), 'Refund reason required')
        with self._atomic():
            self._lease(intent_id, lease)
            row, intent = self._intent(intent_id)
            require(intent['action'] == 'buy' and row['terminal'] is None, 'Only incomplete purchases may refund')
            self._amount(intent_id, 'deposit')
            mint = self._stage(intent_id, 'mint')
            require(mint is None or mint['state'] == 'failed', 'Issued or unresolved mint prevents refund')
            self.db.execute("UPDATE settlement_intents SET route_mode='refund' WHERE id=?", (intent_id,))
            self._obligation(intent_id, 'refund_due', 'USDC', intent['amount_raw'])
            self._event('refund_due', intent_id, {'reason': reason})
        return self.status(intent_id)

    def status(self, intent_id):
        if not self.db.in_transaction:
            with self._snapshot():
                return self.status(intent_id)
        row, intent = self._intent(intent_id)
        stages = [dict(v) for v in self.db.execute('SELECT * FROM settlement_stages WHERE intent_id=? ORDER BY prepared_at,stage', (intent_id,))]
        order = list(STAGES[intent['action']]) + ['refund']
        stages.sort(key=lambda v: order.index(v['stage']))
        obligation = self.db.execute('SELECT * FROM settlement_obligations WHERE intent_id=?', (intent_id,)).fetchone()
        if row['terminal']:
            state = row['terminal']
        elif any(v['state'] == 'unknown' for v in stages):
            state = 'manual_review'
        elif any(v['state'] == 'failed' and v['stage'] in self._order(row, intent) for v in stages):
            state = 'manual_review' if obligation else 'failed'
        else:
            state = 'processing' if stages else 'awaiting_funding'
        return {'schema_version': SCHEMA_VERSION, 'id': intent_id, 'client_request_id': intent['client_request_id'],
            'intent': intent, 'status': state, 'route_mode': row['route_mode'], 'stages': stages,
            'obligation': dict(obligation) if obligation else None,
            'processing_paused': self.processing_state()['paused'], 'can_accept_funds': False}

    def obligations(self):
        if not self.db.in_transaction:
            with self._snapshot():
                return self.obligations()
        rows = [dict(v) for v in self.db.execute('SELECT * FROM settlement_obligations ORDER BY intent_id')]
        totals = {}
        for row in rows:
            key = (row['kind'], row['asset'])
            totals[key] = totals.get(key, 0) + int(row['amount_raw'])
        return {'items': rows, 'totals': [{'kind': k, 'asset': a, 'amount_raw': str(v)} for (k, a), v in sorted(totals.items())]}

    def recovery_items(self):
        """Public original IDs only. Reconcile before any resend; no new transaction IDs."""
        if not self.db.in_transaction:
            with self._snapshot():
                return self.recovery_items()
        rows = [dict(v) for v in self.db.execute("SELECT * FROM settlement_stages WHERE state IN ('prepared','submitted','unknown') ORDER BY prepared_at,intent_id,stage")]
        active = dict(self.db.execute('SELECT intent_id,worker_id,fence,expires_at FROM settlement_route WHERE singleton=1').fetchone())
        return {'stages': rows, 'active_route': active, 'obligations': self.obligations(),
                'processing_paused': self.processing_state()['paused'], 'can_accept_funds': False}
