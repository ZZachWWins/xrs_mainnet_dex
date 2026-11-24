"""Integration fixtures for the real read-only adapter; no network or key files.

The ownership signature verifier alone is mocked to accept synthetic intents.
Actual receipt, message, amount, Merkle and accounting validators are exercised.
"""
import base64
import copy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import journal as j
import settlement_observer as o
import solana_verify as sv
import solana_payout as pay
import xeris_purchase as p
import xeris_redemption as red
from payout_builder import build_payout
from solana_deposit import build_deposit
import test_solana_verify as sf
import test_solana_payout as pf
import test_xeris_purchase as xf
import test_xeris_accounting as af


class Reader:
    def __init__(self):self.responses={};self.calls=[]
    def solana(self, method, params=None):
        self.calls.append((method,copy.deepcopy(params)))
        if method=='getGenesisHash':return sv.SOLANA_GENESIS
        if method not in self.responses:raise AssertionError('Unexpected read '+method)
        value=self.responses[method]
        return copy.deepcopy(value(params) if callable(value) else value)
    def xeris(self, method, params=None, node=None):
        self.calls.append((method,params,node))
        if method=='getBalance':return {'value':1000000}
        if method=='getLatestBlockhash':return {'context':{'slot':100},'value':{'blockhash':xf.BLOCKHASH.hex(),'lastValidBlockHeight':250}}
        raise AssertionError('Unexpected Xeris read '+method)


