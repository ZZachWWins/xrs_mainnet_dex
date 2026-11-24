"""Offline executor failure/restart tests; synthetic public signatures only.

Signature math is mocked here (separately tested by journal public fixtures).
The state machine, exact unsigned wire and all persistent byte comparisons run
unchanged. No real keys, RPC, signer processes, journal paths or sockets.
"""
import base64
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import journal as j
import runtime as r
import xeris_purchase as p
import xeris_redemption as red
import solana_verify as sv
import solana_payout as pay
from solana_deposit import build_deposit
from payout_builder import build_payout

OWNER = sv.b58encode(bytes([3])*32)
RECIPIENT = sv.b58encode(bytes([4])*32)
OPERATOR = sv.b58encode(bytes([7])*32)
SIGNATURE = sv.b58encode(bytes([6])*64)
BLOCK = sv.b58encode(bytes([8])*32)
POLICY = 'settlement-v2-offline-fixture'


def signed(prepared):
    message = base64.b64decode(prepared['message_base64'])
    return base64.b64encode(b'\1'+hashlib.sha512(message).digest()+message).decode()


class Observer:
    def __init__(self, case):
        self.case, self.reserve, self.supply = case, 10_000_000, 10_000_000
        self.applied, self.pending, self.fail = set(), set(), set()
        self.corrupt, self.late = False, False
        self.reads = 0
        self.operator_native,self.authority_native=10_000_000,10_000_000
    def snapshot(self):
        return {'checked_at':self.case.now,'checkpoint_verified':True,'archive_complete':True,
            'reserve_usdc_raw':str(self.reserve),'xusdc_supply_raw':str(self.supply),
            'operator_native_raw':str(self.operator_native),'mint_authority_native_raw':str(self.authority_native),'solana_fee_lamports':'30000000'}
    def prepare_source(self, intent):
        if intent['action']=='buy':
            prepared=build_deposit(intent['source_owner'],intent['amount_raw'],BLOCK,
                intent_digest=j.digest(j.intent_message({k:v for k,v in intent.items() if k!='intent_id'})))
            fee='5000'
        else:
            prepared=red.build_stage('burn',intent['source_owner'],intent['destination_owner'],bytes([8])*32,
                int(intent['amount_raw']),route_id=intent['intent_id'],route_kind='xusdc');fee='1000000'
        if self.corrupt:
            prepared=copy.deepcopy(prepared);prepared['message_base64']=base64.b64encode(b'bad').decode()
        return {'prepared':prepared,'context':{'before':{'slot':5}},'expires_at':intent['expires_at'],'fee_raw':fee}
    def prepare_operator(self,intent,stage,stages):
        confirmed={s['stage']:s['amount_raw'] for s in stages if s['state']=='confirmed'}
        if stage in ('payout','refund'):
            amount=confirmed['burn' if stage=='payout' else 'deposit']
            owner=intent['destination_owner'] if stage=='payout' else intent['source_owner']
            prepared=build_payout(owner,amount,6,BLOCK);fee='5000000'
        else:
            amount=confirmed['buy'] if stage in ('unwrap','deliver') else intent['amount_raw']
            prepared=p.build_stage(stage,OPERATOR,intent['destination_owner'],bytes([8])*32,int(amount),
                int(intent['min_output_raw']) if stage=='buy' else None,route_id=intent['intent_id']);fee='1000000'
        return {'prepared':prepared,'context':{'before':{'slot':5}},'expires_at':self.case.now+60,'fee_raw':fee}
    def validate_before_submission(self,*args): return True
    def observe(self,intent,stage,prepared,txid,context):
        self.reads+=1
        if stage in self.pending:return {'state':'pending','evidence_digest':'ab'*32}
        if stage in self.fail:return {'state':'failed','evidence_digest':'ac'*32}
        amount=33383761746 if stage=='buy' else int(prepared.get('amount_raw') or prepared.get('fields',{}).get('amount_raw') or intent['amount_raw'])
        if txid not in self.applied:
            self.applied.add(txid)
            if stage=='deposit':self.reserve+=amount
            if stage=='mint':self.supply+=amount
            if stage=='mint':self.authority_native-=p.BASE_FEE_RAW
            if stage in ('buy','unwrap','deliver'):self.operator_native-=p.BASE_FEE_RAW
            if stage=='unwrap':self.operator_native+=amount
            if stage=='deliver':self.operator_native-=amount
            if stage=='burn':self.supply-=amount
            if stage in ('payout','refund'):self.reserve-=amount
        return {'state':'confirmed','amount_raw':str(amount),'evidence_digest':j.digest((stage+txid).encode()),
                'refund_required':self.late and stage=='deposit'}


