#!/usr/bin/python3
"""Run on cxl2 as root. Default is read-only plan; execution ERASES EXISTING DAOS DATA."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import time
import uuid


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute',action='store_true')
    p.add_argument('--destroy-existing-data',action='store_true')
    args=p.parse_args()
    if os.getuid()!=0:
        p.error('Run on cxl2 as root')
    api=runpy.run_path('/usr/local/libexec/gpuqueue-daos')
    cfg_path=Path('/etc/gpuqueue-daos/config.json')
    cfg=json.loads(cfg_path.read_text())
    if cfg.get('test_storage') or cfg['hosts']!=['da1','da2','da3','da4']:
        p.error('This activation is only for the dedicated da1~da4 production config')
    print('DESTROYS both existing DAOS systems and all pools on these NVMe:')
    for host,serial in zip(cfg['hosts'],['EXAMPLE_NVME_SERIAL_1','EXAMPLE_NVME_SERIAL_2','EXAMPLE_NVME_SERIAL_3','EXAMPLE_NVME_SERIAL_4']):
        print('  {}  0000:01:00.0  {}'.format(host,serial))
    if not args.execute:
        print('PLAN ONLY. Existing systems and data are unchanged.')
        return
    if not args.destroy_existing_data:
        p.error('--execute also requires --destroy-existing-data after owner approval')
    command=api['command']
    atomic=api['atomic_json']
    root=Path(cfg['state_dir'])
    active=root/'active.json'
    blocked=root/'blocked.json'
    # Coordinate with the broker across processes; never overwrite an in-flight lease.
    lease_lock=(root/'lease.lock').open('a')
    fcntl.flock(lease_lock.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
    if active.exists():
        lease=json.loads(active.read_text())
        if not lease.get('activation'):
            p.error('A normal lease exists; finish/recover it before activation')
    else:
        if blocked.exists():
            p.error('Resolve existing recovery hold first')
        def idle():
            listing=command(['/usr/local/bin/gpuq','list']).stdout
            if not re.search(r'\[run=0/1\]',listing):
                raise RuntimeError('GPU queue is not idle; do not interrupt institution jobs')
            gpu=command(['nvidia-smi','--query-compute-apps=pid','--format=csv,noheader']).stdout.strip()
            if gpu:
                raise RuntimeError('GPU has active compute processes')
        idle()
        atomic(blocked,{'reason':'Approved NVMe migration in progress'})
        try:
            idle()  # Close the admission race before changing any DAOS service.
        except Exception:
            blocked.unlink()
            raise
        lease=dict(id=uuid.uuid4().hex,uid=0,inst='inst1',cwd='/mnt/nvme1',
                   phase='activating',activation=True,started=time.time(),profile=cfg['profiles']['inst1'])
        atomic(active,lease)
    # Until this script finishes, all new GPU workloads remain behind the persistent hold.
    for n,host in enumerate(cfg['hosts'],1):
        serial=['EXAMPLE_NVME_SERIAL_1','EXAMPLE_NVME_SERIAL_2','EXAMPLE_NVME_SERIAL_3','EXAMPLE_NVME_SERIAL_4'][n-1]
        script='''import json,os,subprocess
p='/etc/gpuqueue-daos/node.json'
c=json.load(open(p))
assert c['host']==HOST
assert c['serial']==SERIAL and c['pci']=='0000:01:00.0'
assert c['vendor']=='0x144d' and c['device']=='0xa80a'
for v in c['profiles'].values():
    image=v['image']
    assert image.startswith('sha256:') and len(image)==71
    subprocess.run(['podman','image','exists',image],check=True)
c['enabled']=True
with open(p+'.tmp','w') as f:
    os.chmod(p+'.tmp',0o600)
    json.dump(c,f,indent=2)
os.replace(p+'.tmp',p)
'''.replace('HOST',repr(host)).replace('SERIAL',repr(serial))
        subprocess.run(['ssh','-o','BatchMode=yes',host,'python3','-'],input=script,text=True,check=True)
    for inst,old in [('inst1','da12'),('inst2','da34')]:
        agent='daos-'+old+'-nvme-agent.service'
        if command(['systemctl','is-active',agent],check=False).returncode==0:
            command(['dmg','-o','/etc/daos/'+old+'-nvme/daos_control.yml','system','stop'],check=False)
        command(['systemctl','disable','--now',agent])
    for n,host in enumerate(cfg['hosts'],1):
        unit='daos-'+('da12' if n<=2 else 'da34')+'-nvme-server.service'
        command(['ssh','-o','BatchMode=yes',host,'systemctl','disable','--now',unit],timeout=150)
    backend=api['Backend'](cfg)
    # Non-destructive claim checks ALL serials/in-use state before any discard takes place.
    backend.all_nodes('claim-reset',lease)
    lease['phase']='cleaning'
    atomic(active,lease)
    cfg['enabled']=True
    atomic(cfg_path,cfg)
    # Persistent active/blocked records now prevent admission while the broker reloads.
    lease_lock.close()
    command(['systemctl','restart','gpuqueue-daos'],timeout=30)
    deadline=time.monotonic()+15
    while True:
        try:
            result=api['rpc']({'action':'recover'},path='/run/gpuqueue-daos/control.sock')
            if result:
                raise RuntimeError('Reset failed; persistent hold retained for recovery')
            break
        except (ConnectionRefusedError,FileNotFoundError):
            if time.monotonic()>deadline:
                raise
            time.sleep(.2)
    print('Activated: four-server disposable DAOS jobs; no pools are pre-created.')


if __name__=='__main__':
    main()
