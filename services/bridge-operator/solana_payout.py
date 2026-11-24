"""Intent-bound unsigned Squads payout review and public evidence verification.
No keys, signing, submission or network transport. Public reads/simulation are
possible only through the caller-supplied allowlisted RPC callback. Freeze the
returned plan, authenticate the immutable intent, and serialize reserve actions
in a durable journal. A verified receipt never authorizes another payout.
"""
import base64
import copy
import hashlib
import json
import re
import solana_verify as sv

PROGRAM='SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf'
MULTISIG='bHNiYFGPMqNvTMNxBdngKskVFGbzzgFvFRx1kmynLEU'
VAULT=sv.RESERVE_OWNER
SOURCE=sv.RESERVE_TOKEN_ACCOUNT
FUNDING='BzyrcS63PR4kxt8WAbtCptsbN1czdSsyTt4AaaWJpKTA'
CREATION_SEED='2z5hTrp38oFVzpTx3ZhUNYAhkFjtYkexX98xFukpi8Cs'
USDC=sv.USDC_MINT
TOKEN=sv.TOKEN_PROGRAM
ATA=sv.ATA_PROGRAM
SYSTEM='1'*32
MAX_SOL_DEBIT=20_000_000
NETWORK_FEE=5000
b58=sv.b58encode

def require(ok,message):
    if not ok: raise ValueError(message)

def pub(value): return sv.b58decode(value,32,nonzero=False)
def uint(value,positive=False): return sv._uint(value,'Invalid exact unsigned integer',positive=positive)
def raw(value,positive=False): return sv._raw(value,'Invalid exact raw amount',positive=positive)
def equal(actual,expected,message): require(json.dumps(actual,sort_keys=True,separators=(',',':'),allow_nan=False)==json.dumps(expected,sort_keys=True,separators=(',',':'),allow_nan=False),message)
def pda(seeds):
    require(len(seeds)<16 and all(type(s) is bytes and len(s)<=32 for s in seeds),'Invalid PDA seeds')
    for bump in range(255,0,-1):
        candidate=hashlib.sha256(b''.join(seeds)+bytes([bump])+pub(PROGRAM)+b'ProgramDerivedAddress').digest()
        if not sv._is_curve_point(candidate): return b58(candidate),bump
    raise ValueError('PDA derivation failed')

class Reader:
    def __init__(self,data):self.data=data;self.pos=0
    def take(self,n):
        require(n>=0 and self.pos+n<=len(self.data),'Truncated bytes')
        value=self.data[self.pos:self.pos+n];self.pos+=n;return value
    def num(self,n):return int.from_bytes(self.take(n),'little')
    def short(self):
        result=0
        for i in range(3):
            b=self.num(1);result|=(b&127)<<(7*i)
            if b<128:
                require(not(i>0 and b==0) and result<=65535,'Noncanonical shortvec');return result
        raise ValueError('Invalid shortvec')
    def done(self):require(self.pos==len(self.data),'Trailing bytes')

