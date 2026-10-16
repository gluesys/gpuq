#!/usr/bin/env python3
"""Provision FILE-backed isolated test containers. Does not arm NVMe lifecycle."""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent


def remote(host, script):
    return subprocess.run(['ssh','-o','BatchMode=yes',host,'bash','-se'], input=script,
                           text=True, check=True, stdout=subprocess.PIPE).stdout


def main():
    image = remote('cxl2', "podman image inspect localhost/gpuq-daos:2.8.1 --format '{{.Id}}'\n").strip()
    if not image.startswith('sha256:'):
        image = 'sha256:' + image
    remote('cxl2', '''
mkdir -p /mnt/nvme1/gpuqueue-daos-e2e
podman save -o /mnt/nvme1/gpuqueue-daos-e2e/image.tar localhost/gpuq-daos:2.8.1
''')
    for n in range(1,5):
        host='da'+str(n)
        cfg=json.loads((ROOT/'config'/ (host+'.json')).read_text())
        cfg.update(enabled=True,storage_mode='file')
        cfg['conflicting_units']=[]
        for inst,p in cfg['profiles'].items():
            p.update(image=image,system='gpuqt_'+inst,port=15401,fabric_port=33516,
                     access_points=['192.0.2.91:15401'],targets=1,first_core=12,
                     scm_gib=4,hugepages=5120)
        text=json.dumps(cfg)
        # Send structured JSON via stdin, not shell interpolation.
        subprocess.run(['ssh','-o','BatchMode=yes',host,
                        'umask 077; cat > /etc/gpuqueue-daos/test-node.json'],
                       input=text,text=True,check=True)
        print(remote('cxl2','scp -q /mnt/nvme1/gpuqueue-daos-e2e/image.tar '+host+':/var/tmp/gpuqueue-daos-test-image.tar\n'),end='')
        print(remote(host,'podman load -i /var/tmp/gpuqueue-daos-test-image.tar\n'),end='')
    cfg=json.loads((ROOT/'config/config.json').read_text())
    cfg.update(enabled=True,test_storage=True,state_dir='/var/lib/gpuqueue-daos-test',startup_timeout=120)
    for inst,p in cfg['profiles'].items():
        p.update(client_image=image,system='gpuqt_'+inst,port=15401,targets=1,scm_gib=4,
                 hugepages=5120,first_core=12,fabric_port=33516,access_points=['192.0.2.91:15401'])
    subprocess.run(['ssh','-o','BatchMode=yes','cxl2',
                    'umask 077; cat > /etc/gpuqueue-daos/test-config.json'],
                   input=json.dumps(cfg),text=True,check=True)
    print(remote('cxl2','''
install -d -m 0755 /var/lib/gpuqueue-daos-test
if [ -e /var/lib/gpuqueue-daos-test/active.json ]; then
    echo 'Interrupted test lease exists; recover first' >&2; exit 1
fi
systemd-run --unit=gpuqueue-daos-test --property=RuntimeDirectory=gpuqueue-daos-test --property=RuntimeDirectoryMode=0750 --property=Group=gpuqueue --property=KillMode=mixed --property=TimeoutStopSec=600 --property=LimitNOFILE=65536 /usr/local/libexec/gpuqueue-daos --config /etc/gpuqueue-daos/test-config.json --socket /run/gpuqueue-daos-test/control.sock serve
'''),end='')
    (ROOT/'results/test-image-id.txt').write_text(image+'\n')


if __name__=='__main__':
    main()
