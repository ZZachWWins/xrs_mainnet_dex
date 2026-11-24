"""Local scoped signer and one-shot transport. Disabled without a new policy.

No private bytes enter Python: a short-lived local Node process loads/signs
internally and emits only the public signed transaction. Policy files contain
paths/public identities and explicit limits, never key contents. Neither adapter
is invoked merely by importing this module or starting the paused API.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.request

from journal import canonical, decode58, require, SOLANA, XERIS
from public_rpc import strict_json, NoRedirect, SOLANA as SOLANA_RPC, XERIS as XERIS_RPC
import xeris_purchase as purchase
import solana_payout as payout


def charged_amount(journal, *, policy_id=None, intent_id=None):
    """A proved failed mint has no issuance effect and can use its refund path.

    Every unknown/prepared/submitted signature keeps its charge. The failed
    record remains immutable; only independently verified journal failure makes
    the alternative compensation fit the same reserved economic exposure.
    """
    rows=journal.db.execute('SELECT r.*,s.state AS execution_state FROM settlement_signer_records r LEFT JOIN settlement_stages s USING(intent_id,stage)').fetchall()
    return sum(int(row['charge_raw']) for row in rows
        if (policy_id is None or row['policy_id']==policy_id) and
           (intent_id is None or row['intent_id']==intent_id) and
           not(row['stage']=='mint' and row['execution_state']=='failed'))


class ScopedPolicy:
    def __init__(self, path, clock):
        self.path, self.clock = Path(path), clock

    def read(self):
        require(self.path.is_absolute() and not self.path.is_symlink(), 'Explicit local authorization required')
        info = self.path.stat()
        require(info.st_uid == os.getuid() and info.st_mode & 0o777 == 0o600 and info.st_size <= 8192,
                'Authorization file metadata rejected')
        policy = strict_json(self.path.read_bytes())
        require(type(policy) is dict and set(policy) == {'schema_version', 'policy_id', 'enabled',
            'valid_from', 'expires_at', 'maximum_usdc_raw', 'maximum_total_usdc_raw',
            'maximum_sol_debit_lamports', 'operator_wallet', 'keys'}, 'Invalid signing policy')
        require(policy['schema_version'] == 1 and type(policy['schema_version']) is int and policy['enabled'] is True,
                'Signing authorization is disabled')
        require(type(policy['policy_id']) is str and re.fullmatch('settlement-v2-[A-Za-z0-9_-]{1,80}', policy['policy_id']),
                'New settlement authorization required')
        now = self.clock()
        require(type(policy['valid_from']) is int and type(policy['expires_at']) is int and
                policy['valid_from'] <= now < policy['expires_at'] and
                0 < policy['expires_at']-policy['valid_from'] <= 86400, 'Signing authorization expired')
        for key in ('maximum_usdc_raw', 'maximum_total_usdc_raw', 'maximum_sol_debit_lamports'):
            value = policy[key]
            require(type(value) is str and re.fullmatch('[1-9][0-9]{0,19}', value) and int(value) < 2**64, 'Invalid signing limit')
        require(int(policy['maximum_usdc_raw']) <= int(policy['maximum_total_usdc_raw']) and
                int(policy['maximum_sol_debit_lamports']) <= payout.MAX_SOL_DEBIT, 'Invalid policy ceilings')
        operator = policy['operator_wallet']
        decode58(operator, 32)
        require(operator not in purchase.PRODUCERS | {purchase.LP}, 'Separate route wallet required')
        keys = policy['keys']
        require(type(keys) is dict and set(keys) == {'treasury', 'operator', 'solana'}, 'Signer roles missing')
        for role, expected in (('treasury', purchase.TREASURY), ('operator', operator), ('solana', payout.FUNDING)):
            entry = keys[role]
            require(type(entry) is dict and set(entry) == {'public_key', 'path'} and entry['public_key'] == expected,
                    'Signing role identity changed')
            path = Path(entry['path'])
            require(path.is_absolute() and str(path.resolve()) == str(path), 'Key path must be absolute and not a symlink')
            meta = path.stat()  # Metadata only; contents stay in the local helper.
            require(meta.st_uid == os.getuid() and meta.st_mode & 0o777 == 0o600 and 0 < meta.st_size < 4096,
                    'Key file metadata rejected')
        return policy


class LocalScopedSigner:
    def __init__(self, journal, policy, node=None, *, key_identity_check=None, can_operate=None):
        self.journal, self.policy = journal, policy
        self.can_operate = can_operate or (lambda:True)
        self.node = node or shutil.which('node')
        self.helper = Path(__file__).with_name('local_key_process.mjs').resolve()
        self.key_identity_check=key_identity_check or self._check_identity
        journal.db.execute('''CREATE TABLE IF NOT EXISTS settlement_signer_records(
          intent_id TEXT NOT NULL,stage TEXT NOT NULL,policy_id TEXT NOT NULL,
          message_sha256 TEXT NOT NULL,charge_raw TEXT NOT NULL,signed_base64 TEXT,
          PRIMARY KEY(intent_id,stage))''')
        journal.db.execute('''CREATE TABLE IF NOT EXISTS settlement_signer_reservations(
          intent_id TEXT PRIMARY KEY,policy_id TEXT NOT NULL,intent_digest TEXT NOT NULL,
          amount_raw TEXT NOT NULL)''')

    def _check_identity(self,entry):
        response=subprocess.run([self.node,str(self.helper)],
            input=canonical({**entry,'operation':'inspect'}).encode(),
            capture_output=True,timeout=8,check=False,env={'PATH':'/usr/bin:/bin'})
        return response.returncode==0 and response.stdout==b'{"identity_verified":true}'

    def _reserved(self,policy_id):
        return sum(int(r[0]) for r in self.journal.db.execute('SELECT amount_raw FROM settlement_signer_reservations WHERE policy_id=?',(policy_id,)))

    def has_reservation(self,intent):
        try:
            policy=self.policy.read()
            row=self.journal.db.execute('SELECT * FROM settlement_signer_reservations WHERE intent_id=?',(intent['intent_id'],)).fetchone()
            return (row is not None and row['policy_id']==policy['policy_id'] and
                row['intent_digest']==hashlib.sha256(canonical(intent).encode()).hexdigest() and
                int(row['amount_raw'])<=int(policy['maximum_usdc_raw']) and
                self._reserved(policy['policy_id'])<=int(policy['maximum_total_usdc_raw']))
        except Exception:
            return False

    def reserve_intent(self,intent,lease):
        policy=self.policy.read()
        with self.journal._atomic():
            self.journal._lease(intent['intent_id'],lease)
            _,stored=self.journal._intent(intent['intent_id'])
            require(canonical(intent)==canonical({**stored,'intent_id':intent['intent_id']}) and
                    stored['action'] in ('buy','redeem'),'Invalid reservation intent')
            require(not self.journal.processing_state()['paused'],'Intake paused')
            if self.has_reservation(intent):return
            require(self.journal.db.execute('SELECT 1 FROM settlement_signer_reservations WHERE intent_id=?',(intent['intent_id'],)).fetchone() is None,'Original reservation policy changed')
            require(policy['expires_at']>=self.journal._now()+600,'Insufficient settlement authorization runway')
            amount=int(stored['amount_raw'])
            require(amount<=int(policy['maximum_usdc_raw']) and self._reserved(policy['policy_id'])+amount<=int(policy['maximum_total_usdc_raw']), 'Settlement reservation budget exhausted')
            self.journal.db.execute('INSERT INTO settlement_signer_reservations VALUES(?,?,?,?)',
                (intent['intent_id'],policy['policy_id'],hashlib.sha256(canonical(intent).encode()).hexdigest(),str(amount)))

    def readiness(self):
        result = {'authorized': False, 'checked_at': self.journal._now(), 'policy_id': None, 'signers': []}
        try:
            require(self.can_operate(), 'Operator is stopping')
            policy = self.policy.read()
            require(self.node is not None and Path(self.node).is_file() and os.access(self.node,os.X_OK)
                    and self.helper.is_file(), 'Local signing helper unavailable')
            require(all(self.key_identity_check(entry) is True for entry in policy['keys'].values()),
                    'Local signing identity unavailable')
            result.update(authorized=True, policy_id=policy['policy_id'],
                signers=[policy['operator_wallet'], purchase.TREASURY, payout.FUNDING],
                maximum_usdc_raw=policy['maximum_usdc_raw'],
                remaining_total_usdc_raw=str(max(0,int(policy['maximum_total_usdc_raw'])-self._reserved(policy['policy_id']))),
                expires_at=policy['expires_at'])
        except Exception:
            pass
        return result

    def sign(self, intent, stage, prepared, fence):
        require(self.can_operate(), 'Operator is stopping')
        policy = self.policy.read()
        intent_id = intent.get('intent_id')
        with self.journal._atomic():
            # Hold the database write lock through the helper call. Another
            # worker cannot steal/renew the fence between validation and signing.
            self.journal._lease(intent_id, fence)
            row, stored = self.journal._intent(intent_id)
            require(canonical(intent) == canonical({**stored, 'intent_id': intent_id}), 'Signing intent changed')
            self.journal._previous(row, stored, stage)
            require(not self.journal.processing_state()['paused'], 'Signing paused')
            require(self.has_reservation(intent),'Signing has no current reserved settlement budget')
            plan_row = self.journal.db.execute('SELECT plan_json FROM settlement_plans WHERE intent_id=? AND stage=?', (intent_id,stage)).fetchone()
            require(plan_row is not None, 'Frozen plan is required')
            wrapper = json.loads(plan_row[0])
            require(canonical(wrapper['prepared']) == canonical(prepared) and wrapper['expires_at'] > self.journal._now(),
                    'Frozen signing plan changed or expired')
            charge = 0
            if stage in ('mint','buy','unwrap','deliver'):
                purchase.validate_prepared(prepared)
                require(stored['action'] == 'buy' and prepared['route_id'] == intent_id and prepared['stage'] == stage and
                    prepared['operator_wallet'] == policy['operator_wallet'] and prepared['recipient'] == stored['destination_owner'], 'Signing purchase scope changed')
                expected = self.journal._amount(intent_id, 'buy') if stage in ('unwrap','deliver') else self.journal._amount(intent_id, 'deposit')
                require(prepared['fields']['amount_raw'] == expected, 'Signing amount is not confirmed')
                if stage == 'buy':
                    require(prepared['fields']['min_output_raw'] == stored['min_output_raw'], 'Signing minimum changed')
                role = 'treasury' if stage == 'mint' else 'operator'
                charge = int(expected) if stage == 'mint' else 0
            elif stage in ('payout','refund'):
                expected = self.journal._amount(intent_id,'burn' if stage=='payout' else 'deposit')
                recipient = stored['destination_owner'] if stage=='payout' else stored['source_owner']
                payout.review_plan(prepared, {'intent_id':intent_id,'amount_raw':expected,
                    'destination_owner':recipient,'transaction_index':prepared.get('transaction_index')})
                payout._costs(prepared)
                require(prepared['quoted_total_sol_debit_lamports'] <= int(policy['maximum_sol_debit_lamports']), 'Signing rent/fee ceiling exceeded')
                if stage=='refund': require(row['route_mode']=='refund','Refund is not due')
                role, charge = 'solana', int(expected)
            else:
                raise ValueError('User wallet stages cannot be signed by the operator')
            require(charge <= int(policy['maximum_usdc_raw']), 'Signing per-request limit exceeded')
            message = base64.b64decode(prepared['message_base64'], validate=True)
            message_hash = hashlib.sha256(message).hexdigest()
            previous = self.journal.db.execute('SELECT * FROM settlement_signer_records WHERE intent_id=? AND stage=?', (intent_id,stage)).fetchone()
            if previous:
                require(previous['policy_id']==policy['policy_id'] and previous['message_sha256']==message_hash and previous['charge_raw']==str(charge), 'Signing original is immutable')
                if previous['signed_base64'] is not None:return previous['signed_base64']
            else:
                total = charged_amount(self.journal,policy_id=policy['policy_id'])
                require(total+charge <= int(policy['maximum_total_usdc_raw']), 'Aggregate signing limit exhausted')
                intent_charge=charged_amount(self.journal,intent_id=intent_id)
                require(intent_charge+charge<=int(stored['amount_raw']),'Settlement may mint OR refund, not both')
                self.journal.db.execute('INSERT INTO settlement_signer_records VALUES(?,?,?,?,?,NULL)',(intent_id,stage,policy['policy_id'],message_hash,str(charge)))
            request = {**policy['keys'][role], 'message_base64':prepared['message_base64']}
            # Never propagate helper stdout/stderr on failure: only a validated
            # public signature envelope is allowed back into the journal.
            require(self.can_operate(), 'Operator is stopping')
            process = subprocess.run([self.node,str(self.helper)],input=canonical(request).encode(),capture_output=True,timeout=8,check=False,
                env={'PATH':'/usr/bin:/bin'})
            require(process.returncode==0 and len(process.stdout)<=16384, 'Local signing helper failed')
            answer = strict_json(process.stdout)
            require(type(answer) is dict and set(answer)=={'signed_transaction_base64'}, 'Invalid signing helper response')
            signed = answer['signed_transaction_base64']
            wire = base64.b64decode(signed,validate=True)
            require(base64.b64encode(wire).decode()==signed and wire[:1]==b'\1' and wire[65:]==message, 'Local signer altered message')
            self.journal._lease(intent_id,fence)
            self.journal.db.execute('UPDATE settlement_signer_records SET signed_base64=? WHERE intent_id=? AND stage=?',(signed,intent_id,stage))
            return signed


class SingleSubmissionBroadcaster:
    def __init__(self, journal, policy, transport=None, *, policy_id, can_operate=None):
        self.journal, self.policy, self.transport = journal, policy, transport
        self.can_operate = can_operate or (lambda:True)
        require(type(policy_id) is str and re.fullmatch('settlement-v2-[A-Za-z0-9_-]{1,80}',policy_id), 'Explicit broadcast authorization required')
        self.policy_id=policy_id
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        journal.db.execute('''CREATE TABLE IF NOT EXISTS settlement_network_attempts(
          chain TEXT NOT NULL,transaction_id TEXT NOT NULL,payload_digest TEXT NOT NULL,
          at INTEGER NOT NULL,PRIMARY KEY(chain,transaction_id))''')

    def _authorize(self,record,policy):
        require(policy['policy_id']==self.policy_id,'Broadcast authorization changed')
        _,intent=self.journal._intent(record['intent_id'])
        require(int(intent['amount_raw'])<=int(policy['maximum_usdc_raw']), 'Current broadcast amount limit exceeded')
        plan_row=self.journal.db.execute('SELECT plan_json FROM settlement_plans WHERE intent_id=? AND stage=?',(record['intent_id'],record['stage'])).fetchone()
        require(plan_row is not None,'Frozen broadcast plan missing')
        prepared=json.loads(plan_row[0])['prepared']
        message_hash=hashlib.sha256(base64.b64decode(prepared['message_base64'],validate=True)).hexdigest()
        wire=base64.b64decode(record['transaction_base64'],validate=True)
        require(hashlib.sha256(wire[65:]).hexdigest()==message_hash,'Broadcast message differs from frozen plan')
        total=charged_amount(self.journal,policy_id=self.policy_id)
        reservation=self.journal.db.execute('SELECT * FROM settlement_signer_reservations WHERE intent_id=?',(record['intent_id'],)).fetchone()
        require(reservation is not None and reservation['policy_id']==self.policy_id and
            reservation['intent_digest']==hashlib.sha256(canonical({**intent,'intent_id':record['intent_id']}).encode()).hexdigest() and
            reservation['amount_raw']==intent['amount_raw'],'Broadcast has no bound settlement reservation')
        reserved=sum(int(r[0]) for r in self.journal.db.execute('SELECT amount_raw FROM settlement_signer_reservations WHERE policy_id=?',(self.policy_id,)))
        require(reserved<=int(policy['maximum_total_usdc_raw']),'Current broadcast reservation budget exceeded')
        if record['stage'] in ('deposit','burn'):
            require(total<=reserved, 'Current source signing budget inconsistent')
        else:
            signer=self.journal.db.execute('SELECT * FROM settlement_signer_records WHERE intent_id=? AND stage=?',(record['intent_id'],record['stage'])).fetchone()
            require(signer is not None and signer['policy_id']==self.policy_id and signer['message_sha256']==message_hash and signer['signed_base64']==record['transaction_base64'],'Broadcast is not the scoped signed original')
            require(total<=int(policy['maximum_total_usdc_raw']) and int(signer['charge_raw'])<=int(policy['maximum_usdc_raw']), 'Current broadcast signing budget exceeded')
            if record['stage'] in ('payout','refund'):
                require(prepared['quoted_total_sol_debit_lamports']<=int(policy['maximum_sol_debit_lamports']), 'Current broadcast fee ceiling exceeded')
            else:
                require(prepared['operator_wallet']==policy['operator_wallet'], 'Broadcast operator changed')

    def submit(self,chain,transaction_base64,transaction_id,fence):
        require(self.can_operate(), 'Operator is stopping')
        require(chain in (SOLANA,XERIS), 'Unsupported broadcast chain')
        policy=self.policy.read()  # Revocation takes effect at the network boundary.
        wire=base64.b64decode(transaction_base64,validate=True)
        require(base64.b64encode(wire).decode()==transaction_base64 and wire[:1]==b'\1' and wire[1:65]==decode58(transaction_id,64), 'Broadcast signature mismatch')
        payload_hash=hashlib.sha256(wire).hexdigest()
        with self.journal._atomic():
            self.journal._lease(fence['intent_id'],fence)
            require(not self.journal.processing_state()['paused'], 'Broadcast paused')
            record=self.journal.db.execute('SELECT s.*,p.transaction_base64 FROM settlement_stages s JOIN settlement_payloads p USING(intent_id,stage) WHERE s.chain=? AND s.transaction_id=?',(chain,transaction_id)).fetchone()
            require(record is not None and record['intent_id']==fence['intent_id'] and record['state']=='submitted' and record['signed_payload_digest']==payload_hash and record['transaction_base64']==transaction_base64, 'Broadcast has no durable submitted original')
            self._authorize(record,policy)
            require(self.journal.db.execute('SELECT 1 FROM settlement_network_attempts WHERE chain=? AND transaction_id=?',(chain,transaction_id)).fetchone() is None, 'Original network attempt already reserved')
            self.journal.db.execute('INSERT INTO settlement_network_attempts VALUES(?,?,?,?)',(chain,transaction_id,payload_hash,self.journal._now()))
        # The attempt reservation is durable even when the process crashes or
        # times out. Re-enter under the same fence and keep its DB lock across
        # the network boundary so another worker cannot concurrently take it.
        with self.journal._atomic():
            self.journal._lease(fence['intent_id'],fence)
            self._authorize(record,self.policy.read())
            require(not self.journal.processing_state()['paused'], 'Broadcast paused')
            url = SOLANA_RPC if chain==SOLANA else XERIS_RPC+'/submit'
            body = {'jsonrpc':'2.0','id':1,'method':'sendTransaction','params':[transaction_base64,{'encoding':'base64','skipPreflight':False,'preflightCommitment':'confirmed','maxRetries':0}]} if chain==SOLANA else {'tx_base64':transaction_base64}
            require(self.can_operate(), 'Operator is stopping')
            if self.transport:
                self.transport(url,body)
            else:
                request=urllib.request.Request(url,data=canonical(body).encode(),headers={'Content-Type':'application/json','Accept':'application/json'},method='POST')
                with self.opener.open(request,timeout=12) as response:
                    require(response.status==200,'Submission response unavailable')
                    # A response, even an RPC error, is never execution evidence.
                    require(len(response.read(65537))<=65536,'Submission response exceeds bound')
