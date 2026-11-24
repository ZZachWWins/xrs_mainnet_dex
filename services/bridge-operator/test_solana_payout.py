"""Public synthetic fixtures and independent Rust builder; no keys or network."""
import base64
import copy
import json
from pathlib import Path
import subprocess
import unittest
import solana_payout as p

BINARY=Path(__file__).resolve().parent/'solana-payout/target/debug/xeris-mainnet-unsigned-payout'
OWNER=p.b58(bytes([8])*32)
SIGNATURE=p.b58(bytes([9])*64)
BLOCKHASH=p.b58(bytes([18])*32)


def plan(owner=OWNER):
    frozen=json.loads((Path(__file__).parent/'fixtures/unsigned-payout-sdk.json').read_text())['plans'][owner]
    if BINARY.is_file():
        value=json.loads(subprocess.check_output([str(BINARY),BLOCKHASH,'500000','8',owner],text=True))
        if value!=frozen:raise ValueError('Unsigned SDK fixture differs from the independent local builder')
    else:
        value=frozen
    intent={'intent_id':'offline-intent','amount_raw':'500000','transaction_index':8,'destination_owner':owner}
    return value,intent


def account(owner,data,lamports=2039280):
    return {'owner':owner,'executable':False,'lamports':lamports,'data':[base64.b64encode(data).decode(),'base64']}


def token(owner,amount):
    data=bytearray(165);data[:32]=p.pub(p.USDC);data[32:64]=p.pub(owner);data[64:72]=amount.to_bytes(8,'little');data[108]=1
    return account(p.TOKEN,data)


def multisig(index=7):
    data=(p.hashlib.sha256(b'account:Multisig').digest()[:8]+p.pub(p.CREATION_SEED)+bytes(32)+bytes([1,0])+bytes(4)
        +index.to_bytes(8,'little')+bytes(8)+bytes([1])+p.pub(p.FUNDING)+bytes([255])+(1).to_bytes(4,'little')+p.pub(p.FUNDING)+bytes([7]))
    return account(p.PROGRAM,data)


def proposal(plan):
    data=bytearray(plan['proposal_space']);data[:8]=p.hashlib.sha256(b'account:Proposal').digest()[:8]
    data[8:40]=p.pub(p.MULTISIG);data[40:48]=plan['transaction_index'].to_bytes(8,'little');data[48]=5;data[57]=plan['bumps']['proposal']
    data[58:62]=(1).to_bytes(4,'little');data[62:94]=p.pub(p.FUNDING)
    return account(p.PROGRAM,data,plan['quoted_rent_proposal_lamports'])


def message(plan):
    reader=p.Reader(base64.b64decode(plan['message_base64']));a,b,c=reader.take(3);keys=[p.b58(reader.take(32)) for _ in range(reader.short())]
    blockhash=p.b58(reader.take(32));instructions=[]
    for _ in range(reader.short()):
        index=reader.num(1);accounts=list(reader.take(reader.short()));data=reader.take(reader.short())
        instructions.append({'programIdIndex':index,'accounts':accounts,'data':p.b58(data)})
    reader.done()
    return {'header':{'numRequiredSignatures':a,'numReadonlySignedAccounts':b,'numReadonlyUnsignedAccounts':c},'accountKeys':keys,'recentBlockhash':blockhash,'instructions':instructions}


