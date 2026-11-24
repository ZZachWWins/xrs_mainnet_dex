"""Offline lifecycle regressions: real journals, synthetic adapters, no key/RPC I/O."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import journal
import operator_main as operator
import observer_daemon
import runtime_adapters as adapters
import solana_payout as payout
import xeris_purchase as purchase
import test_runtime_adapters as adapter_fixtures


OPERATOR = payout.b58(bytes([10]) * 32)
AUTHORIZATION = 'settlement-v2-lifecycle-test'


class OperatorLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.handlers = {}
        self.events = []
        self.signer_scope = {'authorized': True, 'policy_id': AUTHORIZATION,
            'signers': [OPERATOR, purchase.TREASURY, payout.FUNDING],
            'maximum_usdc_raw': '500000', 'remaining_total_usdc_raw': '1000000', 'remaining_requests': 2}
        self.good_facts = True
        self.on_handle = lambda: self.handlers[signal.SIGTERM]()
        self.on_collect = lambda: None
        self.server_failure = False
        self.server = None
        self.runtime = None
        self.signer = None
        self.broadcaster = None

    def arguments(self, *extra):
        return ['operator_main', '--state-directory', str(self.directory),
            '--operator-wallet', OPERATOR, '--authorization-path',
            str(self.directory / 'nonexistent-authorization.json'),
            '--authorization-id', AUTHORIZATION, *extra]

    def run_operator(self, *extra):
        owner = self
        real_runtime = operator.SettlementRuntime

        class FakeChain:
            def __init__(self, archive):
                self.archive = archive
            def collect(self):
                owner.events.append('collect')
                owner.on_collect()
                return {'chain_id': journal.XERIS}

        class FakeObserver:
            def __init__(self, chain, db, **kwargs):
                self.journal = db
            def snapshot(self):
                owner.events.append('snapshot')
                return {'checked_at': self.journal._now(), 'checkpoint_verified': True,
                    'archive_complete': owner.good_facts, 'reserve_usdc_raw': '1000000000',
                    'xusdc_supply_raw': '0', 'operator_native_raw': '1000000000',
                    'mint_authority_native_raw': '1000000000', 'solana_fee_lamports': '1000000000'}

        class FakeSigner:
            def __init__(self, db, policy, **kwargs):
                self.journal, self.can_operate = db, kwargs['can_operate']
                owner.signer = self
            def readiness(self):
                owner.events.append('signer-readiness')
                return {**owner.signer_scope, 'checked_at': self.journal._now(),
                    'expires_at': self.journal._now() + 3600}
            def has_reservation(self, intent):
                return False
            def sign(self, *args):
                raise AssertionError('No signing is allowed in lifecycle tests')

        class FakeBroadcaster:
            def __init__(self, db, policy, *, policy_id, can_operate):
                owner.assertEqual(policy_id, AUTHORIZATION)
                self.can_operate = can_operate
                owner.broadcaster = self
            def submit(self, *args):
                raise AssertionError('No submission is allowed in lifecycle tests')

        class FakeServer:
            def __init__(self, address, service):
                owner.events.append('server-open')
                if owner.server_failure:
                    raise OSError('Synthetic bind failure')
                self.address, self.service, self.closed = address, service, False
                owner.server = self
            def handle_request(self):
                owner.events.append('handle-request')
                owner.on_handle()
            def server_close(self):
                self.closed = True

        def runtime(*args, **kwargs):
            owner.runtime = real_runtime(*args, **kwargs)
            return owner.runtime

        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            for name, value in [('ChainObserver', FakeChain), ('SettlementObserver', FakeObserver),
                ('LocalScopedSigner', FakeSigner), ('SingleSubmissionBroadcaster', FakeBroadcaster),
                ('SettlementRuntime', runtime), ('LimitedServer', FakeServer), ('handler_for', lambda service: service)]:
                stack.enter_context(patch.object(operator, name, value))
            stack.enter_context(patch.object(operator.signal, 'signal', lambda kind, callback: self.handlers.__setitem__(kind, callback)))
            stack.enter_context(patch.object(sys, 'argv', self.arguments(*extra)))
            stack.enter_context(patch.object(adapters.subprocess, 'run', side_effect=AssertionError('No local key helper')))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            result = operator.main()
        return result, stdout.getvalue()

    def assert_paused_and_unlocked(self):
        with ExitStack() as stack:
            db = journal.Journal(self.directory / 'settlement.sqlite3')
            stack.callback(db.close)
            self.assertTrue(db.processing_state()['paused'])
            descriptor = os.open(self.directory / 'operator.lock', os.O_RDWR)
            stack.callback(os.close, descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_check_resets_prior_resume_and_never_reads_policy_or_chain(self):
        db = journal.Journal(self.directory / 'settlement.sqlite3')
        db.resume('Synthetic prior process')
        db.close()
        result, output = self.run_operator('--check')
        self.assertEqual(result, 0)
        caps = json.loads(output)
        self.assertFalse(caps['can_accept_funds'])
        self.assertTrue(all(not value['enabled'] for value in caps['routes'].values()))
        self.assertEqual(self.events, [])
        self.assertFalse((self.directory / 'nonexistent-authorization.json').exists())
        self.assert_paused_and_unlocked()

    def test_default_server_is_paused_even_with_authorized_adapter(self):
        def handle():
            self.assertEqual(self.server.address, ('127.0.0.1', 8791))
            self.assertFalse(self.server.service.capabilities()['can_accept_funds'])
            self.handlers[signal.SIGTERM]()
        self.on_handle = handle
        self.assertEqual(self.run_operator()[0], 0)
        self.assertNotIn('signer-readiness', self.events)
        self.assertNotIn('collect', self.events)
        self.assertTrue(self.server.closed)
        self.assert_paused_and_unlocked()

    def test_disabled_or_wrong_authorization_never_opens_server(self):
        for scope in ({'authorized': False}, {'policy_id': 'settlement-v2-wrong'}):
            with self.subTest(scope=scope):
                baseline = dict(self.signer_scope)
                self.signer_scope.update(scope)
                with self.assertRaisesRegex(ValueError, 'authorization is unavailable'):
                    self.run_operator('--enable-processing')
                self.assertNotIn('server-open', self.events)
                self.assert_paused_and_unlocked()
                self.signer_scope = baseline

    def test_wrong_signer_identity_or_incomplete_archive_repauses_before_bind(self):
        for wrong_identity in (True, False):
            with self.subTest(wrong_identity=wrong_identity):
                self.signer_scope['signers'] = [purchase.TREASURY] if wrong_identity else [OPERATOR, purchase.TREASURY, payout.FUNDING]
                self.good_facts = wrong_identity
                with self.assertRaisesRegex(ValueError, 'readiness is incomplete'):
                    self.run_operator('--enable-processing')
                self.assertNotIn('server-open', self.events)
                self.assert_paused_and_unlocked()

    def test_explicit_activation_enables_only_buy_redeem_and_shutdown_revokes_callbacks(self):
        def handle():
            caps = self.server.service.capabilities()
            self.assertTrue(caps['can_accept_funds'])
            self.assertTrue(caps['routes'][journal.ROUTES['buy']]['enabled'])
            self.assertTrue(caps['routes'][journal.ROUTES['redeem']]['enabled'])
            self.assertFalse(caps['routes'][journal.ROUTES['sell']]['enabled'])
            self.assertTrue(self.signer.can_operate())
            self.assertTrue(self.broadcaster.can_operate())
            self.handlers[signal.SIGTERM]()
        self.on_handle = handle
        self.run_operator('--enable-processing')
        self.assertNotIn('collect', self.events)
        self.assertFalse(self.signer.can_operate())
        self.assertFalse(self.broadcaster.can_operate())
        self.assert_paused_and_unlocked()

    def test_stop_during_collection_never_expires_or_steps_obligations(self):
        def handle():
            self.runtime.journal.recovery_items = lambda: {'active_route': {'intent_id': 'synthetic-pending'}}
            self.runtime.expire_unfunded = Mock()
            self.runtime.step = Mock()
        self.on_handle = handle
        self.on_collect = lambda: self.handlers[signal.SIGINT]()
        self.run_operator('--enable-processing')
        self.assertEqual(self.events.count('collect'), 1)
        self.runtime.expire_unfunded.assert_not_called()
        self.runtime.step.assert_not_called()
        self.assert_paused_and_unlocked()

    def test_bind_failure_after_resume_still_persists_pause_and_releases_lock(self):
        self.server_failure = True
        with self.assertRaisesRegex(OSError, 'Synthetic bind failure'):
            self.run_operator('--enable-processing')
        self.assert_paused_and_unlocked()

    def test_process_lock_rejects_second_owner_before_opening_databases(self):
        descriptor = os.open(self.directory / 'operator.lock', os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaises(SystemExit):
            self.run_operator('--check')
        self.assertEqual(self.events, [])
        self.assertFalse((self.directory / 'settlement.sqlite3').exists())

    def test_check_cannot_enable_processing_and_insecure_state_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.run_operator('--check', '--enable-processing')
        self.directory.chmod(0o755)
        with self.assertRaises(SystemExit):
            self.run_operator('--check')
        self.directory.chmod(0o700)
        self.assertFalse((self.directory / 'settlement.sqlite3').exists())


class BoundaryStopTests(unittest.TestCase):
    def fixture(self):
        case = adapter_fixtures.AdapterTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        return case

    def test_stop_between_plan_validation_and_helper_prevents_signing(self):
        case = self.fixture()
        case.signer.can_operate = Mock(side_effect=[True, False])
        with patch.object(adapters.subprocess, 'run') as helper:
            with self.assertRaisesRegex(ValueError, 'stopping'):
                case.signer.sign(case.intent, 'mint', case.plan, case.lease)
            helper.assert_not_called()
        self.assertEqual(case.journal.db.execute('SELECT COUNT(*) FROM settlement_signer_records').fetchone()[0], 0)

    def test_stop_after_attempt_reservation_never_sends_or_allows_restart_resend(self):
        case = self.fixture()
        wire, transaction_id = case.prepare_submitted()
        transport = Mock()
        broadcaster = adapters.SingleSubmissionBroadcaster(case.journal, case.scope, transport,
            policy_id=case.policy['policy_id'], can_operate=Mock(side_effect=[True, False]))
        with self.assertRaisesRegex(ValueError, 'stopping'):
            broadcaster.submit(journal.XERIS, wire, transaction_id, case.lease)
        transport.assert_not_called()
        self.assertEqual(case.journal.db.execute('SELECT COUNT(*) FROM settlement_network_attempts').fetchone()[0], 1)
        restarted = adapters.SingleSubmissionBroadcaster(case.journal, case.scope, transport,
            policy_id=case.policy['policy_id'])
        with self.assertRaisesRegex(ValueError, 'already reserved'):
            restarted.submit(journal.XERIS, wire, transaction_id, case.lease)
        transport.assert_not_called()


class ReadOnlyObserverLifecycleTests(unittest.TestCase):
    def test_once_records_public_unavailable_health_on_observer_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            fake = SimpleNamespace(collect=Mock(side_effect=ValueError('synthetic unavailable')))
            output = io.StringIO()
            with patch.object(sys, 'argv', ['observer_daemon', '--state-directory', str(directory), '--once']), \
                    patch.object(observer_daemon, 'ChainObserver', return_value=fake), \
                    patch.object(observer_daemon.signal, 'signal'), redirect_stdout(output), \
                    patch.object(adapters.subprocess, 'run', side_effect=AssertionError('No key helper')):
                self.assertEqual(observer_daemon.main(), 1)
            health = json.loads(output.getvalue())
            self.assertEqual(health['mode'], 'read_only')
            self.assertFalse(health['available'])
            self.assertEqual(health['error'], 'observation_incomplete')
            self.assertEqual(json.loads((directory / 'observer-health.json').read_text()), health)
            self.assertFalse((directory / 'settlement.sqlite3').exists())

    def test_health_write_rejects_symlink_temporary_and_preserves_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            target = directory / 'public-sentinel.txt'
            target.write_text('unchanged public fixture')
            path = directory / 'observer-health.json'
            path.with_suffix('.pending').symlink_to(target)
            with self.assertRaises(OSError):
                observer_daemon.write_health(path, {'available': False})
            self.assertEqual(target.read_text(), 'unchanged public fixture')


if __name__ == '__main__':
    unittest.main()