def review_plan(plan, intent):
    require(type(plan) is dict and type(intent) is dict, 'Plan and intent objects required')
    amount=raw(plan.get('amount_raw'),positive=True);index=uint(plan.get('transaction_index'),positive=True)
    require(type(intent.get('intent_id')) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',intent['intent_id']), 'Invalid intent ID')
    destination_owner=intent.get('destination_owner');pub(destination_owner)
    require(intent.get('amount_raw')==plan['amount_raw'] and type(intent.get('transaction_index')) is int and intent['transaction_index']==index, 'Payout differs from immutable intent')
    destination=sv.associated_token_address(destination_owner)
    tx,tx_bump=pda([b'multisig',pub(MULTISIG),b'transaction',index.to_bytes(8,'little')])
    proposal,proposal_bump=pda([b'multisig',pub(MULTISIG),b'transaction',index.to_bytes(8,'little'),b'proposal'])
    vault,vault_bump=pda([b'multisig',pub(MULTISIG),b'vault',b'\0'])
    require(vault==VAULT and sv.associated_token_address(VAULT)==SOURCE,'Pinned reserve derivation mismatch')
    require(destination_owner not in {SYSTEM,PROGRAM,TOKEN,USDC,ATA,MULTISIG,VAULT,SOURCE,tx,proposal} and destination not in {FUNDING,SYSTEM,PROGRAM,TOKEN,USDC,ATA,MULTISIG,VAULT,SOURCE,tx,proposal},'Reserved or overlapping recipient')
    expected={'multisig':MULTISIG,'vault':VAULT,'vault_token_account':SOURCE,'usdc_mint':USDC,
        'destination_token_account':destination,'destination_owner':destination_owner,'destination_authority':destination_owner,
        'member':FUNDING,'ata_program':ATA,'ata_space':165,'creates_destination_ata_idempotently':True,
        'transaction_pda':tx,'proposal_pda':proposal,'decimals':6,'required_signer_public_keys':[FUNDING]}
    equal({key:plan.get(key) for key in expected},expected,'Plan metadata differs from intended payout')
    require(plan.get('private_keys_accessed') is False and type(plan.get('real_signatures')) is int and plan['real_signatures']==0 and type(plan.get('transactions_submitted')) is int and plan['transactions_submitted']==0,'Builder is not unsigned-only')
    data=base64.b64decode(plan['message_base64'],validate=True);r=Reader(data)
    signers,readonly_signers,readonly_unsigned=r.take(3)
    require(signers==1 and readonly_signers==0,'Unexpected signing header')
    keys=[b58(r.take(32)) for _ in range(r.short())]
    require(len(keys)==len(set(keys)) and keys[0]==FUNDING,'Duplicate keys or wrong fee payer')
    require(set(keys)=={FUNDING,MULTISIG,tx,proposal,SOURCE,destination,VAULT,SYSTEM,TOKEN,USDC,PROGRAM,ATA,destination_owner},'Unexpected outer account')
    writable=set(keys[:len(keys)-readonly_unsigned])
    require(writable=={FUNDING,MULTISIG,tx,proposal,SOURCE,destination,VAULT},'Unexpected writable accounts')
    hash_offset=r.pos;blockhash=r.take(32);require(blockhash!=bytes(32) and b58(blockhash)==plan.get('recent_blockhash'),'Wrong recent blockhash')
    instructions=[]
    for _ in range(r.short()):
        program_index=r.num(1);accounts=list(r.take(r.short()));payload=r.take(r.short())
        require(program_index<len(keys) and all(a<len(keys)for a in accounts),'Invalid account index')
        instructions.append((keys[program_index],[keys[a]for a in accounts],payload))
    r.done();require(len(instructions)==5,'Payout must have exactly five outer instructions')
    require(instructions[0]==(ATA,[FUNDING,destination,destination_owner,USDC,SYSTEM,TOKEN],b'\1'),'Wrong idempotent ATA creation')
    instructions=instructions[1:]
    expected_accounts=[[MULTISIG,tx,FUNDING,FUNDING,SYSTEM],[MULTISIG,proposal,FUNDING,FUNDING,SYSTEM],[MULTISIG,FUNDING,proposal]]
    for i in range(3):require(instructions[i][0]==PROGRAM and instructions[i][1]==expected_accounts[i],'Wrong outer instruction accounts')
    create=Reader(instructions[0][2]);require(create.take(8)==bytes([48,250,78,168,208,226,218,211]),'Wrong create discriminator')
    require(create.take(2)==bytes(2),'Vault index/ephemeral signers changed')
    compact=create.take(create.num(4));require(create.take(1)==b'\0','Unexpected create memo');create.done()
    inner=Reader(compact);require(inner.take(3)==bytes([1,1,2]),'Wrong inner account privileges')
    inner_keys=[b58(inner.take(32))for _ in range(inner.num(1))]
    require(len(inner_keys)==5 and inner_keys[0]==VAULT and set(inner_keys[1:3])=={SOURCE,destination} and set(inner_keys[3:])=={TOKEN,USDC},'Wrong inner account layout')
    require(inner.num(1)==1,'Only one inner instruction allowed')
    program_index=inner.num(1);accounts=list(inner.take(inner.num(1)));transfer=inner.take(inner.num(2))
    require(program_index<len(inner_keys) and all(a<len(inner_keys)for a in accounts),'Wrong inner indices')
    require(inner_keys[program_index]==TOKEN and [inner_keys[a]for a in accounts]==[SOURCE,USDC,destination,VAULT],'Wrong token transfer accounts')
    require(transfer==bytes([12])+amount.to_bytes(8,'little')+bytes([6]),'Transfer amount/mint decimals/opcode changed')
    require(inner.take(1)==b'\0','Lookup tables forbidden');inner.done()
    require(instructions[1][2]==bytes([220,60,73,224,30,108,79,159])+index.to_bytes(8,'little')+b'\0','Proposal index/draft changed')
    require(instructions[2][2]==bytes([144,37,164,136,188,216,42,248,0]),'Approval payload changed')
    require(instructions[3]==(PROGRAM,[MULTISIG,proposal,tx,FUNDING]+inner_keys,bytes([194,8,161,87,153,164,25,171])),'Execution accounts/payload changed')
    template=bytearray(data);template[hash_offset:hash_offset+32]=bytes(32)
    wire=base64.b64decode(plan['unsigned_transaction_base64'],validate=True)
    require(wire==b'\1'+bytes(64)+data and len(wire)<=1232,'Wrong unsigned envelope')
    require(base64.b64decode(plan['message_template_base64'],validate=True)==bytes(template),'Template mismatch')
    stored_message_size=3+4+32*5+4+1+4+4+4+10+4
    require(plan.get('transaction_space')==8+32+32+8+1+1+1+4+stored_message_size and plan.get('proposal_space')==8+32+8+1+8+1+3*(4+32),'Wrong account rent spaces')
    require(plan.get('transaction_bytes')==len(wire),'Incorrect packet size metadata')
    require(plan.get('bumps',{}).get('transaction')==tx_bump and plan['bumps'].get('proposal')==proposal_bump and plan['bumps'].get('vault')==vault_bump,'Wrong PDA bump metadata')
    return {'intent_id':intent['intent_id'],'destination_owner':destination_owner,'destination_token_account':destination,'transaction_pda':tx,'proposal_pda':proposal,'status':'INDEPENDENT_PUBLIC_WIRE_REVIEW_PASSED','amount_raw':str(amount),'transaction_index':index,'packet_bytes':len(wire),'required_signers':[FUNDING],
            'stable_template_sha256':hashlib.sha256(template).hexdigest(),'message_sha256':hashlib.sha256(data).hexdigest(),'blockhash_offset':hash_offset,'private_keys_accessed':False}


def account_bytes(account, owner, length):
    require(type(account) is dict and account.get('owner')==owner and account.get('executable') is False,'Wrong account owner/executable flag')
    uint(account.get('lamports'))
    data=account.get('data');require(type(data) is list and len(data)==2 and data[1]=='base64','Expected base64 account data')
    require(type(data[0]) is str and len(data[0])<=4096,'Account data exceeds bound')
    decoded=base64.b64decode(data[0],validate=True);require(len(decoded)==length,'Wrong account size')
    return decoded


def decode_multisig(account):
    data=account_bytes(account,PROGRAM,165)
    require(data[:8]==hashlib.sha256(b'account:Multisig').digest()[:8],'Wrong multisig discriminator')
    current=int.from_bytes(data[78:86],'little');stale=int.from_bytes(data[86:94],'little')
    expected=(hashlib.sha256(b'account:Multisig').digest()[:8]+pub(CREATION_SEED)+bytes(32)+bytes([1,0])+bytes(4)
        +data[78:94]+bytes([1])+pub(FUNDING)+bytes([255])+(1).to_bytes(4,'little')+pub(FUNDING)+bytes([7]))
    require(data==expected and stale<=current,'Pinned 1-of-1 Squads configuration changed')
    return {'transaction_index':current,'stale_transaction_index':stale,'threshold':1,'time_lock':0,'member':FUNDING}


def verify_ata(account, owner, amount=None):
    data=account_bytes(account,TOKEN,165)
    require(data[:32]==pub(USDC) and data[32:64]==pub(owner),'Wrong token mint/authority')
    require(data[72:76]==bytes(4) and data[108]==1 and data[109:113]==bytes(4) and data[129:133]==bytes(4),'Unexpected delegate/frozen/native/close-authority token state')
    value=int.from_bytes(data[64:72],'little')
    require(amount is None or value==uint(amount),'Token balance differs from expected exact amount')
    return value


def verify_proposal(account,plan):
    data=account_bytes(account,PROGRAM,plan['proposal_space'])
    require(data[:8]==hashlib.sha256(b'account:Proposal').digest()[:8] and data[8:40]==pub(MULTISIG)
        and int.from_bytes(data[40:48],'little')==plan['transaction_index'],'Wrong proposal identity')
    require(data[48]==5 and data[57]==plan['bumps']['proposal'],'Proposal was not executed or bump changed')
    require(data[58:62]==(1).to_bytes(4,'little') and data[62:94]==pub(FUNDING) and data[94:102]==bytes(8),'Unexpected approval/rejection/cancellation list')
    require(data[102:]==bytes(len(data)-102),'Unexpected proposal trailing state')
    return {'status':'Executed','transaction_index':plan['transaction_index']}


def _costs(plan):
    tx=uint(plan.get('quoted_rent_transaction_lamports'),positive=True)
    proposal=uint(plan.get('quoted_rent_proposal_lamports'),positive=True)
    ata=uint(plan.get('quoted_rent_destination_lamports'))
    require(type(plan.get('destination_created')) is bool,'ATA existence flag required')
    require((ata>0)==plan['destination_created'],'ATA rent/existence mismatch')
    require(type(plan.get('quoted_network_fee_lamports')) is int and plan['quoted_network_fee_lamports']==NETWORK_FEE,'Unexpected network fee')
    maximum=uint(plan.get('max_checked_sol_debit_lamports'),positive=True)
    total=uint(plan.get('quoted_total_sol_debit_lamports'),positive=True)
    require(total==tx+proposal+ata+NETWORK_FEE and total<=maximum<=MAX_SOL_DEBIT,'Payout rent/fee exceeds exact checked ceiling')
    return tx,proposal,ata,total


def _short(value):
    require(type(value) is int and 0<=value<=65535,'Invalid short vector')
    out=bytearray()
    while True:
        b=value&127;value>>=7;out.append(b|(128 if value else 0))
        if not value:return bytes(out)


def public_message_bytes(message):
    require(type(message) is dict,'Receipt message required')
    header=message.get('header');keys=message.get('accountKeys');instructions=message.get('instructions')
    require(type(header) is dict and type(keys) is list and 1<=len(keys)<=64 and type(instructions) is list and len(instructions)==5,'Unexpected legacy message shape')
    fields=[header.get(k) for k in ('numRequiredSignatures','numReadonlySignedAccounts','numReadonlyUnsignedAccounts')]
    require(all(type(v) is int and 0<=v<=255 for v in fields),'Invalid header integer')
    result=bytes(fields)+_short(len(keys))+b''.join(pub(key) for key in keys)+sv.b58decode(message.get('recentBlockhash'),32)+_short(len(instructions))
    for ix in instructions:
        require(type(ix) is dict,'Invalid compiled instruction')
        data=decode58_data(ix.get('data'));accounts=ix.get('accounts');index=ix.get('programIdIndex')
        require(type(index) is int and 0<=index<len(keys) and type(accounts) is list and all(type(a) is int and 0<=a<len(keys) for a in accounts),'Invalid instruction account index')
        result+=bytes([index])+_short(len(accounts))+bytes(accounts)+_short(len(data))+data
    return result


def decode58_data(value):
    require(type(value) is str and len(value)<=4096,'Invalid bounded instruction data')
    n=0
    for char in value:
        digit=sv.ALPHABET.find(char);require(digit>=0,'Invalid base58 instruction data');n=n*58+digit
    result=bytes(len(value)-len(value.lstrip('1')))+n.to_bytes((n.bit_length()+7)//8,'big')
    require(b58(result)==value,'Noncanonical base58 instruction data')
    return result


def _inner(ix,keys):
    require(type(ix) is dict and type(ix.get('programIdIndex')) is int and 0<=ix['programIdIndex']<len(keys),'Invalid CPI program index')
    accounts=ix.get('accounts');require(type(accounts) is list and all(type(i) is int and 0<=i<len(keys) for i in accounts),'Invalid CPI accounts')
    require(type(ix.get('stackHeight')) is int and ix['stackHeight']==2,'Unexpected CPI depth')
    return keys[ix['programIdIndex']],[keys[i] for i in accounts],decode58_data(ix.get('data'))


def _parsed_inner(ix):
    # simulateTransaction currently returns parsed CPI records even when json is
    # requested. Reconstruct only this exact allowlist for simulation; finalized
    # receipts still require raw compiled CPI bytes.
    require(type(ix) is dict and type(ix.get('stackHeight')) is int and ix['stackHeight']==2,'Unexpected parsed CPI depth')
    parsed=ix.get('parsed');require(type(parsed) is dict and set(parsed)=={'info','type'},'Invalid parsed CPI')
    info=parsed['info'];kind=parsed['type'];program=ix.get('programId')
    require(type(info) is dict,'Invalid parsed CPI fields')
    if kind=='createAccount':
        require(program==SYSTEM and set(info)=={'source','newAccount','lamports','space','owner'},'Unexpected parsed create-account')
        pub(info['source']);pub(info['newAccount']);pub(info['owner'])
        return _create_account(info['source'],info['newAccount'],uint(info['lamports'],True),uint(info['space'],True),info['owner'])
    require(program==TOKEN,'Unexpected parsed token program')
    if kind=='getAccountDataSize':
        require(set(info)=={'mint','extensionTypes'} and info['extensionTypes']==['immutableOwner'],'Unexpected account-size query')
        pub(info['mint']);return TOKEN,[info['mint']],bytes([21,7,0])
    if kind=='initializeImmutableOwner':
        require(set(info)=={'account'},'Unexpected immutable-owner initialization')
        pub(info['account']);return TOKEN,[info['account']],bytes([22])
    if kind=='initializeAccount3':
        require(set(info)=={'account','mint','owner'},'Unexpected account initialization')
        pub(info['account']);pub(info['mint']);return TOKEN,[info['account'],info['mint']],bytes([18])+pub(info['owner'])
    if kind=='transferChecked':
        require(set(info)=={'source','mint','destination','authority','tokenAmount'},'Unexpected parsed transfer')
        amount=info['tokenAmount'];require(type(amount) is dict and type(amount.get('decimals')) is int and amount['decimals']==6,'Unexpected transfer decimals')
        accounts=[info[k] for k in ('source','mint','destination','authority')]
        for key in accounts:pub(key)
        return TOKEN,accounts,b'\x0c'+raw(amount.get('amount'),True).to_bytes(8,'little')+b'\x06'
    raise ValueError('Unsupported parsed CPI type')


def _create_account(payer,new_account,rent,space,owner):
    return SYSTEM,[payer,new_account],bytes(4)+rent.to_bytes(8,'little')+space.to_bytes(8,'little')+pub(owner)


def verify_cpis(meta,keys,plan,*,allow_parsed=False):
    tx_rent,prop_rent,ata_rent,_=_costs(plan);destination=plan['destination_token_account']
    groups=meta.get('innerInstructions');require(type(groups) is list,'CPI groups missing')
    expected_indices=[0,1,2,4] if plan['destination_created'] else [1,2,4]
    require([g.get('index') for g in groups if type(g) is dict]==expected_indices and len(groups)==len(expected_indices),'Unexpected CPI group/order')
    expected={1:[_create_account(FUNDING,plan['transaction_pda'],tx_rent,plan['transaction_space'],PROGRAM)],
              2:[_create_account(FUNDING,plan['proposal_pda'],prop_rent,plan['proposal_space'],PROGRAM)],
              4:[(TOKEN,[SOURCE,USDC,destination,VAULT],b'\x0c'+raw(plan['amount_raw'],True).to_bytes(8,'little')+b'\x06')]}
    if plan['destination_created']:
        expected[0]=[(TOKEN,[USDC],bytes([21,7,0])),_create_account(FUNDING,destination,ata_rent,165,TOKEN),
                     (TOKEN,[destination],bytes([22])),(TOKEN,[destination,USDC],bytes([18])+pub(plan['destination_owner']))]
    for group in groups:
        instructions=group.get('instructions');require(type(instructions) is list,'Missing CPI instructions')
        actual=[_parsed_inner(ix) if allow_parsed and type(ix) is dict and 'parsed' in ix else _inner(ix,keys) for ix in instructions]
        require(actual==expected[group['index']],'Unexpected CPI; only exact ATA initialization and one intended TransferChecked allowed')
    return {'outer_index':4,'inner_index':0}


def _token_amount(meta,field,keys,address,owner,*,missing_zero=False):
    rows=meta.get(field);require(type(rows) is list,'Token balance metadata missing')
    matches=[v for v in rows if type(v) is dict and v.get('accountIndex')==keys.index(address)]
    if missing_zero and not matches:return 0
    require(len(matches)==1,'Missing/duplicate token balance')
    item=matches[0];ui=item.get('uiTokenAmount');require(type(ui) is dict,'Token amount metadata missing')
    require(type(item.get('accountIndex')) is int and item.get('mint')==USDC and item.get('owner')==owner
            and item.get('programId')==TOKEN and type(ui.get('decimals')) is int and ui['decimals']==6,'Wrong token balance metadata')
    return raw(ui.get('amount'))


def verify_receipt(receipt,signature,plan,intent,*,finality):
    """Validate finalized evidence contents; caller pins RPC/provenance and one-use journal.

    finality={genesis_hash, signature_status}, from getGenesisHash and
    getSignatureStatuses(searchTransactionHistory=true). No browser evidence.
    """
    reviewed=review_plan(plan,intent);sv.b58decode(signature,64)
    require(type(receipt) is dict and type(finality) is dict,'Receipt/finality evidence required')
    slot=uint(receipt.get('slot'),positive=True);status=finality.get('signature_status')
    require(finality.get('genesis_hash')==sv.SOLANA_GENESIS and type(status) is dict and type(status.get('slot')) is int
            and status['slot']==slot and status.get('confirmationStatus')=='finalized' and 'err' in status and status['err'] is None
            and 'confirmations' in status and status['confirmations'] is None,'Payout is not proven finalized on Solana mainnet')
    tx=receipt.get('transaction');meta=receipt.get('meta')
    require(type(tx) is dict and tx.get('signatures')==[signature] and type(meta) is dict,'Wrong receipt transaction')
    require('err' in meta and meta['err'] is None and type(meta.get('fee')) is int and meta['fee']==NETWORK_FEE,'Payout failed or fee changed')
    require(receipt.get('version','legacy')=='legacy','Unexpected transaction version')
    equal(meta.get('loadedAddresses',{'readonly':[],'writable':[]}),{'readonly':[],'writable':[]},'Address lookups forbidden')
    require(public_message_bytes(tx.get('message'))==base64.b64decode(plan['message_base64'],validate=True),'Finalized message differs from reviewed message')
    keys=tx['message']['accountKeys'];locator=verify_cpis(meta,keys,plan)
    destination=plan['destination_token_account'];owner=plan['destination_owner'];amount=raw(plan['amount_raw'],True)
    for field in ('preTokenBalances','postTokenBalances'):
        rows=meta.get(field);require(type(rows) is list,'Token balance evidence missing')
        indices=[v.get('accountIndex') for v in rows if type(v) is dict]
        expected_indices={keys.index(SOURCE),keys.index(destination)}
        if field=='preTokenBalances' and plan['destination_created']:expected_indices={keys.index(SOURCE)}
        require(len(indices)==len(rows)==len(expected_indices) and all(type(i) is int for i in indices) and set(indices)==expected_indices,'Unexpected or duplicate token balance account')
    pre_source=_token_amount(meta,'preTokenBalances',keys,SOURCE,VAULT);post_source=_token_amount(meta,'postTokenBalances',keys,SOURCE,VAULT)
    pre_dest=_token_amount(meta,'preTokenBalances',keys,destination,owner,missing_zero=plan['destination_created'])
    post_dest=_token_amount(meta,'postTokenBalances',keys,destination,owner)
    require(pre_source-post_source==amount and post_dest-pre_dest==amount,'USDC debit/credit mismatch')
    require(not plan['destination_created'] or pre_dest==0,'New ATA had preexisting tokens')
    pre,post=meta.get('preBalances'),meta.get('postBalances')
    require(type(pre) is list and type(post) is list and len(pre)==len(post)==len(keys),'Native balance array mismatch')
    tx_rent,prop_rent,ata_rent,total=_costs(plan)
    expected={FUNDING:-total,plan['transaction_pda']:tx_rent,plan['proposal_pda']:prop_rent,destination:ata_rent}
    for i,key in enumerate(keys):require(uint(post[i])-uint(pre[i])==expected.get(key,0),'Unexpected native balance delta')
    for key in (plan['transaction_pda'],plan['proposal_pda']):require(pre[keys.index(key)]==0,'Squads account already existed')
    if plan['destination_created']:require(pre[keys.index(destination)]==0,'New ATA had preexisting lamports')
    return {**reviewed,'status':'FINALIZED_PAYOUT_RECEIPT_VERIFIED','signature':signature,'slot':slot,
            'instruction_locator':locator,'source_before_raw':str(pre_source),'source_after_raw':str(post_source),
            'recipient_before_raw':str(pre_dest),'recipient_after_raw':str(post_dest),'sol_debit_lamports':total,
            'additional_payout_authorized':False}


def verify_poststate(accounts_response,plan,intent,receipt_result):
    """Finalized minContextSlot read order: multisig, proposal, reserve ATA, recipient ATA."""
    review_plan(plan,intent)
    require(type(accounts_response) is dict and uint(accounts_response.get('context',{}).get('slot'))>=receipt_result['slot'],'Poststate predates payout')
    values=accounts_response.get('value');require(type(values) is list and len(values)==4,'Wrong poststate account count')
    multisig,proposal,source,destination=values;config=decode_multisig(multisig)
    require(config['transaction_index']>=plan['transaction_index'],'Multisig index predates payout')
    verify_proposal(proposal,plan)
    verify_ata(source,VAULT,raw(receipt_result['source_after_raw']))
    verify_ata(destination,plan['destination_owner'],raw(receipt_result['recipient_after_raw']))
    return {'status':'PAYOUT_POSTSTATE_VERIFIED','intent_id':intent['intent_id'],'signature':receipt_result['signature'],
            'proposal_status':'Executed','account_context_slot':accounts_response['context']['slot'],'additional_payout_authorized':False}


def preflight(plan,intent,read_rpc,max_sol_debit_lamports=10_000_000,*,simulate=True):
    """Public-only caller-injected RPC; validates current reserve/config/rent budget.

    No transport is bundled and no signing/sending RPC method is called. The
    maximum is a client preflight ceiling, not an on-chain maximum SOL debit.
    Caller must refresh validity and serialize the Squads index before signing.
    """
    reviewed=review_plan(plan,intent);require(callable(read_rpc),'Public RPC callback required')
    maximum=uint(max_sol_debit_lamports,True);require(maximum<=MAX_SOL_DEBIT,'SOL debit ceiling exceeds service bound')
    require(read_rpc('getGenesisHash',[])==sv.SOLANA_GENESIS,'Wrong Solana network')
    validity=read_rpc('isBlockhashValid',[plan['recent_blockhash'],{'commitment':'confirmed'}])
    require(type(validity) is dict and validity.get('value') is True,'Blockhash expired/unavailable')
    addresses=[MULTISIG,plan['transaction_pda'],plan['proposal_pda'],SOURCE,plan['destination_token_account'],USDC,FUNDING,VAULT]
    before=read_rpc('getMultipleAccounts',[addresses,{'encoding':'base64','commitment':'finalized'}])
    require(type(before) is dict and type(before.get('value')) is list and len(before['value'])==8,'Incomplete preflight accounts')
    context=uint(before.get('context',{}).get('slot'),True)
    multisig,transaction,proposal,source,destination,mint,member,vault=before['value']
    config=decode_multisig(multisig);require(config['transaction_index']+1==plan['transaction_index'],'Squads index changed; reconcile or rebuild before signing')
    require(transaction is None and proposal is None,'Squads transaction/proposal already exists; reconcile')
    source_amount=verify_ata(source,VAULT);require(source_amount>=raw(plan['amount_raw'],True),'Insufficient reserve')
    destination_amount=0 if destination is None else verify_ata(destination,plan['destination_owner'])
    mint_data=account_bytes(mint,TOKEN,82);require(mint_data[44:46]==bytes([6,1]),'Wrong USDC mint state')
    require(type(member) is dict and member.get('owner')==SYSTEM and member.get('executable') is False and member.get('data')==['','base64'],'Member is not an ordinary system account')
    tx_rent=uint(read_rpc('getMinimumBalanceForRentExemption',[plan['transaction_space'],{'commitment':'confirmed'}]),True)
    prop_rent=uint(read_rpc('getMinimumBalanceForRentExemption',[plan['proposal_space'],{'commitment':'confirmed'}]),True)
    ata_rent=uint(read_rpc('getMinimumBalanceForRentExemption',[165,{'commitment':'confirmed'}]),True) if destination is None else 0
    fee=read_rpc('getFeeForMessage',[plan['message_base64'],{'commitment':'confirmed'}]);require(type(fee) is dict and type(fee.get('value')) is int and fee['value']==NETWORK_FEE,'Unexpected network fee')
    total=tx_rent+prop_rent+ata_rent+NETWORK_FEE
    require(total<=maximum and uint(member.get('lamports'))>=total,'Insufficient fee balance or rent/fee exceeds ceiling')
    checked=copy.deepcopy(plan);checked.update({'destination_created':destination is None,'quoted_rent_transaction_lamports':tx_rent,
        'quoted_rent_proposal_lamports':prop_rent,'quoted_rent_destination_lamports':ata_rent,'quoted_network_fee_lamports':NETWORK_FEE,
        'quoted_total_sol_debit_lamports':total,'max_checked_sol_debit_lamports':maximum,'message_sha256':reviewed['message_sha256'],
        'stable_template_sha256':reviewed['stable_template_sha256'],'observed_context_slot':context})
    _costs(checked)
    report={'status':'PUBLIC_PREFLIGHT_CHECKED','plan':checked,'before':before,'simulation':None,'real_signing_authorized':False}
    if simulate:
        simulation=read_rpc('simulateTransaction',[checked['unsigned_transaction_base64'],{'encoding':'base64','sigVerify':False,
            'replaceRecentBlockhash':True,'commitment':'confirmed','innerInstructions':True,'innerInstructionsEncoding':'json',
            'minContextSlot':context,'accounts':{'encoding':'base64','addresses':addresses}}])
        require(type(simulation) is dict and type(simulation.get('value')) is dict and simulation['value'].get('err') is None,'Unsigned payout simulation failed')
        value=simulation['value'];after=value.get('accounts');require(type(after) is list and len(after)==8,'Simulation account evidence missing')
        after_ms,after_tx,after_prop,after_source,after_dest,after_mint,after_member,after_vault=after
        expected_ms=bytearray(account_bytes(multisig,PROGRAM,165));expected_ms[78:86]=plan['transaction_index'].to_bytes(8,'little')
        require(account_bytes(after_ms,PROGRAM,165)==bytes(expected_ms) and after_ms['lamports']==multisig['lamports'],'Simulation changed reserve configuration')
        verify_proposal(after_prop,checked)
        tx_data=account_bytes(after_tx,PROGRAM,plan['transaction_space']);require(tx_data[:8]==hashlib.sha256(b'account:VaultTransaction').digest()[:8],'Wrong transaction account')
        require(after_tx['lamports']==tx_rent and after_prop['lamports']==prop_rent,'Simulation account rents changed')
        amount=raw(plan['amount_raw'],True);verify_ata(after_source,VAULT,source_amount-amount);verify_ata(after_dest,plan['destination_owner'],destination_amount+amount)
        require(after_source['lamports']==source['lamports'] and after_dest['lamports']==(ata_rent if destination is None else destination['lamports']),'Unexpected token account SOL change')
        equal(after_mint,mint,'Simulation changed USDC mint')
        def empty_native(value):
            return value is None or (type(value) is dict and type(value.get('lamports')) is int and value['lamports']==0 and value.get('owner')==SYSTEM and value.get('executable') is False and value.get('data')==['','base64'])
        require(after_vault==vault or (empty_native(after_vault) and empty_native(vault)),'Simulation changed native vault')
        require(type(after_member) is dict and after_member.get('owner')==SYSTEM and after_member.get('executable') is False and after_member.get('data')==['','base64']
                and uint(member['lamports'])-uint(after_member.get('lamports'))==total,'Simulation member debit/configuration mismatch')
        # Use instruction indices compiled in the reviewed message, not plan display metadata.
        reader=Reader(base64.b64decode(plan['message_base64']));reader.take(3);keys=[b58(reader.take(32)) for _ in range(reader.short())]
        verify_cpis(value,keys,checked,allow_parsed=True)
        report.update(status='UNSIGNED_PAYOUT_SIMULATION_VERIFIED',simulation=simulation)
    return report