def fixture(created=True,owner=OWNER):
    value,intent=plan(owner)
    value.update(destination_created=created,quoted_rent_transaction_lamports=3_000_000,
                 quoted_rent_proposal_lamports=2_000_000,quoted_rent_destination_lamports=2_039_280 if created else 0,
                 quoted_network_fee_lamports=5000,max_checked_sol_debit_lamports=10_000_000)
    value['quoted_total_sol_debit_lamports']=5_005_000+value['quoted_rent_destination_lamports']
    msg=message(value);keys=msg['accountKeys'];dest=value['destination_token_account']
    def ix(parts):
        program,accounts,data=parts
        return {'programIdIndex':keys.index(program),'accounts':[keys.index(k) for k in accounts],'data':p.b58(data),'stackHeight':2}
    groups=[]
    if created:
        group=[(p.TOKEN,[p.USDC],bytes([21,7,0])),p._create_account(p.FUNDING,dest,2_039_280,165,p.TOKEN),
               (p.TOKEN,[dest],bytes([22])),(p.TOKEN,[dest,p.USDC],bytes([18])+p.pub(owner))]
        groups.append({'index':0,'instructions':[ix(item) for item in group]})
    groups += [{'index':1,'instructions':[ix(p._create_account(p.FUNDING,value['transaction_pda'],3_000_000,value['transaction_space'],p.PROGRAM))]},
               {'index':2,'instructions':[ix(p._create_account(p.FUNDING,value['proposal_pda'],2_000_000,value['proposal_space'],p.PROGRAM))]},
               {'index':4,'instructions':[ix((p.TOKEN,[p.SOURCE,p.USDC,dest,p.VAULT],b'\x0c'+(500000).to_bytes(8,'little')+b'\x06'))]}]
    def balance(address,owner,amount):
        return {'accountIndex':keys.index(address),'mint':p.USDC,'owner':owner,'programId':p.TOKEN,'uiTokenAmount':{'amount':str(amount),'decimals':6}}
    pre=[100_000_000 if key==p.FUNDING else 0 for key in keys];post=pre.copy()
    post[keys.index(p.FUNDING)]-=value['quoted_total_sol_debit_lamports'];post[keys.index(value['transaction_pda'])]=3_000_000;post[keys.index(value['proposal_pda'])]=2_000_000
    if created:post[keys.index(dest)]=2_039_280
    else:pre[keys.index(dest)]=post[keys.index(dest)]=2_039_280
    pre_tokens=[balance(p.SOURCE,p.VAULT,5_000_000)]+([] if created else [balance(dest,owner,700)])
    post_tokens=[balance(p.SOURCE,p.VAULT,4_500_000),balance(dest,owner,500000+(0 if created else 700))]
    result={'slot':1000,'version':'legacy','transaction':{'signatures':[SIGNATURE],'message':msg},
            'meta':{'err':None,'fee':5000,'innerInstructions':groups,'preTokenBalances':pre_tokens,'postTokenBalances':post_tokens,'preBalances':pre,'postBalances':post}}
    finality={'genesis_hash':p.sv.SOLANA_GENESIS,'signature_status':{'slot':1000,'confirmationStatus':'finalized','confirmations':None,'err':None}}
    return value,intent,result,finality


