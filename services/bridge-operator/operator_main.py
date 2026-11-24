"""Loopback settlement service and bounded worker; starts paused by default.

Activation requires a fresh explicit local signing policy, adequate backing,
fee reserves and complete evidence. No policy is generated or enabled here.
The existing chain and all prior pilot authorizations are untouched.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import time

from chain_observer import BlockArchive, ChainObserver
from journal import Journal
from runtime import SettlementRuntime
from runtime_adapters import ScopedPolicy, LocalScopedSigner, SingleSubmissionBroadcaster
from service import ApiStore, SettlementService, LimitedServer, handler_for
from settlement_observer import SettlementObserver
from observer_daemon import write_health


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-directory',required=True)
    parser.add_argument('--operator-wallet',required=True)
    parser.add_argument('--authorization-path',required=True)
    parser.add_argument('--authorization-id',required=True)
    parser.add_argument('--node-binary')
    parser.add_argument('--openssl')
    parser.add_argument('--port',type=int,default=8791)
    parser.add_argument('--enable-processing',action='store_true')
    parser.add_argument('--check',action='store_true',help='Report paused/current capabilities and exit, with no request processing')
    args=parser.parse_args()
    if not 1024<=args.port<=65535:parser.error('A loopback high port is required')
    if args.check and args.enable_processing:parser.error('Check mode cannot enable processing')
    directory=Path(args.state_directory).absolute()
    if directory.is_symlink():parser.error('State directory cannot be a symlink')
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=directory.stat()
    if info.st_uid!=os.getuid() or info.st_mode&0o077:parser.error('State directory must be private to its owner')
    lock=os.open(directory/'operator.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock);parser.error('Another operator process owns this state directory')
    journal=Journal(directory/'settlement.sqlite3',openssl=args.openssl)
    api=ApiStore(directory/'api.sqlite3')
    archive=BlockArchive(directory/'observer.sqlite3')
    server=None
    stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        journal.pause('Startup: explicit activation and readiness required')
        chain=ChainObserver(archive)
        observer=SettlementObserver(chain,journal,operator_wallet=args.operator_wallet)
        policy=ScopedPolicy(Path(args.authorization_path).absolute(),journal._now)
        signer=LocalScopedSigner(journal,policy,node=args.node_binary,can_operate=lambda:not stopping)
        broadcaster=SingleSubmissionBroadcaster(journal,policy,policy_id=args.authorization_id,can_operate=lambda:not stopping)
        runtime=SettlementRuntime(journal,observer,operator_wallet=args.operator_wallet,
            signer=signer,broadcaster=broadcaster,authorization_id=args.authorization_id)
        service=SettlementService(journal,api,executor=runtime)
        if args.enable_processing:
            authorization=signer.readiness()
            if not authorization.get('authorized') or authorization.get('policy_id')!=args.authorization_id:
                raise ValueError('New local signing authorization is unavailable')
            journal.resume('Explicit local activation with scoped authorization')
            if not runtime.readiness()['can_accept_funds']:
                journal.pause('Activation readiness checks did not pass')
                raise ValueError('Settlement readiness is incomplete')
        if args.check:
            print(json.dumps(service.capabilities(),separators=(',',':')))
            return 0
        server=LimitedServer(('127.0.0.1',args.port),handler_for(service))
        server.timeout=.5
        next_tick=0
        while not stopping:
            server.handle_request()
            if stopping:break
            now=time.monotonic()
            if now<next_tick:continue
            next_tick=now+5
            healthy=True
            try:
                chain.collect()
                if stopping:break
                # A cancelled request can receive late source evidence after its
                # active-route lease was released. Reconcile those original IDs
                # without permitting preparation, signing or broadcasting.
                runtime.observe_late_sources(limit=5,can_continue=lambda:not stopping)
                if stopping:break
                active=journal.recovery_items()['active_route']['intent_id']
                if active:runtime.expire_unfunded(active)
                active=journal.recovery_items()['active_route']['intent_id']
                # Existing obligations get one reconciliation step per tick.
                # Paused runtime observation cannot sign or send new messages.
                if active and not stopping:runtime.step(active)
            except Exception:
                healthy=False
            write_health(directory/'operator-health.json',{
                'checked_at':int(time.time()),'observer_available':healthy,
                'processing_paused':journal.processing_state()['paused'],
                'archive_bounds':archive.bounds(),
                'public_intake_verified':False,
                'note':'Operational health is not evidence of a funded wallet acceptance test.'})
        return 0
    finally:
        journal.pause('Process stopped: explicit activation and readiness required')
        if server:server.server_close()
        archive.close();api.close();journal.close();os.close(lock)


if __name__=='__main__':raise SystemExit(main())
