#!/usr/bin/env python3
"""Add only the reviewed, paused settlement route; never restart an L1 node."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

CONFIG=Path('/etc/nginx/conf.d/mainnetrpc.xerisweb.com.conf')
COMMON=Path('/etc/nginx/snippets/mainnetrpc-proxy-common.conf')
SNIPPETS=Path('/etc/nginx/snippets')
BACKUP_DIRECTORY=Path('/etc/nginx')
EXPECTED_CONFIG='d2f9c95338f237dd2e21a28ac19533bdf703d9b9455ee8cde5472eb18551ba80'
EXPECTED_COMMON='b5a7b66c94308f3be8fcfe2cf1526f7ab139d0a4fb0c14b852d589abc714427f'
FILES=('settlement-globals.conf','settlement-location.conf')

def require(condition, message):
    if not condition:
        raise RuntimeError(message)

def run(*args):
    return subprocess.check_output(args,text=True).strip()

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def paused():
    request=urllib.request.Request('http://127.0.0.1:8791/settlement/v1/capabilities',headers={'Origin':'https://dex.xerisweb.com'})
    with urllib.request.urlopen(request,timeout=5) as response:
        body=response.read(8193)
        require(response.status==200 and len(body)<=8192,'Paused capability response is unavailable or oversized')
        data=json.loads(body)
    require(data['schema_version']==1 and data['can_accept_funds'] is False,'Settlement must be explicitly paused')
    require(data['network']['xeris_chain_id']=='xeris-mainnet-v4','Settlement chain identity changed')
    require(data['routes'] and all(v['enabled'] is False for v in data['routes'].values()),'All settlement routes must be disabled')

def main():
    require(os.getuid()==0 and len(sys.argv)==2,'Run as root with exactly one reviewed staging directory')
    stage=Path(sys.argv[1]).resolve(strict=True)
    require(stage.is_dir() and stage.stat().st_uid==0 and not stage.stat().st_mode&0o022,'Staging directory ownership or permissions rejected')
    manifest=json.loads((stage/'proxy-manifest.json').read_text())
    require(type(manifest) is dict and set(manifest)==set(FILES),'Reviewed proxy manifest is incomplete')
    for name in FILES:
        path=stage/name
        require(path.is_file() and not path.is_symlink() and path.stat().st_uid==0 and not path.stat().st_mode&0o022,'Staged snippet ownership or permissions rejected')
        require(path.stat().st_size<8192 and sha(path)==manifest[name],'Staged snippet differs from reviewed manifest')
    require(sha(CONFIG)==EXPECTED_CONFIG and sha(COMMON)==EXPECTED_COMMON,'Proxy baseline changed; review again')
    installed=[SNIPPETS/('xeris-'+name) for name in FILES]
    require(not any(p.exists() or p.is_symlink() for p in installed),'Settlement snippets already exist; review before replacing')
    pid=run('systemctl','show','xeris-mainnet-v4.service','--property=MainPID','--value')
    require(int(pid)>0 and run('systemctl','is-active','xeris-mainnet-v4.service')=='active','Mainnet node must already be active')
    run('/usr/sbin/nginx','-t')
    paused()
    original=CONFIG.read_text()
    before='server {\n    listen 80;'
    fallback='    location / { return 404; }'
    require(original.count(before)==1 and original.count(fallback)==1,'Expected proxy insertion points changed')
    proposed=original.replace(before,'include /etc/nginx/snippets/xeris-settlement-globals.conf;\n\n'+before).replace(fallback,'    include /etc/nginx/snippets/xeris-settlement-location.conf;\n\n'+fallback)
    backup=BACKUP_DIRECTORY/('xeris-settlement-backup-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()))
    backup.mkdir(mode=0o700)
    shutil.copyfile(CONFIG,backup/'mainnetrpc.before.conf')
    created=[]
    config_changed=False
    try:
        for name,target in zip(FILES,installed):
            descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
            metadata=os.fstat(descriptor)
            created.append((target,metadata.st_dev,metadata.st_ino))
            with os.fdopen(descriptor,'wb') as handle:
                handle.write((stage/name).read_bytes());handle.flush();os.fsync(handle.fileno())
            target.chmod(0o644)
        require(sha(CONFIG)==EXPECTED_CONFIG and sha(COMMON)==EXPECTED_COMMON,'Proxy baseline changed during preparation')
        config_changed=True
        CONFIG.write_text(proposed);CONFIG.chmod(0o644)
        run('/usr/sbin/nginx','-t')
        run('systemctl','reload','nginx.service')
        paused()
        require(run('systemctl','show','xeris-mainnet-v4.service','--property=MainPID','--value')==pid,'Mainnet node PID changed')
        require(run('systemctl','is-active','xeris-mainnet-v4.service')=='active','Mainnet node is no longer active')
        report={'status':'paused_route_installed','node_pid_unchanged':pid,'nginx_sha256':sha(CONFIG),'backup':str(backup),'funds_enabled':False}
        (backup/'deployment.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
    except BaseException:
        if config_changed:
            shutil.copyfile(backup/'mainnetrpc.before.conf',CONFIG)
        for target,device,inode in reversed(created):
            current=target.lstat()
            require((current.st_dev,current.st_ino)==(device,inode),'Rollback snippet changed; preserve it for manual review')
            target.unlink()
        if config_changed:
            run('/usr/sbin/nginx','-t');run('systemctl','reload','nginx.service')
        raise

if __name__=='__main__':main()