class PayoutTests(unittest.TestCase):
    def test_rust_builder_and_python_decoder_agree_for_arbitrary_and_member_recipients(self):
        for owner in (OWNER,p.FUNDING,p.b58(bytes([7])*32)):
            value,intent=plan(owner);review=p.review_plan(value,intent)
            self.assertEqual(review['destination_token_account'],p.sv.associated_token_address(owner))
            self.assertEqual(review['required_signers'],[p.FUNDING]);self.assertLessEqual(review['packet_bytes'],1232)
            self.assertEqual(base64.b64decode(value['unsigned_transaction_base64'])[:65],b'\1'+bytes(64))
            self.assertEqual(len(value['instructions']),5)

    def test_raw_amount_and_intent_mutations_fail_before_review(self):
        for mutate in (lambda i:i.update(destination_owner=p.FUNDING),lambda i:i.update(amount_raw='500001'),lambda i:i.update(transaction_index=9),lambda i:i.update(transaction_index=True)):
            value,intent=plan();mutate(intent)
            with self.assertRaises(ValueError):p.review_plan(value,intent)
        for amount in ('0','01','1.0',True,str(1<<64)):
            value,intent=plan();value['amount_raw']=intent['amount_raw']=amount
            with self.assertRaises(ValueError):p.review_plan(value,intent)

    def test_forged_pda_metadata_or_wire_and_signatures_rejected(self):
        for field,value in [('transaction_pda',OWNER),('destination_authority',p.FUNDING),('real_signatures',True),('ata_space',True)]:
            prepared,intent=plan();prepared[field]=value
            with self.assertRaises(ValueError):p.review_plan(prepared,intent)
        prepared,intent=plan();wire=bytearray(base64.b64decode(prepared['unsigned_transaction_base64']));wire[1]=1;prepared['unsigned_transaction_base64']=base64.b64encode(wire).decode()
        with self.assertRaises(ValueError):p.review_plan(prepared,intent)

    def test_new_and_existing_ata_receipts_have_exact_one_intended_transfer(self):
        for created in (True,False):
            value,intent,result,finality=fixture(created)
            verified=p.verify_receipt(result,SIGNATURE,value,intent,finality=finality)
            self.assertEqual(verified['instruction_locator'],{'outer_index':4,'inner_index':0})
            self.assertEqual(verified['recipient_after_raw'],str(500000+(0 if created else 700)))
            self.assertFalse(verified['additional_payout_authorized'])

    def test_finality_genesis_status_slot_and_failed_transaction_rejected(self):
        for mutate in (lambda f:f.update(genesis_hash='wrong'),lambda f:f['signature_status'].update(confirmationStatus='confirmed'),lambda f:f['signature_status'].update(slot=999),lambda f:f['signature_status'].update(err='failure')):
            value,intent,result,finality=fixture();mutate(finality)
            with self.assertRaises(ValueError):p.verify_receipt(result,SIGNATURE,value,intent,finality=finality)
        value,intent,result,finality=fixture();result['meta']['err']={'InstructionError':[4,'Custom']}
        with self.assertRaises(ValueError):p.verify_receipt(result,SIGNATURE,value,intent,finality=finality)

    def test_extra_transfer_wrong_recipient_amount_and_initialization_cpi_rejected(self):
        for mutate in (lambda m:m['innerInstructions'][-1]['instructions'].append(copy.deepcopy(m['innerInstructions'][-1]['instructions'][0])),
            lambda m:m['innerInstructions'][-1]['instructions'][0].update(data=p.b58(b'\x0c'+(500001).to_bytes(8,'little')+b'\x06')),
            lambda m:m['innerInstructions'][0]['instructions'][-1].update(data=p.b58(b'\x12'+p.pub(p.FUNDING))),
            lambda m:m['innerInstructions'][-1]['instructions'][0].update(stackHeight=True)):
            value,intent,result,finality=fixture();mutate(result['meta'])
            with self.assertRaises(ValueError):p.verify_receipt(result,SIGNATURE,value,intent,finality=finality)

    def test_rent_fee_token_delta_and_preexisting_account_mismatch_rejected(self):
        for mutate in (lambda v,m:m.update(fee=True),lambda v,m:m['postBalances'].__setitem__(0,m['postBalances'][0]-1),
            lambda v,m:m['postTokenBalances'][-1]['uiTokenAmount'].update(amount='500001'),
            lambda v,m:m['preTokenBalances'].append(copy.deepcopy(m['preTokenBalances'][0])),
            lambda v,m:v.update(max_checked_sol_debit_lamports=1000)):
            value,intent,result,finality=fixture();mutate(value,result['meta'])
            with self.assertRaises(ValueError):p.verify_receipt(result,SIGNATURE,value,intent,finality=finality)

    def test_exact_signed_message_not_only_signature_or_summary(self):
        value,intent,result,finality=fixture();result['transaction']['message']['recentBlockhash']=p.b58(bytes([19])*32)
        with self.assertRaises(ValueError):p.verify_receipt(result,SIGNATURE,value,intent,finality=finality)

    def test_pinned_multisig_rejects_changed_threshold_member_lock_and_permissions(self):
        self.assertEqual(p.decode_multisig(multisig())['transaction_index'],7)
        for index in (40,72,74,94,95,127,128,132,164):
            value=multisig();data=bytearray(base64.b64decode(value['data'][0]));data[index]^=1;value['data'][0]=base64.b64encode(data).decode()
            with self.assertRaises(ValueError):p.decode_multisig(value)

    def test_poststate_requires_executed_proposal_and_pinned_reserve(self):
        value,intent,result,finality=fixture();verified=p.verify_receipt(result,SIGNATURE,value,intent,finality=finality)
        response={'context':{'slot':1000},'value':[multisig(8),proposal(value),token(p.VAULT,4_500_000),token(OWNER,500_000)]}
        self.assertEqual(p.verify_poststate(response,value,intent,verified)['proposal_status'],'Executed')
        data=bytearray(base64.b64decode(response['value'][1]['data'][0]));data[48]=1;response['value'][1]['data'][0]=base64.b64encode(data).decode()
        with self.assertRaises(ValueError):p.verify_poststate(response,value,intent,verified)

    def test_recipient_activity_does_not_undo_exact_finalized_delivery(self):
        value,intent,result,finality=fixture();verified=p.verify_receipt(result,SIGNATURE,value,intent,finality=finality)
        changed=token(OWNER,0)
        data=bytearray(base64.b64decode(changed['data'][0]))
        data[32:64]=p.pub(p.FUNDING);data[72:76]=(1).to_bytes(4,'little')
        changed['data'][0]=base64.b64encode(data).decode()
        for destination in (token(OWNER,499999),token(OWNER,500001),None,changed):
            with self.subTest(destination_present=destination is not None):
                response={'context':{'slot':1001},'value':[multisig(8),proposal(value),token(p.VAULT,4500001),destination]}
                checked=p.verify_poststate(response,value,intent,verified)
                self.assertEqual(checked['delivery_evidence'],'exact_finalized_transaction_receipt')
                self.assertFalse(checked['additional_payout_authorized'])
        response={'context':{'slot':999},'value':[multisig(8),proposal(value),token(p.VAULT,4500000),None]}
        with self.assertRaises(ValueError):p.verify_poststate(response,value,intent,verified)
        response['context']['slot']=1001;response['value'][2]=token(OWNER,4500000)
        with self.assertRaises(ValueError):p.verify_poststate(response,value,intent,verified)
        response['value'][2]=token(p.VAULT,4500000)
        changed_receipt={**verified,'amount_raw':'500001'}
        with self.assertRaises(ValueError):p.verify_poststate(response,value,intent,changed_receipt)

    def test_mutable_account_tolerance_does_not_relax_historical_delivery_checks(self):
        for mutate in (lambda m:m['postTokenBalances'][-1].update(owner=p.FUNDING),
                       lambda m:m['postTokenBalances'][-1].update(mint=p.FUNDING),
                       lambda m:m['postTokenBalances'][-1]['uiTokenAmount'].update(amount='499999')):
            value,intent,result,finality=fixture();mutate(result['meta'])
            with self.assertRaises(ValueError):p.verify_receipt(result,SIGNATURE,value,intent,finality=finality)

    def test_simulation_parsed_cpi_allowlist_keeps_exact_units_and_authority(self):
        ix={'programId':p.TOKEN,'stackHeight':2,'parsed':{'type':'transferChecked','info':{
            'source':p.SOURCE,'mint':p.USDC,'destination':p.sv.associated_token_address(OWNER),
            'authority':p.VAULT,'tokenAmount':{'amount':'500000','decimals':6,'uiAmount':0.5}}}}
        parsed=p._parsed_inner(ix)
        self.assertEqual(parsed,(p.TOKEN,[p.SOURCE,p.USDC,p.sv.associated_token_address(OWNER),p.VAULT],b'\x0c'+(500000).to_bytes(8,'little')+b'\x06'))
        for mutate in (lambda value:value['parsed'].update(type='transfer'),
                       lambda value:value.update(programId=p.ATA),
                       lambda value:value['parsed']['info']['tokenAmount'].update(decimals=True),
                       lambda value:value['parsed']['info']['tokenAmount'].update(amount=500000)):
            changed=copy.deepcopy(ix);mutate(changed)
            with self.assertRaises(ValueError):p._parsed_inner(changed)

    def test_preflight_only_uses_read_methods_and_accounts_new_ata_rent(self):
        value,intent=plan();calls=[]
        mint=bytearray(82);mint[44:46]=bytes([6,1])
        before=[multisig(),None,None,token(p.VAULT,5_000_000),None,account(p.TOKEN,mint),account(p.SYSTEM,b'',100_000_000),None]
        def rpc(method,params):
            calls.append((method,params))
            if method=='getGenesisHash':return p.sv.SOLANA_GENESIS
            if method=='isBlockhashValid':return {'value':True}
            if method=='getMultipleAccounts':return {'context':{'slot':1000},'value':before}
            if method=='getMinimumBalanceForRentExemption':return {value['transaction_space']:3_000_000,value['proposal_space']:2_000_000,165:2_039_280}[params[0]]
            if method=='getFeeForMessage':return {'value':5000}
            raise AssertionError('Unexpected RPC method '+method)
        checked=p.preflight(value,intent,rpc,simulate=False)
        self.assertEqual(checked['plan']['quoted_total_sol_debit_lamports'],7_044_280)
        self.assertTrue(checked['plan']['destination_created']);self.assertFalse(checked['real_signing_authorized'])
        self.assertTrue(all(method in {'getGenesisHash','isBlockhashValid','getMultipleAccounts','getMinimumBalanceForRentExemption','getFeeForMessage'} for method,_ in calls))
        with self.assertRaises(ValueError):p.preflight(value,intent,rpc,max_sol_debit_lamports=1000,simulate=False)
        before[0]=multisig(8)
        with self.assertRaises(ValueError):p.preflight(value,intent,rpc,simulate=False)


if __name__=='__main__':unittest.main()
