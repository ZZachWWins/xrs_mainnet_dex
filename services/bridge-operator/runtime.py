"""Fenced settlement executor with durable original-transaction recovery.

This module does not contain transports, private key loaders or live defaults.
The HTTP executable stays paused until a separately authorized signer and
broadcaster plus verified observer are explicitly wired. Unknown submission
outcomes are observed by their original ID; no automatic resend or replacement.
"""
import base64
import hashlib
import json
import threading
from journal import (NETWORK, ROUTES, SOLANA, XERIS, JournalError, canonical,
                     decode58, digest, intent_message, raw_amount, require,
                     verify_ed25519, evidence_hash)
from runtime_store import RuntimeStore
from solana_deposit import build_deposit
from solana_verify import b58encode
import xeris_purchase as purchase
import xeris_redemption as redemption
import solana_payout as payout


def decode_wire(value):
    require(type(value) is str and 1 <= len(value) <= 16384, 'Invalid transaction envelope size')
    try:
        data = base64.b64decode(value, validate=True)
    except Exception as error:
        raise JournalError('Invalid transaction base64') from error
    require(base64.b64encode(data).decode() == value and len(data) > 68 and data[0] == 1,
            'One canonical signature transaction is required')
    return data


class SettlementRuntime:
    def __init__(self, journal, observer, *, operator_wallet, signer=None, broadcaster=None,
                 authorization_id=None, worker_id='settlement_worker', clock=None):
        decode58(operator_wallet, 32)
        require(operator_wallet not in purchase.PRODUCERS | {purchase.LP}, 'Isolated route operator required')
        self.journal, self.observer = journal, observer
        self.operator_wallet, self.signer, self.broadcaster = operator_wallet, signer, broadcaster
        self.authorization_id, self.worker_id = authorization_id, worker_id
        self.clock = clock or journal._now
        self.store, self.lock = RuntimeStore(journal), threading.RLock()

    def _intent(self, intent_id):
        status = self.journal.status(intent_id)
        return status, {**status['intent'], 'intent_id': intent_id}

    def _lease(self, intent_id):
        return self.journal.acquire_route(intent_id, self.worker_id, 300)

    def _facts(self, *, operator_fees=3, authority_fees=1, solana_budget=payout.MAX_SOL_DEBIT):
        now = self.clock()
        facts = self.observer.snapshot()
        require(type(facts) is dict and type(facts.get('checked_at')) is int and
                0 <= now - facts['checked_at'] <= 10, 'Stale reserve observation')
        require(facts.get('checkpoint_verified') is True and facts.get('archive_complete') is True,
                'Network identity or archive is incomplete')
        amounts = {}
        for key in ('reserve_usdc_raw', 'xusdc_supply_raw', 'operator_native_raw',
                    'mint_authority_native_raw', 'solana_fee_lamports'):
            value = facts.get(key)
            require(type(value) is str and value.isascii() and value.isdigit() and str(int(value)) == value
                    and 0 <= int(value) <= (1 << 64)-1, 'Invalid reserve/fee facts')
            amounts[key] = int(value)
        obligations = self.journal.obligations()['items']
        liability = sum(int(v['amount_raw']) for v in obligations if v['asset'] == 'USDC')
        require(amounts['reserve_usdc_raw'] >= amounts['xusdc_supply_raw'] + liability,
                'Reserve does not cover issued supply and outstanding obligations')
        require(amounts['operator_native_raw'] >= operator_fees * purchase.BASE_FEE_RAW and
                amounts['mint_authority_native_raw'] >= authority_fees * purchase.BASE_FEE_RAW and
                amounts['solana_fee_lamports'] >= solana_budget,
                'Settlement gas/rent reserves are insufficient')
        return facts

    def _authorized_scope(self):
        require(self.signer is not None and self.broadcaster is not None and
                type(self.authorization_id) is str and self.authorization_id.startswith('settlement-v2-'),
                'New scoped signer authorization required')
        scope = self.signer.readiness()
        require(type(scope) is dict and scope.get('authorized') is True and
                scope.get('policy_id') == self.authorization_id and
                type(scope.get('checked_at')) is int and 0 <= self.clock()-scope['checked_at'] <= 10,
                'Scoped signer authorization is unavailable')
        require(set(scope.get('signers', [])) == {self.operator_wallet, purchase.TREASURY, payout.FUNDING},
                'Signer identities differ from the settlement scope')
        require(raw_amount(scope.get('maximum_usdc_raw')) >= self.journal.policy.maximum_usdc_raw and
                type(scope.get('expires_at')) is int and scope['expires_at'] > self.clock(),
                'Scoped request budget or authorization lifetime exhausted')
        return scope

    def readiness(self):
        result = {'can_accept_funds': False, 'routes': {r: False for r in ROUTES.values()},
                  'checked_at': self.clock()}
        try:
            require(not self.journal.processing_state()['paused'], 'Processing paused')
            scope = self._authorized_scope()
            active_id = self.journal.recovery_items()['active_route']['intent_id']
            reserved = active_id is not None and self.signer.has_reservation(self._intent(active_id)[1])
            require(reserved or (raw_amount(scope.get('remaining_total_usdc_raw')) >= 1 and
                                scope['expires_at'] >= self.clock()+600), 'New reservation budget exhausted')
            require(not any(v['state'] == 'unknown' for v in self.journal.recovery_items()['stages']),
                    'Unresolved submission requires reconciliation')
            self._facts()
            result['can_accept_funds'] = True
            result['routes'][ROUTES['buy']] = result['routes'][ROUTES['redeem']] = True
        except Exception:
            pass
        return result

    def _admit(self, intent, stage=None):
        if self.signer is not None and self.signer.has_reservation(intent):
            require(not self.journal.processing_state()['paused'], 'Processing paused')
            self._authorized_scope()
            if stage is None:
                self._facts()
            else:
                self._facts(operator_fees={'mint':3,'buy':3,'unwrap':2,'deliver':1}.get(stage,0),
                            authority_fees=1 if stage=='mint' else 0,
                            solana_budget=0)  # Exact payout fee/rent is checked in preflight.
            return
        ready = self.readiness()
        require(ready['can_accept_funds'] and ready['routes'].get(intent['route']) is True,
                'Settlement is unavailable')

    def admit_new_request(self):
        active = self.journal.recovery_items()['active_route']['intent_id']
        if active is not None:
            self.expire_unfunded(active)
        require(self.journal.recovery_items()['active_route']['intent_id'] is None,
                'Settlement capacity is reserved by an unfinished request')

    def _validate_plan(self, intent, stage, wrapper, *, allow_expired=False):
        require(type(wrapper) is dict and set(wrapper) == {'prepared', 'context', 'expires_at', 'fee_raw'},
                'Malformed prepared wrapper')
        prepared = wrapper['prepared']
        require(type(prepared) is dict and type(wrapper['context']) is dict and
                type(wrapper['expires_at']) is int and (allow_expired or wrapper['expires_at'] > self.clock()), 'Expired prepared transaction')
        fee = raw_amount(wrapper['fee_raw'])
        if stage == 'deposit':
            expected = build_deposit(intent['source_owner'], intent['amount_raw'], prepared.get('blockhash'),
                                     intent_digest=digest(intent_message({k:v for k,v in intent.items() if k != 'intent_id'})))
            require(canonical(prepared) == canonical(expected), 'Prepared deposit differs from authenticated intent')
            require(wrapper['expires_at'] <= intent['expires_at'] and fee <= 100000,
                    'Deposit expiry or fee ceiling changed')
            signer, chain = intent['source_owner'], SOLANA
        elif stage == 'burn':
            redemption.validate_prepared(prepared)
            require(intent['action'] == 'redeem' and prepared['stage'] == 'burn' and
                    prepared['route_kind'] == 'xusdc' and prepared['route_id'] == intent['intent_id'] and
                    prepared['source_wallet'] == intent['source_owner'] and
                    prepared['solana_recipient'] == intent['destination_owner'] and
                    prepared['fields']['amount_raw'] == intent['amount_raw'], 'Prepared burn differs from authenticated intent')
            require(wrapper['expires_at'] <= intent['expires_at'] and fee == purchase.BASE_FEE_RAW,
                    'Burn expiry or fee changed')
            signer, chain = intent['source_owner'], XERIS
        elif stage in ('mint', 'buy', 'unwrap', 'deliver'):
            purchase.validate_prepared(prepared)
            require(intent['action'] == 'buy' and prepared['route_id'] == intent['intent_id'] and
                    prepared['stage'] == stage and prepared['operator_wallet'] == self.operator_wallet and
                    prepared['recipient'] == intent['destination_owner'] and fee == purchase.BASE_FEE_RAW,
                    'Operator stage differs from purchase intent')
            amount = self.journal._amount(intent['intent_id'], 'buy') if stage in ('unwrap', 'deliver') else intent['amount_raw']
            require(prepared['fields']['amount_raw'] == amount, 'Operator stage changed confirmed amount')
            if stage == 'buy':
                require(prepared['fields']['min_output_raw'] == intent['min_output_raw'], 'Swap minimum changed')
            signer, chain = prepared['signer_public_address'], XERIS
        elif stage in ('payout', 'refund'):
            amount = self.journal._amount(intent['intent_id'], 'burn' if stage == 'payout' else 'deposit')
            owner = intent['destination_owner'] if stage == 'payout' else intent['source_owner']
            payout.review_plan(prepared, {'intent_id': intent['intent_id'], 'amount_raw': amount,
                'destination_owner': owner, 'transaction_index': prepared.get('transaction_index')})
            require(fee <= payout.MAX_SOL_DEBIT, 'Payout fee/rent ceiling exceeded')
            signer, chain = payout.FUNDING, SOLANA
        else:
            raise JournalError('Unsupported public settlement stage')
        wire = decode_wire(prepared['unsigned_transaction_base64'])
        require(wire[1:65] == bytes(64), 'Preparation must be unsigned')
        message = base64.b64decode(prepared['message_base64'], validate=True)
        require(wire[65:] == message and message[0] == 1 and message[4:36] == decode58(signer, 32),
                'Prepared wire signer or message changed')
        if 'message_sha256' in prepared:
            require(prepared['message_sha256'] == digest(message), 'Prepared message hash differs')
        return signer, chain

    def prepare_source(self, intent_id):
        with self.lock:
            status, intent = self._intent(intent_id)
            require(intent['action'] in ('buy', 'redeem') and status['status'] == 'awaiting_funding', 'Source stage is not available')
            self._admit(intent)
            require(self.clock() < intent['expires_at'], 'Source authorization expired')
            lease = self._lease(intent_id)
            stage = 'deposit' if intent['action'] == 'buy' else 'burn'
            wrapper = self.store.get(intent_id, stage)
            if wrapper is None:
                self.signer.reserve_intent(intent, lease)
                wrapper = self.observer.prepare_source(intent)
                self._validate_plan(intent, stage, wrapper)
                self.store.put(intent_id, stage, wrapper, lease)
            signer, chain = self._validate_plan(intent, stage, wrapper)
            prepared = wrapper['prepared']
            return {'client_request_id': intent['client_request_id'],
                'intent_digest': digest(intent_message(status['intent'])), 'chain': chain, 'stage': stage,
                'signer': signer, 'transaction_base64': prepared['unsigned_transaction_base64'],
                'message_base64': prepared['message_base64'],
                'message_sha256': digest(base64.b64decode(prepared['message_base64'])),
                'expires_at': wrapper['expires_at'], 'fee_raw': wrapper['fee_raw'],
                'fee_asset': 'SOL' if chain == SOLANA else 'XRS'}

    def _verify_signed(self, intent, stage, wrapper, encoded, *, allow_expired=False):
        signer, chain = self._validate_plan(intent, stage, wrapper, allow_expired=allow_expired)
        wire = decode_wire(encoded)
        expected = base64.b64decode(wrapper['prepared']['message_base64'], validate=True)
        require(wire[65:] == expected, 'Signed transaction message differs from prepared original')
        signature = b58encode(wire[1:65])
        verify_ed25519(signer, signature, expected, self.journal.openssl)
        return chain, signature, digest(wire)

    def submit_source(self, intent_id, encoded):
        with self.lock:
            status, intent = self._intent(intent_id)
            stage = 'deposit' if intent['action'] == 'buy' else 'burn'
            original = self.store.payload(intent_id, stage)
            if original is not None:
                require(original['transaction_base64'] == encoded, 'Replacement source transaction forbidden')
                return self.status(intent_id)  # Safe acknowledgement only; never a resend.
            recovery = self.store.recovery_source(intent_id)
            if recovery:
                require(recovery['transaction_base64'] == encoded, 'Replacement recovery source forbidden')
                return self.status(intent_id)
            wrapper = self.store.get(intent_id, stage)
            require(wrapper is not None, 'Prepare the source transaction first')
            if self.clock() >= intent['expires_at'] or self.clock() >= wrapper['expires_at'] or status['status'] == 'cancelled':
                chain, signature, _ = self._verify_signed(intent, stage, wrapper, encoded, allow_expired=True)
                self.store.put_recovery_source(intent_id, chain, signature, encoded)
                return self.status(intent_id)  # Observation only, never late broadcasting.
            self._admit(intent)
            require(self.clock() < intent['expires_at'], 'Source authorization expired')
            chain, signature, payload_hash = self._verify_signed(intent, stage, wrapper, encoded)
            require(self.clock() < min(intent['expires_at'], wrapper['expires_at']),
                    'Source expired during verification; use original recovery')
            lease = self._lease(intent_id)
            self.journal.prepare_stage(intent_id, stage, chain, signature, payload_hash,
                lease=lease, signed_payload_base64=encoded)
            self._submit_original(intent_id, stage, lease)
            return self.journal.status(intent_id)

    def expire_unfunded(self, intent_id):
        with self.lock:
            status, intent = self._intent(intent_id)
            if status['status'] != 'awaiting_funding' or self.clock() < intent['expires_at'] or self.store.recovery_source(intent_id):
                return status
            active = self.journal.recovery_items()['active_route']['intent_id']
            if active not in (None, intent_id):
                return status  # Another route owns the fence; never steal it.
            lease = self._lease(intent_id)
            self.journal.cancel_unfunded(intent_id, 'Unsigned request expired; late source evidence remains recoverable', lease=lease)
            self.journal.release_route(intent_id, lease=lease)
            return self.journal.status(intent_id)

    def status(self, intent_id):
        status = self.expire_unfunded(intent_id)
        recovery = self.store.recovery_source(intent_id)
        if recovery and not recovery['resolved']:
            return {**status, 'status': 'manual_review'}
        return status

    def _observe_recovery_source(self, intent_id, status, intent):
        source = self.store.recovery_source(intent_id)
        if not source or source['resolved']:
            return None
        stage = 'deposit' if intent['action'] == 'buy' else 'burn'
        wrapper = self.store.get(intent_id, stage)
        self._verify_signed(intent, stage, wrapper, source['transaction_base64'], allow_expired=True)
        try:
            observed = self.observer.observe(intent, stage, wrapper['prepared'], source['transaction_id'], wrapper['context'])
        except Exception:
            return self.status(intent_id)
        require(type(observed) is dict and observed.get('state') in ('confirmed','pending','failed'), 'Invalid recovery evidence')
        evidence_hash(observed.get('evidence_digest'))
        if observed['state'] == 'confirmed':
            self.journal.claim_source(intent_id, source['chain'], source['transaction_id'], observed['evidence_digest'],
                observed.get('amount_raw'), refund_required=observed.get('refund_required', False))
            self.store.finish_recovery_source(intent_id)
        elif observed['state'] == 'failed':
            self.store.finish_recovery_source(intent_id)
        return self.status(intent_id)

    def _submit_original(self, intent_id, stage, lease):
        original = self.store.payload(intent_id, stage)
        require(original is not None, 'Durable original transaction is missing')
        wrapper = self.store.get(intent_id, stage)
        _, intent = self._intent(intent_id)
        require(wrapper is not None and self.clock() < wrapper['expires_at'], 'Original submission window expired')
        self.observer.validate_before_submission(intent, stage, wrapper['prepared'], wrapper['context'])
        # Atomic marker precedes the one possible network call. A crash between
        # these lines is deliberately ambiguous and only permits reconciliation.
        record = self.journal.mark_submitted(intent_id, stage, original['transaction_id'], lease=lease)
        if not record['first_submission']:
            return
        self.journal._lease(intent_id, lease)
        try:
            self.broadcaster.submit(record['chain'], original['transaction_base64'], original['transaction_id'], lease)
        except Exception:
            self.journal.record_outcome(intent_id, stage, original['transaction_id'], 'unknown',
                digest(b'submission outcome unknown'), lease=lease)

    def step(self, intent_id):
        """One bounded observation/preparation/submission step, safe after restart.

        Observation remains allowed when paused. Expired signed preparations are
        still observed but cannot be replaced. No looping or implicit retries.
        """
        with self.lock:
            status, intent = self._intent(intent_id)
            recovered = self._observe_recovery_source(intent_id, status, intent)
            if recovered is not None:
                return recovered
            active = self.journal.recovery_items()['active_route']
            if status['status'] in ('completed', 'refunded', 'cancelled'):
                if active['intent_id'] == intent_id:
                    self.journal.release_route(intent_id, lease=self._lease(intent_id))
                return status
            if not status['stages']:
                return self.expire_unfunded(intent_id)
            lease = self._lease(intent_id)
            pending = next((s for s in status['stages'] if s['state'] in ('prepared', 'submitted', 'unknown')), None)
            if pending:
                stage = pending['stage']
                wrapper = self.store.get(intent_id, stage)
                require(wrapper is not None and self.store.payload(intent_id, stage) is not None, 'Recovery context/original missing')
                stored = self.store.payload(intent_id, stage)
                chain, signature, hashed = self._verify_signed(intent, stage, wrapper, stored['transaction_base64'], allow_expired=True)
                require(signature == pending['transaction_id'] and chain == pending['chain'] and hashed == pending['signed_payload_digest'],
                        'Durable original integrity failure')
                try:
                    observed = self.observer.observe(intent, stage, wrapper['prepared'], pending['transaction_id'], wrapper['context'])
                except Exception:
                    return self.journal.status(intent_id)  # Never turn missing evidence into failure.
                require(type(observed) is dict and observed.get('state') in ('confirmed', 'pending', 'failed'), 'Invalid observed outcome')
                evidence_hash(observed.get('evidence_digest'))
                if observed['state'] == 'pending':
                    if pending['state'] == 'prepared' and wrapper['expires_at'] > self.clock():
                        self._admit(intent, stage=None if stage in ('deposit','burn') else stage)
                        self._submit_original(intent_id, stage, lease)  # First send, never a resend.
                    elif pending['state'] == 'prepared':
                        self.journal.record_outcome(intent_id, stage, pending['transaction_id'], 'unknown',
                            digest(b'Original signing window expired; observation only'), lease=lease)
                    return self.journal.status(intent_id)
                if observed['state'] == 'confirmed' and stage in ('deposit', 'burn'):
                    self.journal.claim_source(intent_id, pending['chain'], pending['transaction_id'], observed['evidence_digest'],
                        observed.get('amount_raw'), refund_required=observed.get('refund_required', False))
                else:
                    self.journal.record_outcome(intent_id, stage, pending['transaction_id'], observed['state'],
                        observed['evidence_digest'], observed.get('amount_raw'), lease=lease)
                if observed['state'] == 'failed' and stage in ('deposit', 'burn'):
                    self.journal.cancel_unfunded(intent_id, 'Canonical source execution failed with no obligation', lease=lease)
                    self.journal.release_route(intent_id, lease=lease)
                elif observed['state'] == 'failed' and stage == 'mint':
                    self.journal.mark_refund_due(intent_id, 'Canonical mint failed before issuance', lease=lease)
                return self.journal.status(intent_id)
            if status['status'] in ('manual_review', 'failed'):
                return status
            stages = ('deposit', 'refund') if status['route_mode'] == 'refund' else (
                ('deposit', 'mint', 'buy', 'unwrap', 'deliver') if intent['action'] == 'buy' else ('burn', 'payout'))
            done = {s['stage'] for s in status['stages'] if s['state'] == 'confirmed'}
            stage = next((s for s in stages if s not in done), None)
            if stage is None:
                return status
            self._admit(intent, stage=stage)
            wrapper = self.store.get(intent_id, stage)
            if wrapper is None:
                wrapper = self.observer.prepare_operator(intent, stage, status['stages'])
                self._validate_plan(intent, stage, wrapper)
                self.store.put(intent_id, stage, wrapper, lease)
            self._validate_plan(intent, stage, wrapper)
            self.journal._lease(intent_id, lease)
            signed = self.signer.sign(intent, stage, wrapper['prepared'], lease)
            chain, signature, payload_hash = self._verify_signed(intent, stage, wrapper, signed)
            self.journal.prepare_stage(intent_id, stage, chain, signature, payload_hash,
                lease=lease, signed_payload_base64=signed)
            self._submit_original(intent_id, stage, lease)
            return self.journal.status(intent_id)

    def recover(self, limit=100):
        """One active route only; caller schedules bounded subsequent steps."""
        require(type(limit) is int and 1 <= limit <= 100, 'Invalid recovery limit')
        results = []
        # Late source observations never submit and preserve liability even while
        # another route owns the fence. They remain bounded and globally unique.
        for row in self.journal.db.execute('SELECT intent_id FROM settlement_source_recovery WHERE resolved=0 LIMIT ?', (limit,)).fetchall():
            results.append(self.step(row[0]))
        active = self.journal.recovery_items()['active_route']['intent_id']
        if active is not None:
            results.append(self.step(active))
        else:
            for intent_id in self.store.pending_intents(limit):
                status = self.journal.status(intent_id)
                if status['stages']:
                    results.append(self.step(intent_id)); break
                results.append(self.expire_unfunded(intent_id))
        return results
