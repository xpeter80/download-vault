"""Host-only fixed-action bridge. No shell, Docker socket or arbitrary commands."""
import os, json, time, subprocess, urllib.request
from pathlib import Path
QUEUE=Path('/var/lib/nova-download-vault/admin-control')
def save_session():
    conf={}
    for line in Path('/root/.aria2/aria2.conf').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k,v=line.split('=',1);conf[k.strip()]=v.strip()
    params=['token:'+conf['rpc-secret']] if conf.get('rpc-secret') else []
    data=json.dumps({'jsonrpc':'2.0','id':'maintenance','method':'aria2.saveSession','params':params}).encode()
    req=urllib.request.Request('http://127.0.0.1:'+conf.get('rpc-listen-port','6800')+'/jsonrpc',data=data,headers={'Content-Type':'application/json'})
    result=json.loads(urllib.request.urlopen(req,timeout=10).read())
    if 'error' in result:raise RuntimeError('Cannot save download session')
for action in ('downloader','nas'):
    path=QUEUE/(action+'.request')
    if not path.exists():continue
    if path.is_symlink() or not path.is_file():continue
    try:
        save_session()
        path.unlink() # consume before reboot so requests never replay at boot
        subprocess.run(['/bin/sync'],check=True)
        if action=='nas':
            subprocess.run(['/bin/systemctl','reboot'],check=True)
        else:
            subprocess.run(['/bin/systemctl','restart','aria2.service'],check=True,timeout=120)
        (QUEUE/'last-result.json').write_text(json.dumps({'action':action,'status':'accepted','at':time.time()}))
    except Exception as error:
        if path.exists():path.unlink()
        (QUEUE/'last-result.json').write_text(json.dumps({'action':action,'status':'failed','error':type(error).__name__,'at':time.time()}))
        raise
