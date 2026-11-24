"""Canonical, unsigned Solana USDC deposit construction for a bound intent."""
import base64
import hashlib
import re
from solana_verify import (associated_token_address, b58decode, RESERVE_TOKEN_ACCOUNT,
                           USDC_MINT, TOKEN_PROGRAM, SOLANA_GENESIS, MEMO_PROGRAM, MEMO_PREFIX)


def build_deposit(source_owner, amount_raw, blockhash, *, intent_digest=None):
    if type(amount_raw) is not str or not amount_raw.isascii() or not amount_raw.isdigit():
        raise ValueError('invalid_deposit_amount')
    amount = int(amount_raw)
    if str(amount) != amount_raw or not 0 < amount < 2**64:
        raise ValueError('invalid_deposit_amount')
    source = associated_token_address(source_owner)
    keys = [source_owner, source, RESERVE_TOKEN_ACCOUNT, USDC_MINT, TOKEN_PROGRAM]
    if intent_digest is not None:
        if type(intent_digest) is not str or re.fullmatch('[0-9a-f]{64}', intent_digest) is None:
            raise ValueError('invalid_intent_digest')
        keys.append(MEMO_PROGRAM)
    if len(set(keys)) != len(keys):
        raise ValueError('overlapping_deposit_accounts')
    data = bytes([12]) + amount.to_bytes(8, 'little') + bytes([6])
    # Public settlement requires the Memo. Legacy construction is retained only
    # for historical fixture compatibility; runtime always supplies the digest.
    bound = intent_digest is not None
    message = (bytes([1, 0, 3 if bound else 2, len(keys)])
               + b''.join(b58decode(key, 32) for key in keys) + b58decode(blockhash, 32)
               + bytes([2 if bound else 1, 4, 4, 1, 3, 2, 0, len(data)]) + data)
    if bound:
        memo = (MEMO_PREFIX + intent_digest).encode('ascii')
        message += bytes([5, 1, 0, len(memo)]) + memo
    result = {'source_owner': source_owner, 'source_token_account': source,
            'destination_token_account': RESERVE_TOKEN_ACCOUNT, 'mint': USDC_MINT,
            'amount_raw': amount_raw, 'solana_genesis': SOLANA_GENESIS,
            'message_base64': base64.b64encode(message).decode(),
            'message_sha256': hashlib.sha256(message).hexdigest(),
            'unsigned_transaction_base64': base64.b64encode(b'\x01' + bytes(64) + message).decode(),
            'blockhash': blockhash, 'requires_wallet_approval': True}
    if bound:
        result['intent_digest'] = intent_digest
    return result
