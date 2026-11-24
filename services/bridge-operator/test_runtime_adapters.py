"""Isolated signing/broadcast boundary tests. Never touches project private keys."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys
import unittest
from unittest.mock import patch

import journal as j
import runtime_adapters as adapters
from runtime_store import RuntimeStore
import xeris_purchase as p
import solana_payout as payout
from test_journal import OWNER, DESTINATION, ATA, SIGNATURE, b58

NODE_FOR_TESTS = shutil.which('node') or ('/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node'
    if Path('/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node').is_file() else None)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name).resolve()
        self.now=1_800_000_000
        self.journal=j.Journal(self.root/'isolated.sqlite3',clock=lambda:self.now)
        self.operator=b58(bytes([10])*32)
        self.store=RuntimeStore(self.journal)
        self.policy_path=self.root/'authorization.json'
        self.policy={'schema_version':1,'policy_id':'settlement-v2-unit-test','enabled':True,
            'valid_from':self.now-1,'expires_at':self.now+600,'maximum_usdc_raw':'500000',
            'maximum_total_usdc_raw':'1000000','maximum_requests':2,'maximum_sol_debit_lamports':'10000000',
            'operator_wallet':self.operator,'keys':{}}
        for role,pub in (('operator',self.operator),('treasury',p.TREASURY),('solana',payout.FUNDING)):
            path=self.root/(role+'.json')
            path.write_text('Public metadata fixture; no private key')
            path.chmod(0o600)
            self.policy['keys'][role]={'public_key':pub,'path':str(path)}
        self.save_policy()
        self.scope=adapters.ScopedPolicy(self.policy_path,lambda:self.now)
        self.signer=adapters.LocalScopedSigner(self.journal,self.scope,node=sys.executable,key_identity_check=lambda entry:True)
        with patch.object(j,'verify_ed25519'):
            intent=self.journal.issue_challenge(action='buy',source_owner=OWNER,destination_owner=DESTINATION,
                solana_token_account=ATA,amount_raw='500000',min_output_raw='30000000000',quote_id='q1',client_request_id='c1')['intent']
            self.intent_id=self.journal.accept_intent(intent,SIGNATURE)['id']
        self.intent={**intent,'intent_id':self.intent_id}
        self.journal.resume('Isolated test authorization')
        self.lease=self.journal.acquire_route(self.intent_id,'worker1')
        self.signer.reserve_intent(self.intent,self.lease)
        self.journal.claim_source(self.intent_id,j.SOLANA,b58(bytes([21])*64),'ab'*32,'500000')
        self.plan=p.build_stage('mint',self.operator,DESTINATION,bytes([8])*32,500000,route_id=self.intent_id)
        self.wrapper={'prepared':self.plan,'context':{},'expires_at':self.now+60,'fee_raw':'1000000'}
        self.store.put(self.intent_id,'mint',self.wrapper,self.lease)

    def save_policy(self):
        self.policy_path.write_text(json.dumps(self.policy))
        self.policy_path.chmod(0o600)

    def tearDown(self):
        self.journal.close();self.temp.cleanup()

    def wire(self):
        return base64.b64encode(b'\1'+bytes([19])*64+base64.b64decode(self.plan['message_base64'])).decode()

    def test_disabled_expired_wrong_role_and_insecure_policy_fail_closed(self):
        baseline=copy.deepcopy(self.policy)
        for change in ({'enabled':False},{'expires_at':self.now},{'maximum_total_usdc_raw':'1'}, {'operator_wallet':p.TREASURY}):
            self.policy={**baseline,**change};self.save_policy()
            self.assertFalse(self.signer.readiness()['authorized'])
        self.policy=baseline;self.save_policy();self.policy_path.chmod(0o644)
        self.assertFalse(self.signer.readiness()['authorized'])

    def test_missing_authorization_does_not_load_private_file(self):
        self.policy_path.unlink()
        with patch.object(subprocess,'run') as run:
            self.assertFalse(self.signer.readiness()['authorized'])
            run.assert_not_called()

    def test_missing_executable_or_unavailable_identity_never_reports_ready(self):
        self.signer.node='/not/installed/node'
        self.assertFalse(self.signer.readiness()['authorized'])
        self.signer.node=sys.executable
        self.signer.key_identity_check=lambda entry:False
        self.assertFalse(self.signer.readiness()['authorized'])

    def test_request_limit_is_required_and_exactly_an_integer_between_one_and_one_thousand(self):
        baseline=copy.deepcopy(self.policy)
        with patch.object(subprocess,'run') as helper:
            for value in (None,0,1001,True,False,1.0,'2'):
                with self.subTest(maximum_requests=value):
                    self.policy=copy.deepcopy(baseline)
                    if value is None:del self.policy['maximum_requests']
                    else:self.policy['maximum_requests']=value
                    self.save_policy()
                    with self.assertRaises(ValueError):self.scope.read()
                    self.assertFalse(self.signer.readiness()['authorized'])
            helper.assert_not_called()
        for value in (1,1000):
            self.policy={**baseline,'maximum_requests':value};self.save_policy()
            self.assertEqual(self.scope.read()['maximum_requests'],value)
            self.assertEqual(self.signer.readiness()['remaining_requests'],value-1)

    def test_changed_destination_message_and_stale_fence_never_invoke_helper(self):
        changed={**self.intent,'destination_owner':OWNER}
        changed_plan=copy.deepcopy(self.plan);changed_plan['fields']['amount_raw']='499999'
        with patch.object(subprocess,'run') as run:
            for intent,plan,fence in ((changed,self.plan,self.lease),(self.intent,changed_plan,self.lease),(self.intent,self.plan,{**self.lease,'fence':999})):
                with self.assertRaises(ValueError):self.signer.sign(intent,'mint',plan,fence)
            run.assert_not_called()

    def test_original_signing_is_idempotent_and_budget_counts_once(self):
        response=subprocess.CompletedProcess([],0,json.dumps({'signed_transaction_base64':self.wire()}).encode(),b'')
        with patch.object(subprocess,'run',return_value=response) as run:
            first=self.signer.sign(self.intent,'mint',self.plan,self.lease)
            self.assertEqual(self.signer.sign(self.intent,'mint',self.plan,self.lease),first)
            self.assertEqual(run.call_count,1)
        rows=self.journal.db.execute('SELECT charge_raw FROM settlement_signer_records').fetchall()
        self.assertEqual([r[0] for r in rows],['500000'])

    def test_aggregate_scope_exhaustion_rejects_before_key_helper(self):
        self.journal.db.execute('INSERT INTO settlement_signer_records VALUES(?,?,?,?,?,?)',('prior','mint',self.policy['policy_id'],'aa'*32,'1000000','public'))
        with patch.object(subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'Aggregate'):
                self.signer.sign(self.intent,'mint',self.plan,self.lease)
            run.assert_not_called()

    def prepare_submitted(self):
        wire=self.wire();raw=base64.b64decode(wire);txid=b58(raw[1:65])
        self.journal.prepare_stage(self.intent_id,'mint',j.XERIS,txid,hashlib.sha256(raw).hexdigest(),lease=self.lease,signed_payload_base64=wire)
        self.journal.mark_submitted(self.intent_id,'mint',txid,lease=self.lease)
        self.journal.db.execute('INSERT INTO settlement_signer_records VALUES(?,?,?,?,?,?)',(self.intent_id,'mint',self.policy['policy_id'],hashlib.sha256(raw[65:]).hexdigest(),'500000',wire))
        return wire,txid

    def test_broadcast_requires_persisted_original_and_occurs_at_most_once(self):
        calls=[]
        broadcaster=adapters.SingleSubmissionBroadcaster(self.journal,self.scope,lambda *args:calls.append(args),policy_id=self.policy['policy_id'])
        wire,txid=self.prepare_submitted()
        broadcaster.submit(j.XERIS,wire,txid,self.lease)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][0],'https://mainnetrpc.xerisweb.com/submit')
        with self.assertRaisesRegex(ValueError,'already reserved'):broadcaster.submit(j.XERIS,wire,txid,self.lease)
        self.assertEqual(len(calls),1)

    def test_timeout_attempt_survives_restart_and_cannot_be_sent_again(self):
        calls=[]
        def timeout(*args):calls.append(args);raise TimeoutError()
        broadcaster=adapters.SingleSubmissionBroadcaster(self.journal,self.scope,timeout,policy_id=self.policy['policy_id'])
        wire,txid=self.prepare_submitted()
        with self.assertRaises(TimeoutError):broadcaster.submit(j.XERIS,wire,txid,self.lease)
        restarted=adapters.SingleSubmissionBroadcaster(self.journal,self.scope,timeout,policy_id=self.policy['policy_id'])
        with self.assertRaisesRegex(ValueError,'already reserved'):restarted.submit(j.XERIS,wire,txid,self.lease)
        self.assertEqual(len(calls),1)

    def test_revocation_prevents_broadcast_at_boundary(self):
        calls=[]
        broadcaster=adapters.SingleSubmissionBroadcaster(self.journal,self.scope,lambda *args:calls.append(args),policy_id=self.policy['policy_id'])
        wire,txid=self.prepare_submitted()
        self.policy['enabled']=False;self.save_policy()
        with self.assertRaises(ValueError):broadcaster.submit(j.XERIS,wire,txid,self.lease)
        self.assertEqual(calls,[])

    def test_replacement_policy_or_lowered_limit_cannot_send_old_signature(self):
        calls=[]
        broadcaster=adapters.SingleSubmissionBroadcaster(self.journal,self.scope,lambda *args:calls.append(args),policy_id=self.policy['policy_id'])
        wire,txid=self.prepare_submitted()
        self.policy['policy_id']='settlement-v2-replacement';self.save_policy()
        with self.assertRaisesRegex(ValueError,'authorization changed'):broadcaster.submit(j.XERIS,wire,txid,self.lease)
        self.policy['policy_id']='settlement-v2-unit-test';self.policy['maximum_usdc_raw']='400000';self.save_policy()
        with self.assertRaisesRegex(ValueError,'amount limit'):broadcaster.submit(j.XERIS,wire,txid,self.lease)
        self.assertEqual(calls,[])


class LocalHelperTests(unittest.TestCase):
    @unittest.skipUnless(NODE_FOR_TESTS,'Node required')
    def test_synthetic_key_is_signed_locally_without_secret_output(self):
        # Generation, key storage and signing all happen inside a local process.
        # Python receives a public-only assertion, never private key contents.
        code=r'''
const fs=require('node:fs'),crypto=require('node:crypto'),cp=require('node:child_process');
const pair=crypto.generateKeyPairSync('ed25519');
const privateJwk=pair.privateKey.export({format:'jwk'}),publicBytes=Buffer.from(privateJwk.x,'base64url');
const secret=Buffer.concat([Buffer.from(privateJwk.d,'base64url'),publicBytes]);
fs.writeFileSync(process.argv[1],JSON.stringify([...secret]),{mode:0o600});secret.fill(0);
let n=BigInt('0x'+publicBytes.toString('hex')),pub='';const alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
while(n){pub=alphabet[Number(n%58n)]+pub;n/=58n;}for(const b of publicBytes){if(b)break;pub='1'+pub;}
const message=Buffer.concat([Buffer.from([1,0,1,2]),publicBytes,Buffer.alloc(32),Buffer.alloc(32,1),Buffer.from([1,1,1,0,1,0])]);
const result=cp.spawnSync(process.execPath,[process.argv[2]],{input:JSON.stringify({path:process.argv[1],public_key:pub,message_base64:message.toString('base64')}),encoding:'utf8'});
if(result.status!==0)throw Error('helper failed');
const answer=JSON.parse(result.stdout),wire=Buffer.from(answer.signed_transaction_base64,'base64');
if(Object.keys(answer).join(',')!=='signed_transaction_base64'||!wire.subarray(65).equals(message)||!crypto.verify(null,message,pair.publicKey,wire.subarray(1,65)))throw Error('bad public signature');
const inspected=cp.spawnSync(process.execPath,[process.argv[2]],{input:JSON.stringify({operation:'inspect',path:process.argv[1],public_key:pub}),encoding:'utf8'});
if(inspected.status!==0||inspected.stdout!=='{"identity_verified":true}')throw Error('identity check failed');
process.stdout.write('public signature verified');
'''
        with tempfile.TemporaryDirectory() as folder:
            result=subprocess.run([NODE_FOR_TESTS,'-e',code,str(Path(folder).resolve()/'synthetic.json'),str(Path(adapters.__file__).with_name('local_key_process.mjs'))],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,'Synthetic local helper check failed')
            self.assertEqual(result.stdout,'public signature verified')
            self.assertEqual(result.stderr,'')


if __name__=='__main__':unittest.main()
