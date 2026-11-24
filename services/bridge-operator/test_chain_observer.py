"""Adversarial archive and public-reader boundary tests; no live chain I/O."""
import copy
import hashlib
from functools import lru_cache
from pathlib import Path
import tempfile
import unittest

import chain_observer as o
from public_rpc import PublicReadClient, ReadError, strict_json
import xeris_accounting as a
import xeris_purchase as p
from test_xeris_purchase import OPERATOR, SIGNATURE, BLOCKHASH, prepared
from test_xeris_accounting import signed_wire


NOW = 2_000_000_000


def raw_block(slot, txs=None):
    txs = txs or []
    import base64
    wires = [base64.b64decode(a.encode_rpc_transaction(tx)) for tx in txs]
    value={'slot': slot, 'nonce':0,'poh_timestamp':NOW*1000,'poh_hash':[7]*32,'proposer_sig':[9]*77,
            'previous_hash': list(fixture_hash(slot-1)),
            'merkle_root': list(bytes.fromhex(a.merkle_root(wires))),
            'proposer': list(p._decode58(p.TREASURY, 32)), 'transactions': txs}
    value['hash']=list(o.work_hash(value))
    return value


@lru_cache(maxsize=1000)
def fixture_hash(slot):
    return bytes([1])*32 if slot==0 else bytes(raw_block(slot)['hash'])


def rpc_transaction():
    plan = prepared('buy', 500_000, 1)
    sv = lambda items: [list(p._shortvec(len(items))), *items]
    return {'signatures': sv([list(p._decode58(SIGNATURE, 64))]), 'message': {
        'header': {'num_required_signatures': 1, 'num_readonly_signed_accounts': 0, 'num_readonly_unsigned_accounts': 1},
        'account_keys': sv([list(p._decode58(OPERATOR, 32)), [0]*32]),
        'recent_blockhash': list(BLOCKHASH),
        'instructions': sv([{'program_id_index': 1, 'accounts': sv([0]), 'data': sv(list(bytes.fromhex(plan['instruction_hex'])))}])}}


def summary(slot):
    raw = raw_block(slot)
    return {'slot': slot, 'hash': bytes(raw['hash']).hex(), 'merkle_root': bytes(raw['merkle_root']).hex(),
            'proposer': p.TREASURY, 'transaction_count': 0, 'transactions': [], 'poh_timestamp': NOW*1000}


