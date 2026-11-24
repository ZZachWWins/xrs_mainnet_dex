"""HTTP/API contract tests over in-memory transport and isolated SQLite files."""
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import journal as j
import service as s
import solana_verify as sv
from test_runtime import OWNER, RECIPIENT, SIGNATURE


class Reader:
    def __init__(self):self.calls=0
    def pool(self):self.calls+=1;return {'reserve_a':709700000000,'reserve_b':10099031}


class Executor:
    def __init__(self,case):self.case=case;self.enabled=True;self.prepares=[];self.submits=[]
    def readiness(self):return {'checked_at':self.case.now,'can_accept_funds':self.enabled,
        'routes':{route:self.enabled and action!='sell' for action,route in j.ROUTES.items()}}
    def status(self,identifier):return self.case.journal.status(identifier)
    def prepare_source(self,identifier):self.prepares.append(identifier);return {'test_only':'prepared'}
    def submit_source(self,identifier,encoded):self.submits.append((identifier,encoded));return self.status(identifier)


class Socket:
    def __init__(self,data):self.input=io.BytesIO(data);self.output=bytearray()
    def makefile(self,*_):return self.input
    def sendall(self,data):self.output.extend(data)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();base=Path(self.tmp.name);self.now=1800000000
        self.journal=j.Journal(base/'settlement.sqlite3',clock=lambda:self.now);self.journal.resume('Offline contract test')
        self.store=s.ApiStore(base/'api.sqlite3');self.reader=Reader();self.executor=Executor(self)
        self.api=s.SettlementService(self.journal,self.store,self.reader,self.executor,clock=lambda:self.now)
        self.proof=patch.object(j,'verify_ed25519',return_value=None);self.proof.start()
    def tearDown(self):self.proof.stop();self.store.close();self.journal.close();self.tmp.cleanup()
    def fields(self,action='buy',client='request1'):
        return {'client_request_id':client,'action':action,'route':j.ROUTES[action],'amount_raw':'500000',
            'source_owner':OWNER,'destination_owner':RECIPIENT,
            'solana_token_account':sv.associated_token_address(OWNER if action=='buy' else RECIPIENT),'slippage_bps':50}
    def accepted(self,action='buy',client='request1'):
        quote=self.api.quote(self.fields(action,client));challenge=self.api.challenge({'quote_id':quote['quote_id'],'client_request_id':client})
        body={'intent':challenge['intent'],'signature':SIGNATURE}
        result=self.api.accept(body,client)
        return body,result
    def test_paused_service_and_no_executor_never_quote_or_read_chain(self):
        self.api.executor=None
        self.assertFalse(self.api.capabilities()['can_accept_funds'])
        with self.assertRaises(s.ApiError):self.api.quote(self.fields())
        self.assertEqual(self.reader.calls,0)
    def test_quote_challenge_amount_units_and_sixty_second_bound(self):
        quote=self.api.quote(self.fields());self.assertEqual(quote['expected_output_raw'],'33383761746')
        self.assertEqual(quote['expires_at']-quote['created_at'],60)
        challenge=self.api.challenge({'quote_id':quote['quote_id'],'client_request_id':'request1'})
        self.assertEqual(challenge['intent']['expires_at'],quote['expires_at'])
        self.assertEqual(challenge['signing_message_utf8'],j.intent_message(challenge['intent']).decode())
        self.assertEqual(self.api.quote(self.fields()),quote);self.assertEqual(self.reader.calls,1)
    def test_redeem_is_exact_and_cannot_change_ata_or_amount(self):
        quote=self.api.quote(self.fields('redeem'));self.assertEqual(quote['min_output_raw'],'500000');self.assertEqual(quote['expected_output_raw'],'500000')
        bad=self.fields('redeem',client='bad');bad['solana_token_account']=sv.associated_token_address(OWNER)
        with self.assertRaises(s.ApiError):self.api.quote(bad)
        bad=self.fields(client='bad2');bad['amount_raw']='0.5'
        with self.assertRaises(ValueError):self.api.quote(bad)
    def test_accept_exact_retry_after_expiry_and_pause_restores_access_not_new_request(self):
        body,first=self.accepted();self.now+=1000;self.executor.enabled=False;self.journal.pause('Offline pause')
        second=self.api.accept(body,'request1')
        self.assertEqual(first['request'],second['request']);self.assertNotEqual(first['status_token'],second['status_token'])
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM settlement_intents').fetchone()[0],1)
        self.assertEqual(self.executor.submits,[])
    def test_access_proof_and_bearer_are_separate_and_cross_request_rejected(self):
        body,result=self.accepted();body2,result2=self.accepted(client='request2')
        with self.assertRaises(s.ApiError):self.api.status('request2','Bearer '+result['status_token'])
        with self.assertRaises(s.ApiError):self.api.restore_access('request2',body)
        with self.assertRaises(s.ApiError):self.api.status('request1',None)
        self.now+=1000;access=self.api.restore_access('request1',body)
        self.assertEqual(self.api.status('request1','Bearer '+access['status_token'])['request']['client_request_id'],'request1')
    def test_source_routes_require_bearer_and_exact_fields_even_while_paused(self):
        body,result=self.accepted();auth={'Authorization':'Bearer '+result['status_token']}
        base=s.PREFIX+'/requests/request1/'
        with self.assertRaises(s.ApiError):self.api.dispatch('POST',base+'prepare-source',{}, {})
        self.api.dispatch('POST',base+'prepare-source',{},auth)
        with self.assertRaises(s.ApiError):self.api.dispatch('POST',base+'submit-source',{'transaction_base64':'public','recipient':OWNER},auth)
        self.executor.enabled=False;self.journal.pause('Offline pause')
        self.api.dispatch('POST',base+'submit-source',{'transaction_base64':'original'},auth)
        self.assertEqual(len(self.executor.submits),1)
    def test_quote_pruning_preserves_accepted_quote_after_token_issue_crash(self):
        body,result=self.accepted();self.store.db.execute('DELETE FROM api_access')
        unused=self.api.quote(self.fields(client='unused'));self.now+=61
        self.api.quote(self.fields(client='fresh'))
        self.assertIsNotNone(self.store.quote('request1'));self.assertIsNone(self.store.quote('unused'))
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM settlement_challenges WHERE accepted_id IS NOT NULL').fetchone()[0],1)
    def test_status_actual_output_comes_only_from_verified_terminal_stage(self):
        body,result=self.accepted();status=self.journal.verify_existing_intent(body['intent'],SIGNATURE)
        self.assertIsNone(result['request']['actual_output_raw'])
        status['status']='completed';status['stages']=[{'stage':'deliver','transaction_id':SIGNATURE,'amount_raw':'33383761746','state':'confirmed','updated_at':self.now}]
        flat=self.api._flat(status);self.assertEqual(flat['actual_output_raw'],'33383761746');self.assertIsNone(flat['refund_amount_raw'])
        status['status']='refunded';status['stages']=[{'stage':'refund','transaction_id':SIGNATURE,'amount_raw':'500000','state':'confirmed','updated_at':self.now}]
        flat=self.api._flat(status);self.assertEqual(flat['refund_amount_raw'],'500000');self.assertEqual(flat['refund_asset'],'solana_usdc');self.assertIsNone(flat['actual_output_raw'])
    def test_http_headers_body_and_origin_boundaries_without_sockets(self):
        handler=s.handler_for(self.api)
        def request(raw):
            sock=Socket(raw);handler(sock,('127.0.0.1',1234),SimpleNamespace(server_name='localhost',server_port=8791))
            head,body=bytes(sock.output).split(b'\r\n\r\n',1)
            return int(head.split(b' ')[1]),head,json.loads(body)
        status,head,body=request(b'GET /settlement/v1/capabilities HTTP/1.1\r\nHost: localhost\r\n\r\n')
        self.assertEqual(status,200);self.assertIn(b'Cache-Control: no-store',head)
        for headers,expected in [([],403),(['Origin: https://evil.example'],403),
                (['Origin: https://dex.xerisweb.com','Content-Type: application/json','Content-Length: 2','Content-Length: 2'],400),
                (['Origin: https://dex.xerisweb.com','Content-Type: application/json','Content-Length: 20000'],413),
                (['Origin: https://dex.xerisweb.com','Transfer-Encoding: chunked'],400)]:
            raw=('POST /settlement/v1/quotes HTTP/1.1\r\nHost: localhost\r\n'+'\r\n'.join(headers)+'\r\n\r\n{}').encode()
            self.assertEqual(request(raw)[0],expected)
        body=b'{"a":1,"a":2}'
        raw=b'POST /settlement/v1/quotes HTTP/1.1\r\nHost: localhost\r\nOrigin: https://dex.xerisweb.com\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body
        self.assertEqual(request(raw)[0],400)


if __name__=='__main__':unittest.main()
