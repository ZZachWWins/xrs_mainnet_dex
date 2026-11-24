"""Collect a bounded, same-tip read-only mainnet census. No signing or files."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import re
import time
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from aggregate import aggregate_supply, aggregate_token

CHAIN = 'xeris-mainnet-v4'
CHECKPOINT_SLOT = 27201
CHECKPOINT_HASH = '0a401c139eef73f049010450d4aa13ef38636799784d01bbc0ecdb9c71facaf3'
POOL = 'xrs-xusdc-mainnet-v1'
WRAPPED = 'xrs_native'
USDC = 'xeris_usdc'
U64 = 2**64 - 1
POOL_OWNER = 'HJBaUVbGbRm1JFt6kuC9koxvx2H5NuPsxt4cMuR315iX'
MAX_TIP_AGE_MS = 120_000


class CollectionError(Exception):
    pass


class IdentityError(CollectionError):
    pass


class TransientCollectionError(CollectionError):
    """Only a forward-tip race or an actual transport timeout."""


def verify_checkpoint(identity):
    block = identity.get('data') if isinstance(identity, dict) else None
    attestation = identity.get('attestation') if isinstance(identity, dict) else None
    if not isinstance(identity, dict) or identity.get('success') is not True or not isinstance(block, dict) or \
            type(block.get('slot')) is not int or block['slot'] != CHECKPOINT_SLOT or \
            block.get('hash') != CHECKPOINT_HASH or not isinstance(attestation, dict) or \
            attestation.get('chain_id') != CHAIN:
        raise IdentityError('Mainnet archive checkpoint mismatch.')


def uint(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r'[0-9]+', str(value)):
        raise CollectionError('Invalid integer in mainnet data.')
    result = int(value)
    if result > U64:
        raise CollectionError('Mainnet amount exceeds u64.')
    return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise CollectionError('Unexpected node redirect.')


def exact_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CollectionError('Duplicate field in mainnet data.')
        result[key] = value
    return result


def reject_constant(_):
    raise CollectionError('Non-finite value in mainnet data.')


class NodeReader:
    def __init__(self):
        # Local data must never be routed through HTTP_PROXY/HTTPS_PROXY.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def __call__(self, port, path):
        if port not in (50008, 56001) or not path.startswith('/') or path.startswith('//'):
            raise CollectionError('Invalid node route.')
        checkpoint = port == 50008 and path == f'/v2/block/slot/{CHECKPOINT_SLOT}'
        destination = 56003 if checkpoint else port
        req = Request(f'http://127.0.0.1:{destination}{path}',
                      headers={'Accept': 'application/json', 'Cache-Control': 'no-store'})
        try:
            with self.opener.open(req, timeout=3) as response:
                body = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as error:
            # HTTP failure is not evidence of a transport timeout or a tip race.
            error.close()
            raise CollectionError('Mainnet read is unavailable.') from None
        except TimeoutError:
            raise TransientCollectionError('Mainnet read timed out.') from None
        except URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TransientCollectionError('Mainnet read timed out.') from None
            raise CollectionError('Mainnet read is unavailable.') from None
        if len(body) > 2 * 1024 * 1024:
            raise CollectionError('Node response exceeds the census bound.')
        data = json.loads(body, parse_float=Decimal, parse_constant=reject_constant,
                          object_pairs_hook=exact_object)
        if not isinstance(data, dict) or data.get('success') is False or data.get('error') is not None:
            raise CollectionError('Mainnet read is unavailable.')
        if checkpoint:
            verify_checkpoint(data)
        return data


def registry(read, path, key, count_key='count'):
    rows, cursors, cursor, count = [], set(), None, None
    for _ in range(25):
        data = read(50008, path + '?limit=32' + ('' if cursor is None else '&after=' + quote(cursor, safe='')))
        current_count = uint(data.get(count_key))
        if data.get('success') is not True or not isinstance(data.get(key), list) or 'next_after' not in data:
            raise CollectionError('Registry page is incomplete.')
        if count is not None and count != current_count:
            raise CollectionError('Registry changed during collection.')
        count = current_count
        rows.extend(data[key])
        cursor = data['next_after']
        if cursor is None:
            if len(rows) != count:
                raise CollectionError('Registry census is incomplete.')
            return rows, count
        if not isinstance(cursor, str) or not cursor or len(cursor) > 128 or cursor in cursors:
            raise CollectionError('Invalid registry cursor.')
        cursors.add(cursor)
    raise CollectionError('Registry exceeds the supported census size.')


def tip(read):
    result = read(50008, '/v2/blocks?page=1&page_size=12')
    rows = result.get('data')
    if result.get('success') is not True or not isinstance(rows, list) or not rows:
        raise CollectionError('Mainnet block history is unavailable.')
    previous_slot = None
    for row in rows:
        if not re.fullmatch('[0-9a-f]{64}', str(row.get('hash', ''))):
            raise CollectionError('Invalid mainnet block hash.')
        slot = uint(row.get('slot'))
        if previous_slot is not None and slot >= previous_slot:
            raise CollectionError('Mainnet block history is not ordered.')
        previous_slot = slot
        uint(row.get('tx_count')); uint(row.get('poh_timestamp'))
        if not isinstance(row.get('proposer'), str):
            raise CollectionError('Invalid block proposer.')
    return {'slot': rows[0]['slot'], 'hash': rows[0]['hash']}, rows


def launch_pool_id(token_id):
    candidate = token_id + '_xrs_pool'
    return candidate if len(candidate.encode()) <= 128 else 'xeris_lp_' + hashlib.sha256(token_id.encode()).hexdigest() + '_xrs_pool'


def launch_census(read, tokens, pools, pool_count, policy):
    """Canonical launch pools supply prices; all active pools supply balances."""
    launches, approved = [], set(policy['included_pool_ids'])
    for token in tokens:
        if token.get('mint_policy') != 'LaunchpadManaged':
            continue
        tid = token.get('token_id')
        if not isinstance(tid, str) or not 3 < len(tid) <= 128:
            raise CollectionError('Invalid launch token identity.')
        holder_rows, count = registry(read, '/v2/token/' + quote(tid, safe='') + '/holders', 'holders', 'holder_count')
        supply = aggregate_token(token, holder_rows, count, pools, pool_count, policy,
                                 holders_complete=True, pools_complete=True)
        canonical = launch_pool_id(tid)
        pool = next((p for p in pools if p.get('pool_id') == canonical), None)
        row = {'token_id': tid, 'name': token.get('name', ''), 'symbol': token.get('symbol', ''),
               'decimals': '9', 'circulating_raw': supply['circulating_raw'], 'quote_decimals': '6',
               'quote_token_id': USDC, 'quote_symbol': 'xUSDC', 'valuation_basis': 'circulating_supply',
               'market_cap_quote_raw': None, 'status': 'pricing_unavailable', 'supply': supply}
        if pool and pool.get('is_active') is True:
            detail = read(56001, '/contract/' + quote(canonical, safe=''))
            contract = detail.get('contract', {})
            swap = contract.get('state', {}).get('Swap', {})
            if detail.get('success') is not True or contract.get('contract_id') != canonical or \
                    contract.get('contract_type') != 'Swap' or contract.get('is_active') is not True or \
                    contract.get('owner') != '__launchpad_pool__' or \
                    uint(contract.get('created_slot')) < uint(token.get('created_slot')) or \
                    pool.get('token_a') != tid or pool.get('token_b') != WRAPPED or \
                    swap.get('token_a') != tid or swap.get('token_b') != WRAPPED or \
                    uint(swap.get('fee_bps')) != 30 or uint(pool.get('fee_bps')) != 30 or \
                    uint(swap.get('reserve_a')) != uint(pool.get('reserve_a')) or \
                    uint(swap.get('reserve_b')) != uint(pool.get('reserve_b')) or \
                    uint(swap.get('total_shares')) != uint(pool.get('total_lp_shares')):
                raise CollectionError('Canonical launch market identity differs.')
            if uint(pool['reserve_a']) and uint(pool['reserve_b']):
                approved.add(canonical)
                row.update(status='ready', price_pool_id=canonical,
                           price_xrs_raw_numerator=str(uint(pool['reserve_b'])),
                           price_token_raw_denominator=str(uint(pool['reserve_a'])))
        launches.append(row)
    return launches, sorted(approved)


def collect(policy, read=None):
    if policy.get('chain_id') != CHAIN or not isinstance(policy.get('id'), str) or not policy['id']:
        raise IdentityError('Wrong circulation policy identity.')
    labeled = policy.get('company_wallets')
    if not isinstance(labeled, list) or any(not isinstance(row, dict) or
            not isinstance(row.get('address'), str) or not isinstance(row.get('role'), str) or
            not row['role'] for row in labeled):
        raise CollectionError('Invalid company wallet labels.')
    addresses = [row['address'] for row in labeled]
    if len(addresses) != len(set(addresses)) or set(addresses) != set(policy.get('company_addresses', [])):
        raise CollectionError('Public company wallet labels differ from company classifications.')
    source = read or NodeReader()
    start = time.monotonic()
    def read(port, path):
        if time.monotonic() - start > 12:
            raise CollectionError('Census collection exceeded its read deadline.')
        return source(port, path)
    identity = read(50008, f'/v2/block/slot/{CHECKPOINT_SLOT}')
    verify_checkpoint(identity)
    first, blocks = tip(read)
    stats_response = read(50008, '/v2/stats')
    if stats_response.get('success') is not True:
        raise CollectionError('Mainnet statistics are unavailable.')
    stats = stats_response['data']
    if uint(stats.get('current_slot')) > uint(first['slot']):
        raise TransientCollectionError('Mainnet advanced during collection.')
    if uint(stats.get('current_slot')) != uint(first['slot']):
        raise CollectionError('Mainnet advanced during collection.')
    balances = read(50008, '/balances')
    tokens, _ = registry(read, '/v2/tokens', 'data')
    token_ids = [t.get('token_id') for t in tokens]
    if len(set(token_ids)) != len(token_ids):
        raise CollectionError('Duplicate token metadata.')
    wrapped = next((t for t in tokens if t.get('token_id') == WRAPPED), None)
    if wrapped is None:
        raise CollectionError('Native wrapping registry is unavailable.')
    usdc = next((t for t in tokens if t.get('token_id') == USDC), None)
    if usdc is None or uint(usdc.get('decimals')) != 6 or usdc.get('mint_policy') != 'Standard':
        raise CollectionError('The xUSDC quote-token identity is unavailable.')
    holders, holder_count = registry(read, '/v2/token/xrs_native/holders', 'holders', 'holder_count')
    pools, pool_count = registry(read, '/v2/pools', 'pools')
    launches, included_pools = launch_census(read, tokens, pools, pool_count, policy)
    economics = read(56001, '/network/economics')
    pool_detail = read(56001, f'/contract/{POOL}')
    last, _ = tip(read)
    if uint(last['slot']) > uint(first['slot']):
        raise TransientCollectionError('Mainnet advanced during collection.')
    bundle = {'tip_start': first, 'tip_end': last, 'stats': stats, 'balances': balances,
              'wrapped_token': wrapped, 'holders': holders, 'holder_count': holder_count,
              'holders_complete': True, 'pools': pools, 'pool_count': pool_count,
              'pools_complete': True, 'economics': economics}
    supply = aggregate_supply(bundle, {**policy, 'included_pool_ids': included_pools})
    main_pool = next((p for p in pools if p.get('pool_id') == POOL), None)
    if not main_pool or main_pool.get('is_active') is not True or main_pool.get('token_a') != WRAPPED or main_pool.get('token_b') != USDC or uint(main_pool.get('fee_bps')) != 30:
        raise CollectionError('The mainnet pricing pool is unavailable.')
    reserve_xrs = uint(main_pool.get('reserve_a'))
    reserve_usdc = uint(main_pool.get('reserve_b'))
    if not reserve_xrs or not reserve_usdc:
        raise CollectionError('The mainnet pricing pool is empty.')
    contract = pool_detail.get('contract', {})
    state = contract.get('state', {}).get('Swap', {})
    if pool_detail.get('success') is not True or contract.get('contract_id') != POOL or \
            contract.get('contract_type') != 'Swap' or contract.get('is_active') is not True or \
            contract.get('owner') != POOL_OWNER or uint(contract.get('created_slot')) != CHECKPOINT_SLOT or \
            state.get('token_a') != WRAPPED or state.get('token_b') != USDC or uint(state.get('fee_bps')) != 30 or \
            uint(state.get('reserve_a')) != reserve_xrs or uint(state.get('reserve_b')) != reserve_usdc or \
            uint(state.get('total_shares')) != uint(main_pool.get('total_lp_shares')):
        raise CollectionError('Mainnet pricing-pool identity or reserves differ.')
    now = time.time()
    elapsed = time.monotonic() - start
    if elapsed > 15:
        raise CollectionError('Census collection took too long.')
    tip_age_ms = int(now * 1000) - uint(blocks[0]['poh_timestamp'])
    if tip_age_ms > MAX_TIP_AGE_MS or tip_age_ms < -30_000:
        raise CollectionError('The mainnet tip is stale or has an invalid timestamp.')
    circulation = uint(supply['circulating_raw'])
    for row in launches:
        if row['status'] == 'ready':
            row['market_cap_quote_raw'] = str(uint(row['circulating_raw']) * uint(row['price_xrs_raw_numerator']) * reserve_usdc //
                                              (uint(row['price_token_raw_denominator']) * reserve_xrs))
    policy_hash = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {
        'schema_version': '2', 'chain_id': CHAIN, 'status': 'ready',
        'observed_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
        'observed_at_ms': str(int(now * 1000)), 'expires_at_ms': str(int((now + 20) * 1000)),
        'as_of': {'slot': str(uint(last['slot'])), 'block_hash': last['hash'], 'block_height': str(uint(stats['block_height']))},
        'supply': supply,
        'market': {'token_id': 'native_xrs', 'symbol': 'XRS', 'circulating_raw': str(circulation), 'decimals': '9',
                   'quote_token_id': USDC, 'quote_symbol': 'xUSDC', 'quote_decimals': '6',
                   'market_cap_quote_raw': str(circulation * reserve_usdc // reserve_xrs),
                   'price_quote_raw_numerator': str(reserve_usdc), 'price_xrs_raw_denominator': str(reserve_xrs),
                   'price_pool_id': POOL, 'valuation_basis': 'circulating_supply', 'price_basis': 'pool_spot'},
        'mining': {'total_mined_raw': str(uint(economics.get('total_mined_lamports'))),
                   'total_staked_raw': str(uint(economics.get('total_staked_lamports'))),
                   'emission_cap_raw': '500000000000000000', 'genesis_allocation_raw': '200000000000000000',
                   'current_block_reward_xrs': str(economics.get('current_block_reward_xrs')),
                   'last_block_timestamp_ms': str(uint(blocks[0]['poh_timestamp'])),
                   'last_block_age_ms': str(max(0, tip_age_ms))},
        'blocks': [{'slot': str(uint(b['slot'])), 'hash': b['hash'], 'proposer': b['proposer'],
                    'timestamp_ms': str(uint(b['poh_timestamp'])), 'transaction_count': str(uint(b['tx_count']))} for b in blocks],
        'policy': {'id': policy['id'], 'sha256': policy_hash, 'company_wallets': policy['company_wallets'],
                   'included_pool_ids': included_pools,
                   'automatic_pool_rule': 'Protocol-managed launch token, reserved canonical graduation pool ID, verified __launchpad_pool__ owner and active token/XRS pair.',
                   'description': 'All unlocked on-chain XRS, including the genesis allocation and mined rewards held in company or public wallets, plus active approved AMM inventory. Company ownership alone does not exclude supply. Staking, unbonding, burn addresses, locked and other contract custody are excluded. Wrapped XRS is counted once.',
                   'native_account_limit': '100'},
        'coverage': {'complete': True, 'native_accounts': str(uint(stats['total_accounts'])),
                     'wrapped_holders': str(holder_count), 'pools': str(pool_count), 'collection_ms': str(int(elapsed * 1000))},
        'launchpad_tokens': launches,
    }