class Signer:
    def __init__(self, case):self.case=case;self.calls=[];self.reservations={};self.remaining=5000000;self.remaining_requests=2
    def readiness(self):
        return {'authorized':True,'checked_at':self.case.now,'policy_id':POLICY,'signers':[OPERATOR,p.TREASURY,pay.FUNDING],
            'maximum_usdc_raw':'500000','remaining_total_usdc_raw':str(self.remaining),
            'remaining_requests':self.remaining_requests,'expires_at':self.case.now+3600}
    def has_reservation(self,intent):return intent['intent_id'] in self.reservations
    def reserve_intent(self,intent,lease):
        if intent['intent_id'] not in self.reservations:
            j.require(type(self.remaining_requests) is int and self.remaining_requests>0,'Scope request count exhausted')
            amount=int(intent['amount_raw']);j.require(self.remaining>=amount,'Scope budget exhausted')
            self.remaining-=amount;self.remaining_requests-=1;self.reservations[intent['intent_id']]=copy.deepcopy(intent)
        else:j.require(self.reservations[intent['intent_id']]==intent,'Reservation conflict')
    def sign(self,intent,stage,prepared,lease):
        self.case.db._lease(intent['intent_id'],lease)
        self.calls.append(stage);return signed(prepared)


class Broadcaster:
    def __init__(self,case):self.case=case;self.calls=[];self.lose=set()
    def submit(self,chain,encoded,txid,lease):
        self.case.db._lease(lease['intent_id'],lease)
        stage=self.case.db.db.execute('SELECT * FROM settlement_stages WHERE transaction_id=?',(txid,)).fetchone()
        payload=self.case.runtime.store.payload(lease['intent_id'],stage['stage'])
        self.case.assertEqual(stage['state'],'submitted')
        self.case.assertEqual(payload['transaction_base64'],encoded)
        self.calls.append(stage['stage'])
        if stage['stage'] in self.lose:raise TimeoutError('synthetic lost response')


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'isolated-settlement.sqlite3';self.now=1800000000
        self.db=j.Journal(self.path,clock=lambda:self.now);self.db.resume('Offline synthetic test')
        self.journal_verify=patch.object(j,'verify_ed25519',return_value=None);self.journal_verify.start()
        self.runtime_verify=patch.object(r,'verify_ed25519',return_value=None);self.verify=self.runtime_verify.start()
        self.observer,self.signer,self.broadcaster=Observer(self),Signer(self),Broadcaster(self)
        self.make_runtime()
    def make_runtime(self):
        self.runtime=r.SettlementRuntime(self.db,self.observer,operator_wallet=OPERATOR,signer=self.signer,
            broadcaster=self.broadcaster,authorization_id=POLICY,clock=lambda:self.now)
    def tearDown(self):
        self.journal_verify.stop();self.runtime_verify.stop();self.db.close();self.tmp.cleanup()
    def accepted(self,action='buy',client='client-1'):
        intent=self.db.issue_challenge(action=action,source_owner=OWNER,destination_owner=RECIPIENT,
            solana_token_account=sv.associated_token_address(OWNER if action=='buy' else RECIPIENT),
            amount_raw='500000',min_output_raw='33000000000' if action=='buy' else '500000',
            quote_id='quote-'+client,client_request_id=client,expires_at=self.now+60)['intent']
        return self.db.accept_intent(intent,SIGNATURE)['id']
    def start(self,action='buy'):
        identifier=self.accepted(action);self.runtime.prepare_source(identifier)
        stage='deposit' if action=='buy' else 'burn'
        wire=signed(self.runtime.store.get(identifier,stage)['prepared'])
        return identifier,wire
    def finish(self,identifier):
        for _ in range(15):
            state=self.runtime.step(identifier)
            if state['status'] in ('completed','refunded'):self.runtime.step(identifier);return state
        self.fail('Bounded route did not settle')
    def test_buy_full_route_uses_verified_output_and_clears_obligation(self):
        identifier,wire=self.start();self.runtime.submit_source(identifier,wire)
        result=self.finish(identifier)
        self.assertEqual(result['status'],'completed');self.assertIsNone(result['obligation'])
        self.assertEqual(self.broadcaster.calls,['deposit','mint','buy','unwrap','deliver'])
        self.assertEqual(self.runtime.store.get(identifier,'deliver')['prepared']['fields']['amount_raw'],'33383761746')
        self.assertEqual(self.db.recovery_items()['active_route']['intent_id'],None)
    def test_redeem_burn_then_exact_payout(self):
        identifier,wire=self.start('redeem');self.runtime.submit_source(identifier,wire)
        result=self.finish(identifier)
        self.assertEqual(result['status'],'completed');self.assertEqual(self.broadcaster.calls,['burn','payout'])
        self.assertEqual(self.runtime.store.get(identifier,'payout')['prepared']['amount_raw'],'500000')
    def test_exact_initial_gas_allowance_finishes_without_requiring_spent_fees_again(self):
        self.observer.operator_native=3*p.BASE_FEE_RAW;self.observer.authority_native=p.BASE_FEE_RAW
        identifier,wire=self.start();self.runtime.submit_source(identifier,wire);self.finish(identifier)
        self.assertEqual(self.observer.operator_native,0);self.assertEqual(self.observer.authority_native,0)
    def test_lost_source_response_retry_and_restart_never_resend(self):
        identifier,wire=self.start();self.broadcaster.lose.add('deposit')
        self.assertEqual(self.runtime.submit_source(identifier,wire)['status'],'manual_review')
        self.db.close();self.db=j.Journal(self.path,clock=lambda:self.now);self.make_runtime()
        self.runtime.submit_source(identifier,wire);self.assertEqual(self.broadcaster.calls,['deposit'])
        self.finish(identifier);self.assertEqual(self.broadcaster.calls.count('deposit'),1)
    def test_lost_operator_response_is_observed_before_downstream(self):
        identifier,wire=self.start();self.broadcaster.lose.add('buy');self.runtime.submit_source(identifier,wire)
        self.finish(identifier);self.assertEqual(self.signer.calls.count('buy'),1);self.assertEqual(self.broadcaster.calls.count('buy'),1)
    def test_unknown_pending_blocks_downstream_and_other_route(self):
        identifier,wire=self.start();self.broadcaster.lose.add('deposit');self.observer.pending.add('deposit')
        self.runtime.submit_source(identifier,wire)
        for _ in range(4):self.runtime.step(identifier)
        self.assertEqual(self.signer.calls,[]);self.assertEqual(self.broadcaster.calls,['deposit'])
        with self.assertRaises(ValueError):self.runtime.prepare_source(self.accepted(client='other'))
    def test_mutated_signed_wire_rejected_without_debit_or_submission(self):
        identifier,wire=self.start();bad=bytearray(base64.b64decode(wire));bad[-1]^=1
        with self.assertRaises(ValueError):self.runtime.submit_source(identifier,base64.b64encode(bad).decode())
        self.assertEqual(self.broadcaster.calls,[]);self.assertEqual(self.db.status(identifier)['stages'],[])
    def test_signature_verification_failure_does_not_prepare_stage(self):
        identifier,wire=self.start();self.verify.side_effect=j.JournalError('bad public signature')
        with self.assertRaises(ValueError):self.runtime.submit_source(identifier,wire)
        self.assertEqual(self.db.status(identifier)['stages'],[])
    def test_unsigned_expiry_releases_capacity_but_keeps_binding(self):
        identifier,wire=self.start();self.now+=61
        self.assertEqual(self.runtime.status(identifier)['status'],'cancelled')
        self.assertIsNotNone(self.runtime.store.get(identifier,'deposit'))
        self.assertIsNone(self.db.recovery_items()['active_route']['intent_id'])
        self.runtime.admit_new_request()
    def test_expired_signed_original_recovery_never_broadcasts_and_restores_refund(self):
        identifier,wire=self.start();self.now+=61;self.runtime.status(identifier)
        result=self.runtime.submit_source(identifier,wire);self.assertEqual(result['status'],'manual_review')
        self.assertEqual(self.broadcaster.calls,[])
        result=self.runtime.step(identifier);self.assertEqual(result['route_mode'],'refund');self.assertEqual(result['obligation']['kind'],'refund_due')
        self.finish(identifier);self.assertEqual(self.broadcaster.calls,['refund'])
    def test_late_source_batch_observes_while_paused_without_touching_another_route(self):
        identifier,wire=self.start();self.now+=61;self.runtime.status(identifier)
        self.runtime.submit_source(identifier,wire)
        active=self.accepted(client='other-active');self.runtime.prepare_source(active)
        self.db.pause('Read-only recovery review')
        with patch.object(self.runtime,'step',side_effect=AssertionError('No execution step')):
            result=self.runtime.observe_late_sources(limit=1)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['obligation']['kind'],'refund_due')
        self.assertTrue(self.runtime.store.recovery_source(identifier)['resolved'])
        self.assertEqual(self.db.recovery_items()['active_route']['intent_id'],active)
        self.assertEqual(self.signer.calls,[]);self.assertEqual(self.broadcaster.calls,[])
    def test_late_source_batch_rotates_pending_receipts_and_honors_stop(self):
        identifiers=[]
        for i in range(2):
            identifier=self.accepted(client='pending-'+str(i));self.runtime.prepare_source(identifier)
            wire=signed(self.runtime.store.get(identifier,'deposit')['prepared'])
            self.now+=61;self.runtime.status(identifier);self.runtime.submit_source(identifier,wire)
            identifiers.append(identifier)
        self.db.pause('Read-only recovery review');self.observer.pending.add('deposit')
        with patch.object(self.observer,'observe',wraps=self.observer.observe) as observe:
            self.assertEqual(self.runtime.observe_late_sources(limit=1,can_continue=lambda:False),[])
            observe.assert_not_called()
            for _ in range(3):self.runtime.observe_late_sources(limit=1)
            observed=[call.args[0]['intent_id'] for call in observe.call_args_list]
        self.assertEqual(observed,sorted(identifiers)+[min(identifiers)])
        self.assertTrue(all(not self.runtime.store.recovery_source(v)['resolved'] for v in identifiers))
        self.assertEqual(self.signer.calls,[]);self.assertEqual(self.broadcaster.calls,[])
        for invalid in (0,101,True):
            with self.assertRaises(ValueError):self.runtime.observe_late_sources(limit=invalid)
    def test_late_onchain_deposit_goes_only_to_refund(self):
        identifier,wire=self.start();self.runtime.submit_source(identifier,wire);self.observer.late=True
        result=self.finish(identifier);self.assertEqual(result['status'],'refunded');self.assertEqual(self.broadcaster.calls,['deposit','refund'])
    def test_failed_source_cancels_only_after_observer_proves_failure(self):
        identifier,wire=self.start('redeem');self.observer.fail.add('burn');self.runtime.submit_source(identifier,wire)
        result=self.runtime.step(identifier);self.assertEqual(result['status'],'cancelled');self.assertIsNone(result['obligation'])
    def test_failed_mint_refunds_without_another_mint(self):
        identifier,wire=self.start();self.observer.fail.add('mint');self.runtime.submit_source(identifier,wire)
        result=self.finish(identifier);self.assertEqual(result['status'],'refunded');self.assertEqual(self.broadcaster.calls,['deposit','mint','refund'])
    def test_failed_buy_retains_funded_obligation_and_stops(self):
        identifier,wire=self.start();self.observer.fail.add('buy');self.runtime.submit_source(identifier,wire)
        for _ in range(10):result=self.runtime.step(identifier)
        self.assertEqual(result['status'],'manual_review');self.assertEqual(result['obligation']['kind'],'purchase_settlement')
        self.assertEqual(self.broadcaster.calls,['deposit','mint','buy'])
    def test_pausing_stops_new_writes_but_still_observes_arrived_funds(self):
        identifier,wire=self.start();self.runtime.submit_source(identifier,wire);self.db.pause('Offline pause')
        self.assertEqual(self.runtime.step(identifier)['obligation']['kind'],'pending_issuance')
        with self.assertRaises(ValueError):self.runtime.step(identifier)
        self.assertEqual(self.broadcaster.calls,['deposit'])
    def test_reserve_deficit_and_bad_preparation_fail_before_source(self):
        identifier=self.accepted();self.observer.reserve-=1
        self.assertFalse(self.runtime.readiness()['can_accept_funds'])
        with self.assertRaises(ValueError):self.runtime.prepare_source(identifier)
        self.observer.reserve+=1;self.observer.corrupt=True
        with self.assertRaises(ValueError):self.runtime.prepare_source(identifier)
        self.assertEqual(self.broadcaster.calls,[])
    def test_entire_budget_reserved_by_current_intent_can_finish(self):
        self.signer.remaining=500000
        identifier,wire=self.start();self.assertEqual(self.signer.remaining,0)
        self.runtime.submit_source(identifier,wire);self.finish(identifier)
        self.assertFalse(self.runtime.readiness()['can_accept_funds'])
    def test_last_request_slot_can_finish_both_routes_but_cannot_admit_another(self):
        for action in ('buy','redeem'):
            with self.subTest(action=action):
                self.signer.remaining_requests=1
                identifier=self.accepted(action,client='last-slot-'+action)
                self.runtime.prepare_source(identifier)
                self.assertEqual(self.signer.remaining_requests,0)
                self.assertGreater(self.signer.remaining,0)
                self.assertTrue(self.runtime.readiness()['can_accept_funds'])
                stage='deposit' if action=='buy' else 'burn'
                wire=signed(self.runtime.store.get(identifier,stage)['prepared'])
                self.make_runtime()
                self.runtime.submit_source(identifier,wire)
                self.assertEqual(self.finish(identifier)['status'],'completed')
                self.assertFalse(self.runtime.readiness()['can_accept_funds'])
                with self.assertRaisesRegex(ValueError,'New reservation budget exhausted'):
                    self.runtime.admit_new_request()
    def test_new_admission_rechecks_count_after_expiring_last_reserved_request(self):
        self.signer.remaining_requests=1
        identifier,wire=self.start()
        self.assertTrue(self.runtime.readiness()['can_accept_funds'])
        self.now+=61
        with self.assertRaisesRegex(ValueError,'New reservation budget exhausted'):
            self.runtime.admit_new_request()
        self.assertEqual(self.db.status(identifier)['status'],'cancelled')
        self.assertIsNone(self.db.recovery_items()['active_route']['intent_id'])
        self.assertEqual(self.signer.remaining_requests,0)
        self.assertGreater(self.signer.remaining,0)
        self.assertEqual(self.broadcaster.calls,[])
    def test_missing_or_malformed_request_capacity_never_admits_new_intents(self):
        baseline=self.signer.readiness()
        for value in (None,-1,True,1.0,'2',1001):
            with self.subTest(remaining_requests=value):
                scope={**baseline,'remaining_requests':value}
                if value is None:del scope['remaining_requests']
                with patch.object(self.signer,'readiness',return_value=scope):
                    self.assertFalse(self.runtime.readiness()['can_accept_funds'])
                    with self.assertRaisesRegex(ValueError,'request capacity'):
                        self.runtime.admit_new_request()
    def test_replacement_signature_after_commit_is_rejected(self):
        identifier,wire=self.start();self.runtime.submit_source(identifier,wire)
        bad=bytearray(base64.b64decode(wire));bad[1]^=1
        with self.assertRaises(ValueError):self.runtime.submit_source(identifier,base64.b64encode(bad).decode())
        self.assertEqual(self.broadcaster.calls,['deposit'])
    def test_missing_observer_data_never_confirms_or_fails(self):
        identifier,wire=self.start();self.runtime.submit_source(identifier,wire)
        with patch.object(self.observer,'observe',side_effect=TimeoutError):
            result=self.runtime.step(identifier)
        self.assertEqual(result['stages'][0]['state'],'submitted');self.assertEqual(self.signer.calls,[])
    def test_signed_payload_and_stage_commit_are_atomic(self):
        identifier,wire=self.start();self.db.db.execute("CREATE TEMP TRIGGER reject_payload BEFORE INSERT ON settlement_payloads BEGIN SELECT RAISE(ABORT,'crash'); END")
        with self.assertRaises(Exception):self.runtime.submit_source(identifier,wire)
        self.assertEqual(self.db.status(identifier)['stages'],[]);self.assertIsNone(self.runtime.store.payload(identifier,'deposit'));self.assertEqual(self.broadcaster.calls,[])
    def test_restart_after_preparation_sends_original_only_once(self):
        identifier,wire=self.start();wrapper=self.runtime.store.get(identifier,'deposit');_,intent=self.runtime._intent(identifier)
        chain,signature,hashed=self.runtime._verify_signed(intent,'deposit',wrapper,wire)
        self.db.prepare_stage(identifier,'deposit',chain,signature,hashed,lease=self.runtime._lease(identifier),signed_payload_base64=wire)
        self.observer.pending.add('deposit');self.make_runtime();self.runtime.step(identifier);self.runtime.step(identifier)
        self.assertEqual(self.broadcaster.calls,['deposit'])


if __name__=='__main__':unittest.main()
