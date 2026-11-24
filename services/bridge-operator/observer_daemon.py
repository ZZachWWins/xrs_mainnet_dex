"""Continuously archive public chain evidence; never sign or submit transactions."""
import argparse
import json
import os
from pathlib import Path
import signal
import time

from chain_observer import BlockArchive, ChainObserver


def write_health(path, value):
    temporary=path.with_suffix('.pending')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as handle:
        json.dump(value,handle,separators=(',',':'));handle.write('\n');handle.flush();os.fsync(handle.fileno())
    os.replace(temporary,path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-directory',required=True)
    parser.add_argument('--interval',type=int,default=5)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    if not 3<=args.interval<=20:parser.error('Interval must be 3–20 seconds')
    directory=Path(args.state_directory).absolute()
    if directory.is_symlink():parser.error('State directory cannot be a symlink')
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    if directory.stat().st_uid!=os.getuid() or directory.stat().st_mode&0o077:parser.error('State directory must be private to its owner')
    archive=BlockArchive(directory/'observer.sqlite3');observer=ChainObserver(archive)
    stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        while not stopping:
            started=time.monotonic()
            try:
                anchor=observer.collect()
                health={'mode':'read_only','checked_at':int(time.time()),'available':True,'anchor':anchor,'archive_bounds':archive.bounds()}
            except Exception:
                health={'mode':'read_only','checked_at':int(time.time()),'available':False,'error':'observation_incomplete','archive_bounds':archive.bounds()}
            write_health(directory/'observer-health.json',health)
            if args.once:
                print(json.dumps(health,separators=(',',':')))
                return 0 if health['available'] else 1
            # Normal process lifecycle wait; API/assistant are not blocked.
            deadline=started+args.interval
            while not stopping and time.monotonic()<deadline:
                time.sleep(min(.25,max(0,deadline-time.monotonic())))
    finally:
        archive.close()


if __name__=='__main__':raise SystemExit(main())