class Reader:
    def __init__(self):
        self.tip = 100
        self.mutate = lambda node, value: value
        self.raw_mutate = lambda value: value

    def explorer(self, path, node='droplet'):
        if path == f'/v2/block/slot/{o.CHECKPOINT_SLOT}':
            value = {'slot': o.CHECKPOINT_SLOT, 'hash': o.CHECKPOINT_HASH}
        elif path == '/v2/stats':
            value = {'current_slot': self.tip}
        else:
            value = summary(int(path.rsplit('/', 1)[-1]))
        return {'success': True, 'data': self.mutate(node, value)}

    def raw_blocks(self, node):
        return self.raw_mutate([raw_block(s) for s in range(self.tip, self.tip-50, -1)])


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'archive.sqlite'
        self.archive = o.BlockArchive(self.path)
        self.reader = Reader()
        self.observer = o.ChainObserver(self.archive, self.reader, lambda: NOW)

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def test_bootstrap_then_append_and_restart_keeps_contiguous_anchor(self):
        self.assertEqual(self.observer.collect()['slot'], 100)
        self.reader.tip = 103
        self.observer.collect()
        self.assertEqual(self.archive.bounds(), (100,103))
        self.archive.close()
        self.archive = o.BlockArchive(self.path)
        self.assertEqual(len(self.archive.window(100,103)),4)
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)

    def test_missing_raw_window_is_never_reset_to_current_tip(self):
        self.observer.collect()
        self.reader.tip = 151
        with self.assertRaisesRegex(o.ObservationError,'outside_raw_window'):
            self.observer.collect()
        self.assertEqual(self.archive.bounds(),(100,100))

    def test_checkpoint_change_fails_before_archival(self):
        def mutate(node, value):
            if node=='droplet' and value.get('slot')==o.CHECKPOINT_SLOT:
                value['hash']='ab'*32
            return value
        self.reader.mutate=mutate
        with self.assertRaisesRegex(o.ObservationError,'checkpoint'):
            self.observer.collect()
        self.assertEqual(self.archive.bounds(),(None,None))

    def test_one_node_canonical_fork_is_rejected(self):
        def mutate(node,value):
            if node=='droplet' and value.get('slot')==100:
                value['hash']='ab'*32
            return value
        self.reader.mutate=mutate
        with self.assertRaisesRegex(o.ObservationError,'canonical_mismatch'):
            self.observer.collect()

    def test_wrong_parent_cannot_append_to_existing_archive(self):
        self.observer.collect()
        self.reader.tip=101
        def mutate(values):
            values[0]['previous_hash']=[99]*32
            values[0]['hash']=list(o.work_hash(values[0]))
            return values
        self.reader.raw_mutate=mutate
        with self.assertRaisesRegex(o.ObservationError,'canonical_mismatch|parent_conflict'):
            self.observer.collect()
        self.assertEqual(self.archive.bounds(),(100,100))

    def test_stale_tip_rejects_all_reads(self):
        def mutate(node,value):
            if 'poh_timestamp' in value:value['poh_timestamp']-=121_000
            return value
        self.reader.mutate=mutate
        with self.assertRaisesRegex(o.ObservationError,'stale_node_tip'):
            self.observer.collect()

    def test_wire_encoding_commits_real_order_and_merkle(self):
        raw=raw_block(100,[rpc_transaction()])
        normalized=o.normalize_raw(raw)
        self.assertEqual(normalized['raw_transactions_by_signature'][SIGNATURE],signed_wire(prepared('buy',500_000,1)))
        raw['merkle_root']=[22]*32
        with self.assertRaisesRegex(o.ObservationError,'merkle'):
            o.normalize_raw(raw)

    def test_duplicate_raw_slots_are_rejected(self):
        self.reader.raw_mutate=lambda values:[values[0],values[0]]
        with self.assertRaisesRegex(o.ObservationError,'duplicate_raw_slot'):
            self.observer.collect()

    def test_archive_digest_corruption_is_detected(self):
        self.observer.collect()
        self.archive.db.execute("UPDATE observer_blocks SET evidence='{}' WHERE slot=100")
        with self.assertRaisesRegex(o.ObservationError,'digest_mismatch'):
            self.archive.get(100)

    def test_incomplete_evidence_range_cannot_be_reported_complete(self):
        self.observer.collect()
        with self.assertRaisesRegex(o.ObservationError,'window_missing'):
            self.archive.window(99,100)

    def test_nonempty_live_shape_decimal_receipt_archives_and_survives_restart(self):
        import json
        self.observer.collect()
        self.reader.tip=101
        raw=raw_block(101,[rpc_transaction()])
        plan=prepared('buy',500_000,1)
        base=self.reader.explorer
        def explorer(path,node='droplet'):
            response=base(path,node)
            if path=='/v2/block/slot/101':
                response['data'].update(hash=bytes(raw['hash']).hex(),merkle_root=bytes(raw['merkle_root']).hex(),transaction_count=1,
                    transactions=[{'signature':SIGNATURE,'block_slot':101,'status':'included','amount_xrs':0.0,**plan['expected_receipt_fields']}])
            return strict_json(json.dumps(response))
        self.reader.explorer=explorer
        self.reader.raw_mutate=lambda values:[raw,*values[1:]]
        receipt={'slot':101,'blockTime':NOW,'meta':{'err':None,'fee':1_000_000,'instructionOutcomes':[{'index':0,'ok':True}]},
            'transaction':{'signatures':[SIGNATURE],'amount_xrs':0.0,**plan['expected_receipt_fields']}}
        self.reader.xeris=lambda method,params,node:strict_json(json.dumps(receipt))
        # A receipt-store interruption must leave this block retryable, not
        # poison the immutable archive with an OutcomeUnknown observation.
        good=copy.deepcopy(receipt)
        receipt['meta'].update(err='OutcomeUnknown',instructionOutcomes=None)
        with self.assertRaisesRegex(o.ObservationError,'outcomes_incomplete'):
            self.observer.collect()
        self.assertEqual(self.archive.bounds(),(100,100))
        receipt=good
        self.observer.collect()
        self.archive.close();self.archive=o.BlockArchive(self.path)
        record=self.archive.get(101)
        self.assertEqual(record['receipts_by_node']['droplet'][SIGNATURE]['transaction']['amount'],0)
        self.assertNotIn('amount_xrs',record['receipts_by_node']['droplet'][SIGNATURE]['transaction'])
        self.assertEqual(record['raw_transactions_by_signature'][SIGNATURE],signed_wire(plan))

    def test_parent_timestamp_and_recovery_data_are_bound_to_header_hash(self):
        for key in ('previous_hash','poh_timestamp','proposer_sig'):
            raw=raw_block(100)
            if key=='poh_timestamp':raw[key]+=1
            else:raw[key][0]^=1
            with self.assertRaisesRegex(o.ObservationError,'header_commitment'):
                o.normalize_raw(raw)


class ReaderBoundaryTests(unittest.TestCase):
    def test_v2_routes_use_explorer_ports_raw_routes_use_state_ports(self):
        urls=[]
        reader=PublicReadClient(transport=lambda url,body:urls.append(url))
        reader.explorer('/v2/stats','mac_treasury')
        reader.explorer('/v2/block/slot/100','mac_auxiliary')
        reader.explorer('/token/balance/'+OPERATOR+'/xeris_usdc','mac_treasury')
        self.assertEqual(urls,['http://127.0.0.1:50008/v2/stats','http://127.0.0.1:50010/v2/block/slot/100','http://127.0.0.1:56001/token/balance/'+OPERATOR+'/xeris_usdc'])

    def test_payout_preflight_blockhash_read_allowed_but_send_forbidden(self):
        reader=PublicReadClient(transport=lambda url,body:{'jsonrpc':'2.0','id':1,'result':{'value':True}})
        self.assertEqual(reader.solana('isBlockhashValid',['public-blockhash']),{'value':True})
        with self.assertRaisesRegex(ReadError,'read_only'):
            reader.solana('sendTransaction',['payload'])


if __name__=='__main__':unittest.main()
