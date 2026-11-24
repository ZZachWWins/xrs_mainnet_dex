"""Read-only chain adapter for the fenced settlement executor.

Never signs or broadcasts. Solana receipts are finalized; Xeris evidence comes
from a pinned, contiguous three-node archive and >=12 subsequent blocks. These
are explicit trusted observer assumptions, not cryptographic cross-chain finality.
"""
import base64
from decimal import Decimal
import json
import time
from journal import canonical, digest, intent_message, require
from solana_deposit import build_deposit
import solana_verify as sol
import solana_payout as pay
import xeris_purchase as purchase
import xeris_redemption as redemption
import xeris_accounting as accounting
from payout_builder import build_payout


def compiled_message_bytes(message):
    """Bounded legacy message encoder shared by deposit and payout observation.

    It reconstructs bytes only; the specialized verifier separately enforces
    exact instruction/account/amount semantics for the selected route.
    """
    require(type(message) is dict and not set(message)-{'header','accountKeys','recentBlockhash','instructions','addressTableLookups'},
            'Unsupported Solana message encoding')
    require(message.get('addressTableLookups',[]) == [], 'Address lookups forbidden')
    header,keys,instructions=message.get('header'),message.get('accountKeys'),message.get('instructions')
    require(type(header) is dict and type(keys) is list and 1<=len(keys)<=64 and
            len(keys)==len(set(keys)) and type(instructions) is list and 1<=len(instructions)<=5,
            'Invalid bounded Solana message')
    fields=[header.get(k) for k in ('numRequiredSignatures','numReadonlySignedAccounts','numReadonlyUnsignedAccounts')]
    require(all(type(v) is int and 0<=v<=255 for v in fields), 'Invalid Solana header')
    result=bytes(fields)+pay._short(len(keys))+b''.join(pay.pub(k) for k in keys)+sol.b58decode(message.get('recentBlockhash'),32)+pay._short(len(instructions))
    for ix in instructions:
        require(type(ix) is dict and not set(ix)-{'programIdIndex','accounts','data','stackHeight'}, 'Invalid compiled instruction')
        index,accounts=ix.get('programIdIndex'),ix.get('accounts')
        require(type(index) is int and 0<=index<len(keys) and type(accounts) is list and len(accounts)<=64 and
                all(type(x) is int and 0<=x<len(keys) for x in accounts), 'Invalid instruction account index')
        data=pay.decode58_data(ix.get('data'))
        result+=bytes([index])+pay._short(len(accounts))+bytes(accounts)+pay._short(len(data))+data
    require(len(result)+65<=1232, 'Solana transaction exceeds packet bound')
    return result


def evidence_value(value):
    # Display-only RPC decimals are never monetary inputs. Preserve their exact
    # lexical numeric value in a tagged audit scalar instead of float rounding.
    if isinstance(value,Decimal):
        require(value.is_finite(), 'Nonfinite evidence decimal')
        return {'rpc_decimal':str(value)}
    if type(value) is dict:return {k:evidence_value(v) for k,v in value.items()}
    if type(value) is list:return [evidence_value(v) for v in value]
    return value


