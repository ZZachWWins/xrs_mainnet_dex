"""Bounded public chain reads. No secret access, signing or submission methods."""
from decimal import Decimal
import json
import re
import urllib.request

XERIS = 'https://mainnetrpc.xerisweb.com'
SOLANA = 'https://api.mainnet-beta.solana.com'
NODE_READS = {'mac_treasury': 'http://127.0.0.1:56001',
              'mac_auxiliary': 'http://127.0.0.1:56002', 'droplet': XERIS}
NODE_RPCS = {'mac_treasury': 'http://127.0.0.1:50008',
             'mac_auxiliary': 'http://127.0.0.1:50010', 'droplet': XERIS}
# Optional existing-SSH tunnel, bound only to loopback; never a public proxy.
RAW_READS = {**NODE_READS, 'droplet': 'http://127.0.0.1:56003'}
SOLANA_READS = frozenset(('getGenesisHash', 'getTransaction', 'getSignatureStatuses',
    'getLatestBlockhash', 'getBlockHeight', 'isBlockhashValid', 'getMultipleAccounts', 'getAccountInfo',
    'getFeeForMessage', 'getMinimumBalanceForRentExemption', 'getTokenAccountBalance',
    'simulateTransaction'))
XERIS_READS = frozenset(('getTransaction', 'getBalance', 'getLatestBlockhash', 'getHealth'))


class ReadError(ValueError):
    pass


def strict_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ReadError('duplicate_json_key')
            result[key] = value
        return result
    def constant(_):
        raise ReadError('invalid_json_number')
    return json.loads(data, object_pairs_hook=unique, parse_float=Decimal, parse_constant=constant)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ReadError('redirect_rejected')


class PublicReadClient:
    def __init__(self, *, timeout=12, transport=None):
        if type(timeout) is not int or not 1 <= timeout <= 30:
            raise ReadError('invalid_timeout')
        self.timeout = timeout
        self.transport = transport
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def _read(self, url, body=None, maximum=4_500_000):
        if self.transport:
            return self.transport(url, body)
        payload = None if body is None else json.dumps(body, separators=(',', ':'), allow_nan=False).encode()
        request = urllib.request.Request(url, data=payload, headers={
            'Accept': 'application/json', 'Content-Type': 'application/json',
            'Cache-Control': 'no-cache', 'User-Agent': 'Xeris-settlement-public-reader/1'},
            method='GET' if payload is None else 'POST')
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise ReadError('read_http_error')
                data = response.read(maximum + 1)
            if len(data) > maximum:
                raise ReadError('response_too_large')
            return strict_json(data)
        except ReadError:
            raise
        except Exception as error:
            raise ReadError('chain_read_unavailable') from error

    def solana(self, method, params=None):
        if method not in SOLANA_READS:
            raise ReadError('method_not_read_only')
        if method == 'simulateTransaction':
            if not isinstance(params, list) or len(params) != 2 or params[1].get('sigVerify') is not False:
                raise ReadError('only_unsigned_simulation_allowed')
        return self._rpc(SOLANA, method, params or [])

    def xeris(self, method, params=None, node='droplet'):
        if method not in XERIS_READS or node not in NODE_RPCS:
            raise ReadError('method_or_node_rejected')
        return self._rpc(NODE_RPCS[node], method, params or [])

    def _rpc(self, url, method, params):
        body = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
        response = self._read(url, body)
        if type(response) is not dict or response.get('id') != 1 or 'error' in response or 'result' not in response:
            raise ReadError('rpc_result_invalid')
        return response['result']

    def explorer(self, path, node='droplet'):
        allowed = ('/contract/xrs-xusdc-mainnet-v1', '/tokens?limit=100',
                   '/v2/tokens?limit=100', '/v2/stats')
        address = r'[1-9A-HJ-NP-Za-km-z]{32,44}'
        dynamic = re.fullmatch(r'/v2/block/slot/[0-9]{1,20}', path) or re.fullmatch(
            r'/token/balance/' + address + r'/(xeris_usdc|xrs_native)', path)
        if node not in NODE_READS or path not in allowed and not dynamic:
            raise ReadError('read_path_rejected')
        # The Mac's raw state server and JSON-RPC/explorer server use separate
        # ports. The public reverse proxy consolidates them on the droplet.
        base = NODE_RPCS[node] if path.startswith('/v2/') else NODE_READS[node]
        return self._read(base + path)

    def raw_blocks(self, node):
        if node not in RAW_READS:
            raise ReadError('node_rejected')
        return self._read(RAW_READS[node] + '/blocks')

    def pool(self, node='droplet'):
        response = self.explorer('/contract/xrs-xusdc-mainnet-v1', node)
        if type(response) is not dict or response.get('success') is not True:
            raise ReadError('pool_unavailable')
        contract = response.get('contract', {})
        state = contract.get('state', {}).get('Swap', {})
        if (contract.get('contract_id') != 'xrs-xusdc-mainnet-v1' or contract.get('is_active') is not True
            or state.get('token_a') != 'xrs_native' or state.get('token_b') != 'xeris_usdc'
            or state.get('fee_bps') != 30):
            raise ReadError('pool_identity_changed')
        if any(type(state.get(k)) is not int or not 0 < state[k] < 2**64 for k in ('reserve_a', 'reserve_b')):
            raise ReadError('invalid_pool_reserves')
        return state