class Chain:
    def __init__(self):
        self.reader=Reader();self.state=xf.snapshot(113);self.proof=None;self.target_only=None
        self.archive=self
    def snapshot_wallet(self, wallet):
        result=copy.deepcopy(self.state);result['wallet']=wallet;return result
    def collect(self):pass
    def anchor(self):return copy.deepcopy(self.state)
    def evidence(self, before, after, prepared, signature, target_only=False):
        self.target_only=target_only;return copy.deepcopy(self.proof)
    def get(self, slot):raise ValueError('Synthetic archive gap')


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.now=sf.NOW-90
        self.db=j.Journal(Path(self.tmp.name)/'observer-isolated.sqlite3',clock=lambda:self.now)
        self.db.resume('Offline fixtures only')
        self.proof_patch=patch.object(j,'verify_ed25519',return_value=None);self.proof_patch.start()
        self.chain=Chain();self.observer=o.SettlementObserver(self.chain,self.db,operator_wallet=xf.OPERATOR,clock=lambda:self.now)
    def tearDown(self):
        self.proof_patch.stop();self.db.close();self.tmp.cleanup()
    def intent(self, action='buy', source=sf.OWNER, destination=xf.RECIPIENT):
        value=self.db.issue_challenge(action=action,source_owner=source,destination_owner=destination,
            solana_token_account=sv.associated_token_address(source if action=='buy' else destination),
            amount_raw='500000',min_output_raw='33216842938' if action=='buy' else '500000',
            client_request_id='client-'+action,quote_id='quote-'+action,expires_at=sf.NOW+300)['intent']
        identifier=self.db.accept_intent(value,sf.SIGNATURE)['id'];self.now=sf.NOW
        return {**value,'intent_id':identifier}
    def deposit(self):
        intent=self.intent();request,evidence=sf.bound_fixture()
        prepared=build_deposit(intent['source_owner'],intent['amount_raw'],sf.BLOCKHASH,
            intent_digest=j.digest(j.intent_message({k:v for k,v in intent.items() if k!='intent_id'})))
        evidence['transaction']['transaction']['message']['instructions'][1]['data']=sv.b58encode((sv.MEMO_PREFIX+prepared['intent_digest']).encode())
        self.chain.reader.responses.update(getTransaction=evidence['transaction'],getSignatureStatuses=evidence['signature_status'])
        return intent,prepared,{'min_slot':sf.SLOT-10}
    def observe_deposit(self, args):return self.observer.observe(args[0],'deposit',args[1],sf.SIGNATURE,args[2])
    def test_exact_two_instruction_deposit_memo_and_decimal_evidence_are_durable(self):
        args=self.deposit()
        receipt=self.chain.reader.responses['getTransaction']
        receipt['meta']['postTokenBalances'][0]['uiTokenAmount']['uiAmount']=Decimal('3.5000000000000000001')
        self.assertEqual(o.compiled_message_bytes(receipt['transaction']['message']),base64.b64decode(args[1]['message_base64']))
        result=self.observe_deposit(args)
        self.assertEqual((result['state'],result['amount_raw'],result['refund_required']),('confirmed','500000',False))
        saved=json.loads(self.db.db.execute('SELECT evidence_json FROM settlement_verified_evidence WHERE digest=?',(result['evidence_digest'],)).fetchone()[0])
        self.assertEqual(saved['evidence']['transaction']['meta']['postTokenBalances'][0]['uiTokenAmount']['uiAmount'],{'rpc_decimal':'3.5000000000000000001'})
        self.assertEqual(self.observe_deposit(args)['evidence_digest'],result['evidence_digest'])
        self.assertEqual(self.db.db.execute('SELECT count(*) FROM settlement_verified_evidence').fetchone()[0],1)
    def test_deposit_wire_and_finalized_balance_mutations_never_confirm(self):
        args=self.deposit();original=copy.deepcopy(self.chain.reader.responses)
        edits=[lambda r:r['transaction']['message']['instructions'][1].update(data=sv.b58encode(b'wrong memo')),
               lambda r:r['transaction']['message'].update(recentBlockhash=sf.OTHER),
               lambda r:r['meta']['postTokenBalances'][1]['uiTokenAmount'].update(amount='10599032'),
               lambda r:r['meta']['postTokenBalances'][1].update(mint=sf.OTHER)]
        for edit in edits:
            self.chain.reader.responses=copy.deepcopy(original);edit(self.chain.reader.responses['getTransaction'])
            with self.assertRaises(ValueError):self.observe_deposit(args)
        self.assertEqual(self.db.db.execute('SELECT count(*) FROM settlement_verified_evidence').fetchone()[0],0)
    def test_missing_receipt_pending_and_nonfinalized_not_confirmed(self):
        args=self.deposit();saved=copy.deepcopy(self.chain.reader.responses['getTransaction'])
        self.chain.reader.responses['getTransaction']=None
        self.assertEqual(self.observe_deposit(args)['state'],'pending')
        self.chain.reader.responses['getTransaction']=saved
        self.chain.reader.responses['getSignatureStatuses']['value'][0]['confirmationStatus']='confirmed'
        with self.assertRaises(ValueError):self.observe_deposit(args)
    def test_finalized_failed_source_requires_exact_original_and_matching_error(self):
        args=self.deposit();error={'InstructionError':[0,'InsufficientFunds']}
        self.chain.reader.responses['getTransaction']['meta'].update(err=error,status={'Err':error})
        status=self.chain.reader.responses['getSignatureStatuses']['value'][0];status.update(err=error,status={'Err':error})
        self.assertEqual(self.observe_deposit(args)['state'],'failed')
        status['slot']+=1
        with self.assertRaises(ValueError):self.observe_deposit(args)
    def test_malformed_failed_source_never_authorizes_cancellation(self):
        args=self.deposit();original=copy.deepcopy(self.chain.reader.responses)
        for error in (False,{},'Unknown',{'InstructionError':[True,'Failure']},{'InstructionError':[2,'Failure']},{'InstructionError':[0,{'Custom':True}]}):
            self.chain.reader.responses=copy.deepcopy(original)
            self.chain.reader.responses['getTransaction']['meta']['err']=error
            self.chain.reader.responses['getSignatureStatuses']['value'][0]['err']=error
            with self.assertRaises(ValueError):self.observe_deposit(args)
        self.chain.reader.responses=copy.deepcopy(original)
        error={'InstructionError':[0,'InsufficientFunds']}
        self.chain.reader.responses['getTransaction']['meta']['err']=error
        self.chain.reader.responses['getSignatureStatuses']['value'][0]['err']=error
        self.chain.reader.responses['getSignatureStatuses']['context']['slot']=sf.SLOT-1
        with self.assertRaises(ValueError):self.observe_deposit(args)
    def test_late_valid_deposit_is_refund_only_and_preauthorization_payment_rejected(self):
        args=self.deposit();receipt=self.chain.reader.responses['getTransaction']
        receipt['blockTime']=args[0]['expires_at']+1;self.now=receipt['blockTime']+1
        self.assertTrue(self.observe_deposit(args)['refund_required'])
        receipt['blockTime']=args[0]['created_at']-1
        with self.assertRaises(ValueError):self.observe_deposit(args)
    def test_observer_rejects_unauthenticated_destination_or_amount_override(self):
        args=self.deposit()
        for key,value in [('destination_owner',sv.b58encode(bytes([22])*32)),('amount_raw','500001'),('intent_id','unknown')]:
            altered=copy.deepcopy(args);altered[0][key]=value
            with self.assertRaises(ValueError):self.observe_deposit(altered)
    def test_source_preparation_checks_current_ata_sol_and_exact_bound_message(self):
        intent=self.intent();self.chain.reader.responses.update(
            getLatestBlockhash={'context':{'slot':sf.SLOT},'value':{'blockhash':sf.BLOCKHASH,'lastValidBlockHeight':sf.SLOT+150}},
            getFeeForMessage={'value':5000},getMultipleAccounts={'context':{'slot':sf.SLOT},'value':[
                pf.token(sf.OWNER,500000),pf.account(pay.SYSTEM,b'',5000),pf.token(pay.VAULT,10000000),pf.multisig()]})
        plan=self.observer.prepare_source(intent)
        self.assertEqual(plan['fee_raw'],'5000');self.assertEqual(plan['expires_at'],sf.NOW+60)
        self.assertEqual(plan['prepared']['intent_digest'],j.digest(j.intent_message({k:v for k,v in intent.items() if k!='intent_id'})))
        self.chain.reader.responses['getMultipleAccounts']['value'][1]['lamports']=4999
        with self.assertRaises(ValueError):self.observer.prepare_source(intent)
    def test_snapshot_checks_live_reserve_config_mint_gas_and_pending_archive_gap(self):
        mint=bytearray(82);mint[44:46]=bytes([6,1])
        self.chain.reader.responses['getMultipleAccounts']={'context':{'slot':sf.SLOT},'value':[
            pf.multisig(),pf.token(pay.VAULT,10100000),pf.account(pay.TOKEN,mint),pf.account(pay.SYSTEM,b'',30000000)]}
        result=self.observer.snapshot()
        self.assertEqual((result['reserve_usdc_raw'],result['xusdc_supply_raw']),('10100000','10099031'))
        intent=self.intent();lease=self.db.acquire_route(intent['intent_id'],'offline')
        from runtime_store import RuntimeStore
        store=RuntimeStore(self.db)
        plan={'prepared':{},'context':{'before':{'slot':1,'block_hash':'01'*32}},'expires_at':self.now+60,'fee_raw':'1'}
        store.put(intent['intent_id'],'deposit',plan,lease)
        self.db.prepare_stage(intent['intent_id'],'deposit','solana:mainnet',sf.SIGNATURE,'ab'*32,lease=lease)
        with self.assertRaisesRegex(ValueError,'archive gap'):self.observer.snapshot()
    def xeris_case(self, stage='buy', failed=False):
        source=xf.OPERATOR if stage=='burn' else sf.OWNER
        intent=self.intent('redeem' if stage=='burn' else 'buy',source)
        before=xf.snapshot(100);after=xf.snapshot(113)
        if stage=='burn':
            before.update(native_raw=1000000,xusdc_raw=500000)
            plan=red.build_stage('burn',source,intent['destination_owner'],xf.BLOCKHASH,500000,route_id=intent['intent_id'],route_kind='xusdc')
        else:
            before['xusdc_raw']=500000;before['xusdc_supply_raw']+=500000
            plan=p.build_stage(stage,xf.OPERATOR,intent['destination_owner'],xf.BLOCKHASH,500000,
                33216842938 if stage=='buy' else None,route_id=intent['intent_id'])
        if stage=='buy' and not failed:
            after.update(native_raw=2000000,xusdc_raw=0,wxrs_raw=33383761746,xusdc_supply_raw=before['xusdc_supply_raw'],reserve_a_raw=676316238254,reserve_b_raw=10599031)
        proof=af.evidence(before,after,[(plan,xf.SIGNATURE,101,not failed)])
        proof.update(target_receipt=proof['receipts_by_node']['droplet'][xf.SIGNATURE],observed_confirmation_blocks=12)
        self.chain.state=after;self.chain.proof=proof
        self.db.acquire_route(intent['intent_id'],'offline')
        return intent,plan,{'before':before},proof
    def test_real_xeris_buy_accounting_returns_actual_output_not_quote(self):
        intent,plan,context,proof=self.xeris_case()
        result=self.observer.observe(intent,'buy',plan,xf.SIGNATURE,context)
        self.assertEqual(result['amount_raw'],'33383761746');self.assertFalse(self.chain.target_only)
        proof['raw_transactions_by_signature'][xf.SIGNATURE]=af.signed_wire(p.build_stage('buy',xf.OPERATOR,xf.RECIPIENT,bytes([19])*32,500000,33216842938,route_id=intent['intent_id']))
        with self.assertRaises(ValueError):self.observer.observe(intent,'buy',plan,xf.SIGNATURE,context)
    def test_real_failed_mint_evidence_is_failure_not_new_issuance(self):
        intent,plan,context,proof=self.xeris_case('mint',failed=True)
        result=self.observer.observe(intent,'mint',plan,xf.SIGNATURE,context)
        self.assertEqual(result['state'],'failed');self.assertNotIn('amount_raw',result)
        proof['receipts_by_node']['mac_treasury'][xf.SIGNATURE]['meta']['err']='OutcomeUnknown'
        with self.assertRaises(ValueError):self.observer.observe(intent,'mint',plan,xf.SIGNATURE,context)
    def test_real_burn_checks_canonical_original_without_user_wallet_exclusivity(self):
        intent,plan,context,proof=self.xeris_case('burn')
        result=self.observer.observe(intent,'burn',plan,xf.SIGNATURE,context)
        self.assertEqual(result['amount_raw'],'500000');self.assertTrue(self.chain.target_only)
        proof['canonical_blocks_by_node']['droplet'][-1]['hash']='ab'*32
        with self.assertRaises(ValueError):self.observer.observe(intent,'burn',plan,xf.SIGNATURE,context)
    def test_real_payout_exact_cpi_poststate_and_no_double_payment_authorization(self):
        intent=self.intent('redeem',xf.OPERATOR,pf.OWNER)
        def pure_plan(owner=pf.OWNER):
            return build_payout(owner,'500000',8,pf.BLOCKHASH),{'intent_id':intent['intent_id'],'amount_raw':'500000','transaction_index':8,'destination_owner':owner}
        with patch.object(pf,'plan',side_effect=pure_plan):prepared,binding,receipt,finality=pf.fixture()
        self.chain.reader.responses.update(getTransaction=receipt,getSignatureStatuses={'context':{'slot':1001},'value':[finality['signature_status']]},
            getMultipleAccounts={'context':{'slot':1000},'value':[pf.multisig(8),pf.proposal(prepared),pf.token(pay.VAULT,4500000),pf.token(pf.OWNER,500000)]})
        context={'payout_intent':binding}
        result=self.observer.observe(intent,'payout',prepared,pf.SIGNATURE,context)
        self.assertEqual((result['state'],result['amount_raw']),('confirmed','500000'))
        self.chain.reader.responses['getMultipleAccounts']['value'][-1]=pf.token(pf.OWNER,500001)
        # Later recipient activity cannot invalidate the already finalized
        # exact payout receipt or permit a second payment.
        again=self.observer.observe(intent,'payout',prepared,pf.SIGNATURE,context)
        self.assertEqual(again['state'],'confirmed')
        self.assertEqual(again['amount_raw'],intent['amount_raw'])
    def test_submission_recheck_rejects_expired_blockhash_without_broadcast(self):
        args=self.deposit();self.chain.reader.responses['isBlockhashValid']={'value':False}
        with self.assertRaises(ValueError):self.observer.validate_before_submission(args[0],'deposit',args[1],args[2])
        self.assertFalse(any(name.startswith('send') for name,*_ in self.chain.reader.calls))


if __name__=='__main__':unittest.main()