class SettlementObserver:
    def __init__(self, chain, journal, *, operator_wallet, clock=None):
        purchase.address(operator_wallet)
        self.chain, self.journal, self.reader = chain, journal, chain.reader
        self.operator_wallet = operator_wallet
        self.clock = clock or (lambda: int(time.time()))
        journal.db.execute('''CREATE TABLE IF NOT EXISTS settlement_verified_evidence(
          digest TEXT PRIMARY KEY,evidence_json TEXT NOT NULL,observed_at INTEGER NOT NULL)''')

    def _save(self, value):
        encoded = canonical(evidence_value(value))
        require(len(encoded.encode()) <= 4_500_000, 'Verified evidence exceeds bound')
        hashed = digest(encoded.encode())
        self.journal.db.execute('INSERT OR IGNORE INTO settlement_verified_evidence VALUES(?,?,?)',
                                (hashed, encoded, self.clock()))
        return hashed

    def _bind(self, intent):
        require(type(intent) is dict and type(intent.get('intent_id')) is str, 'Accepted intent required')
        saved = self.journal.status(intent['intent_id'])['intent']
        require(canonical(saved) == canonical({k:v for k,v in intent.items() if k != 'intent_id'}),
                'Observer intent differs from prior authenticated journal record')

    def _solana_accounts(self, addresses, **extra):
        require(self.reader.solana('getGenesisHash') == sol.SOLANA_GENESIS, 'Wrong Solana genesis')
        result = self.reader.solana('getMultipleAccounts', [addresses,
            {'encoding': 'base64', 'commitment': 'finalized', **extra}])
        require(type(result) is dict and type(result.get('value')) is list and len(result['value']) == len(addresses),
                'Incomplete Solana account evidence')
        sol._uint(result.get('context', {}).get('slot'), 'Invalid Solana context', positive=True)
        return result

    def snapshot(self):
        started = self.clock()
        state = self.chain.snapshot_wallet(self.operator_wallet)
        accounts = self._solana_accounts([pay.MULTISIG, pay.SOURCE, pay.USDC, pay.FUNDING])['value']
        pay.decode_multisig(accounts[0])
        reserve = pay.verify_ata(accounts[1], pay.VAULT)
        mint = pay.account_bytes(accounts[2], pay.TOKEN, 82)
        require(mint[44:46] == bytes([6, 1]), 'Wrong USDC mint state')
        member = accounts[3]
        require(type(member) is dict and member.get('owner') == pay.SYSTEM and member.get('executable') is False
                and member.get('data') == ['', 'base64'], 'Invalid payout fee account')
        fee_balance = pay.uint(member.get('lamports'))
        authority = [self.reader.xeris('getBalance', [purchase.TREASURY], node)['value'] for node in sorted(purchase.NODES)]
        require(all(type(x) is int and x == authority[0] and 0 <= x < 2**64 for x in authority), 'Mint authority gas differs across nodes')
        # A fresh archive tip alone does not establish completeness for old
        # pending plans. Every persisted before-anchor must remain available.
        if self.journal.db.execute("SELECT 1 FROM sqlite_master WHERE name='settlement_plans'").fetchone():
            plans = self.journal.db.execute('''SELECT p.plan_json FROM settlement_plans p
              JOIN settlement_stages s ON s.intent_id=p.intent_id AND s.stage=p.stage
              WHERE s.state IN ('prepared','submitted','unknown')''').fetchall()
            for row in plans:
                before = json.loads(row[0])['context'].get('before')
                if before:
                    record = self.chain.archive.get(before['slot'])
                    require(all(b['hash'] == before['block_hash'] for b in record['canonical_blocks_by_node'].values()),
                            'Pending before-anchor conflicts with archive')
        require(self.clock()-started <= 10, 'Reserve snapshot exceeded freshness bound')
        return {'checked_at': started, 'checkpoint_verified': True, 'archive_complete': True,
            'reserve_usdc_raw': str(reserve), 'xusdc_supply_raw': str(state['xusdc_supply_raw']),
            'operator_native_raw': str(state['native_raw']), 'mint_authority_native_raw': str(authority[0]),
            'solana_fee_lamports': str(fee_balance)}

    def _xeris_hash(self):
        values = [self.reader.xeris('getLatestBlockhash', [], node) for node in sorted(purchase.NODES)]
        require(len({canonical(v) for v in values}) == 1, 'Blockhash observers disagree')
        response = values[0]
        blockhash = purchase._hash(response.get('value', {}).get('blockhash'))
        last = purchase.raw(response.get('value', {}).get('lastValidBlockHeight'), positive=True)
        slot = purchase.raw(response.get('context', {}).get('slot'), positive=True)
        require(last > slot, 'Expired Xeris blockhash')
        return bytes.fromhex(blockhash), last

    def prepare_source(self, intent):
        self._bind(intent)
        now = self.clock()
        require(now < intent['expires_at'], 'Source intent expired')
        if intent['action'] == 'buy':
            block = self.reader.solana('getLatestBlockhash', [{'commitment':'finalized'}])
            latest = block.get('value', {})
            last = pay.uint(latest.get('lastValidBlockHeight'), True)
            min_slot = pay.uint(block.get('context', {}).get('slot'), True)
            original = {k:v for k,v in intent.items() if k != 'intent_id'}
            prepared = build_deposit(intent['source_owner'], intent['amount_raw'], latest.get('blockhash'),
                                     intent_digest=digest(intent_message(original)))
            accounts = self._solana_accounts([intent['solana_token_account'], intent['source_owner'], pay.SOURCE, pay.MULTISIG])['value']
            require(pay.verify_ata(accounts[0], intent['source_owner']) >= int(intent['amount_raw']), 'Insufficient source USDC')
            pay.verify_ata(accounts[2], pay.VAULT); pay.decode_multisig(accounts[3])
            fee = self.reader.solana('getFeeForMessage', [prepared['message_base64'], {'commitment':'confirmed'}])
            fee = pay.uint(fee.get('value'), True)
            payer = accounts[1]
            require(type(payer) is dict and payer.get('owner') == pay.SYSTEM and payer.get('executable') is False
                    and payer.get('data') == ['', 'base64'] and pay.uint(payer.get('lamports')) >= fee,
                    'Insufficient source SOL or invalid fee payer')
            context = {'not_before': intent['created_at'], 'expires_at': intent['expires_at'],
                'min_slot': min_slot, 'last_valid_block_height': last}
        elif intent['action'] == 'redeem':
            before = self.chain.snapshot_wallet(intent['source_owner'])
            redemption.validate_fee_allowance(before['native_raw'], int(intent['amount_raw']), 'xusdc')
            require(before['xusdc_raw'] >= int(intent['amount_raw']), 'Insufficient source xUSDC')
            blockhash, last = self._xeris_hash()
            prepared = redemption.build_stage('burn', intent['source_owner'], intent['destination_owner'],
                blockhash, int(intent['amount_raw']), route_id=intent['intent_id'], route_kind='xusdc')
            context = {'before': before, 'last_valid_block_height': last}
            fee = purchase.BASE_FEE_RAW
        else:
            raise ValueError('Unsupported source route')
        return {'prepared': prepared, 'context': context,
                'expires_at': min(intent['expires_at'], now+60), 'fee_raw': str(fee)}

    def prepare_operator(self, intent, stage, confirmed_stages):
        self._bind(intent)
        require(canonical(confirmed_stages) == canonical(self.journal.status(intent['intent_id'])['stages']),
                'Confirmed stages must originate from the durable journal')
        verified = {s['stage']: s['amount_raw'] for s in confirmed_stages if s['state'] == 'confirmed'}
        now = self.clock()
        if stage in ('mint', 'buy', 'unwrap', 'deliver'):
            require(verified.get('deposit') == intent['amount_raw'], 'Verified deposit required')
            before = self.chain.snapshot_wallet(self.operator_wallet)
            blockhash, last = self._xeris_hash()
            amount = verified['buy'] if stage in ('unwrap','deliver') else verified['deposit']
            prepared = purchase.build_stage(stage, self.operator_wallet, intent['destination_owner'], blockhash,
                int(amount), int(intent['min_output_raw']) if stage == 'buy' else None, route_id=intent['intent_id'])
            # Reject an impossible transition BEFORE signing. Exact poststate
            # and output are checked again from independently observed execution.
            trial = {k:before[k] for k in purchase.BALANCE_FIELDS}
            purchase._apply_purchase_accounted(trial, prepared)
            context, fee = {'before': before, 'last_valid_block_height': last}, purchase.BASE_FEE_RAW
        elif stage in ('payout','refund'):
            amount = verified['burn' if stage == 'payout' else 'deposit']
            owner = intent['destination_owner'] if stage == 'payout' else intent['source_owner']
            account = self._solana_accounts([pay.MULTISIG])['value'][0]
            index = pay.decode_multisig(account)['transaction_index'] + 1
            latest = self.reader.solana('getLatestBlockhash', [{'commitment':'finalized'}])['value']
            plan = build_payout(owner, amount, index, latest['blockhash'])
            binding = {'intent_id': intent['intent_id'], 'destination_owner': owner,
                       'amount_raw': amount, 'transaction_index': index}
            preflight = pay.preflight(plan, binding, self.reader.solana,
                max_sol_debit_lamports=pay.MAX_SOL_DEBIT, simulate=True)
            prepared = preflight['plan']
            context = {'payout_intent': binding, 'before_accounts': preflight['before'],
                       'last_valid_block_height': pay.uint(latest['lastValidBlockHeight'], True)}
            fee = prepared['quoted_total_sol_debit_lamports']
        else:
            raise ValueError('Unsupported operator stage')
        return {'prepared': prepared, 'context': context, 'expires_at': now+60, 'fee_raw': str(fee)}

    def validate_before_submission(self, intent, stage, prepared, context):
        if stage in ('deposit','payout','refund'):
            blockhash = prepared.get('blockhash') or prepared.get('recent_blockhash')
            result = self.reader.solana('isBlockhashValid', [blockhash, {'commitment':'confirmed'}])
            require(type(result) is dict and result.get('value') is True, 'Source blockhash is no longer valid')
        else:
            _, last = self._xeris_hash()
            # Node latest validity is latest slot+150; compare actual context
            # slot separately, not wall-clock time or the new last-valid bound.
            latest = self.reader.xeris('getLatestBlockhash', [])
            slot = purchase.raw(latest.get('context', {}).get('slot'), positive=True)
            require(slot <= context['last_valid_block_height'], 'Xeris blockhash expired')
        return True

    def _route_facts(self, intent, stage, prepared, evidence):
        recovery = self.journal.recovery_items()
        require(recovery['active_route']['intent_id'] in (None, intent['intent_id']) or stage == 'burn',
                'Another route owns the operator wallet')
        pending = [s['transaction_id'] for s in recovery['stages']
            if not (s['intent_id'] == intent['intent_id'] and s['stage'] == stage)]
        # Burn inclusion is deterministic and does not require user-wallet
        # exclusivity. Swap accounting requires the actual isolated operator.
        return {**evidence, 'route_id': intent['intent_id'], 'operator_wallet': prepared['operator_wallet'],
            'exclusive_wallet': prepared['operator_wallet'] == self.operator_wallet and
                recovery['active_route']['intent_id'] == intent['intent_id'],
            'pending_transaction_ids': pending}

    def observe(self, intent, stage, prepared, transaction_id, context):
        self._bind(intent)
        if stage in ('deposit','payout','refund'):
            genesis = self.reader.solana('getGenesisHash')
            receipt = self.reader.solana('getTransaction', [transaction_id,
                {'encoding':'json','commitment':'finalized','maxSupportedTransactionVersion':0}])
            statuses = self.reader.solana('getSignatureStatuses', [[transaction_id], {'searchTransactionHistory':True}])
            evidence = {'signature': transaction_id, 'genesis_hash': genesis,
                        'transaction': receipt, 'signature_status': statuses}
            if receipt is None:
                return {'state':'pending','evidence_digest':digest(b'Solana receipt not observed')}
            require(genesis == sol.SOLANA_GENESIS, 'Wrong Solana network')
            require(receipt.get('transaction', {}).get('signatures') == [transaction_id] and
                compiled_message_bytes(receipt['transaction']['message']) == base64.b64decode(prepared['message_base64']),
                'Observed Solana wire differs from persisted original')
            rows = statuses.get('value') if type(statuses) is dict else None
            require(type(rows) is list and len(rows) == 1, 'Ambiguous Solana signature status')
            status = rows[0]
            error = receipt.get('meta', {}).get('err')
            if error is not None:
                # Only an explicit finalized instruction failure of this exact
                # original can end a source. Unknown error shapes remain pending
                # at runtime, never an inferred no-funds/cancellation outcome.
                failure = error.get('InstructionError') if type(error) is dict and set(error) == {'InstructionError'} else None
                require(type(failure) is list and len(failure) == 2 and type(failure[0]) is int and
                    0 <= failure[0] < len(receipt['transaction']['message']['instructions']), 'Unknown Solana failure shape')
                detail = failure[1]
                require((type(detail) is str and 1 <= len(detail) <= 128 and detail.isascii()) or
                    (type(detail) is dict and set(detail) == {'Custom'} and type(detail['Custom']) is int and
                     0 <= detail['Custom'] < 2**32), 'Unknown Solana instruction failure')
                slot = pay.uint(receipt.get('slot'), True)
                require(type(status) is dict and status.get('confirmationStatus') == 'finalized' and
                    status.get('confirmations', -1) is None and status.get('err') == error and
                    type(status.get('slot')) is int and status['slot'] == slot and
                    pay.uint(statuses.get('context', {}).get('slot'), True) >= slot,
                    'Failure is not independently finalized')
                require(receipt.get('version') in ('legacy',None), 'Unsupported failed transaction version')
                return {'state':'failed','evidence_digest':self._save(evidence)}
            if stage == 'deposit':
                request = {'source_owner':intent['source_owner'],'source_token_account':intent['solana_token_account'],
                    'amount_raw':intent['amount_raw'],'not_before':intent['created_at'],
                    'expires_at':intent['expires_at'],'min_slot':context['min_slot'], 'intent_digest':prepared['intent_digest']}
                late = type(receipt.get('blockTime')) is int and receipt['blockTime'] > intent['expires_at']
                if late:
                    request['expires_at'] = None  # Verify actual late funds, refund only.
                result = sol.verify_deposit(request, evidence, now=self.clock())
                return {'state':'confirmed','amount_raw':result['amount_raw'],'refund_required':late,
                        'evidence_digest':self._save({'evidence':evidence,'result':result,'refund_required':late})}
            binding = context['payout_intent']
            result = pay.verify_receipt(receipt, transaction_id, prepared, binding,
                finality={'genesis_hash':genesis,'signature_status':status})
            accounts = self._solana_accounts([pay.MULTISIG, prepared['proposal_pda'], pay.SOURCE,
                prepared['destination_token_account']], minContextSlot=result['slot'])
            verified = pay.verify_poststate(accounts, prepared, binding, result)
            return {'state':'confirmed','amount_raw':prepared['amount_raw'],
                    'evidence_digest':self._save({'evidence':evidence,'poststate':accounts,'result':verified})}
        before = context['before']
        self.chain.collect()
        after = self.chain.anchor() if stage == 'burn' else self.chain.snapshot_wallet(self.operator_wallet)
        evidence = self.chain.evidence(before, after, prepared, transaction_id, target_only=stage=='burn')
        receipt = evidence['target_receipt']
        evidence = self._route_facts(intent, stage, prepared, evidence)
        if receipt.get('meta', {}).get('err') is not None:
            result = accounting.verify_failed_original(before, after, prepared, receipt, transaction_id, evidence)
            return {'state':'failed','evidence_digest':self._save({'result':result,'before':before,'after':after,'evidence':evidence})}
        if stage == 'burn':
            binding = {'route_id':intent['intent_id'],'route_kind':'xusdc','source_wallet':intent['source_owner'],
                'solana_recipient':intent['destination_owner'],'burn_amount_raw':intent['amount_raw'],
                'signed_message_sha256':prepared['message_sha256'],'burn_signature':transaction_id,
                'authenticated_before_submission':True}
            result = redemption.verify_burn_canonical(binding, prepared, receipt, transaction_id, evidence, before, after)
            amount = intent['amount_raw']
        else:
            result = purchase.verify_accounted_transition(before, after, prepared, receipt, transaction_id, evidence)
            amount = str(result['actual_output_xrs_raw']) if stage == 'buy' else prepared['fields']['amount_raw']
        return {'state':'confirmed','amount_raw':amount,
                'evidence_digest':self._save({'result':result,'before':before,'after':after,'evidence':evidence})}
