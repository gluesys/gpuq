#!/usr/bin/env python3
"""Install lifecycle code DISABLED; never stop an existing DAOS system or wipe storage."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def ssh(host, script):
    subprocess.run(['ssh', '-o', 'BatchMode=yes', host, 'bash', '-se'], input=script, text=True, check=True)


def copy(host, source, dest):
    subprocess.run(['scp', '-q', str(ROOT / source), host + ':' + dest], check=True)


def main():
    for n in range(1, 5):
        host = 'da' + str(n)
        copy(host, 'gpuqueue_node.py', '/var/tmp/gpuqueue-daos-node.new')
        copy(host, 'config/' + host + '.json', '/var/tmp/gpuqueue-daos-node.json.new')
        ssh(host, '''
install -d -m 0755 /etc/gpuqueue-daos
install -d -m 0755 /usr/local/libexec
install -m 0755 /var/tmp/gpuqueue-daos-node.new /usr/local/libexec/gpuqueue-daos-node
if [ ! -e /etc/gpuqueue-daos/node.json ]; then
    install -m 0600 /var/tmp/gpuqueue-daos-node.json.new /etc/gpuqueue-daos/node.json
fi
python3 -m py_compile /usr/local/libexec/gpuqueue-daos-node
''')
    for source, dest in [('gpuqueue_daos.py','gpuqueue-daos'), ('gpuq','gpuq'),
                         ('gpuqueue-wait-exec','gpuqueue-wait-exec'),
                         ('config/config.json','config.json'),
                         ('activate_nvme.py','gpuqueue-daos-activate'),
                         ('systemd/gpuqueue-daos.service','gpuqueue-daos.service')]:
        copy('cxl2', source, '/var/tmp/' + dest + '.new')
    ssh('cxl2', '''
if [ -e /var/lib/gpuqueue-daos/active.json ]; then
    echo 'Active/interrupted DAOS lease exists; finish or recover before deploying.' >&2
    exit 1
fi
install -d -m 0755 /etc/gpuqueue-daos /usr/local/libexec
install -d -m 0755 /var/lib/gpuqueue-daos
if [ ! -e /etc/gpuqueue-daos/config.json ]; then
    install -m 0600 /var/tmp/config.json.new /etc/gpuqueue-daos/config.json
fi
install -m 0755 /var/tmp/gpuqueue-daos.new /usr/local/libexec/gpuqueue-daos
install -m 0750 /var/tmp/gpuqueue-daos-activate.new /usr/local/sbin/gpuqueue-daos-activate
python3 -m py_compile /usr/local/libexec/gpuqueue-daos
install -m 0644 /var/tmp/gpuqueue-daos.service.new /etc/systemd/system/gpuqueue-daos.service
systemctl daemon-reload
systemctl enable gpuqueue-daos
systemctl restart gpuqueue-daos
systemctl is-active --quiet gpuqueue-daos
backup=/var/lib/gpuqueue-daos/backups/$(date +%Y%m%d-%H%M%S)
install -d -m 0700 "$backup"
cp -a /usr/local/bin/gpuq /usr/local/libexec/gpuqueue-wait-exec "$backup/"
# Replace atomically; do not restart task-spooler or interrupt submitted GPU jobs.
install -m 0755 /var/tmp/gpuq.new /usr/local/bin/gpuq.new
mv /usr/local/bin/gpuq.new /usr/local/bin/gpuq
install -m 0755 /var/tmp/gpuqueue-wait-exec.new /usr/local/libexec/gpuqueue-wait-exec.new
mv /usr/local/libexec/gpuqueue-wait-exec.new /usr/local/libexec/gpuqueue-wait-exec
gpuq daos-status
''')


if __name__ == '__main__':
    main()
