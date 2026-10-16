#!/usr/bin/env python3
"""Validate ENABLED disposable NVMe mode under the administrator's maintenance lock.

Every --inst job destroys its own experimental data on da1~da4 at completion.
This does not activate/migrate legacy systems or bypass any disk safety check.
"""
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import time

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / 'results' / 'nvme-activation-20261016'
BASE = '/mnt/nvme1/gpuqueue-daos-e2e'


def remote(argv, host='cxl2', check=True):
    p = subprocess.run(['ssh', '-o', 'BatchMode=yes', host, shlex.join(argv)],
                       text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and p.returncode:
        raise RuntimeError(p.stdout)
    return p


def status():
    return json.loads(remote(['gpuq', 'daos-status']).stdout)


def wait_phase(phase, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = status()
        if value['blocked']:
            raise RuntimeError(str(value['blocked']))
        if value['active'] and value['active']['phase'] == phase:
            return value['active']['id']
        time.sleep(1)
    raise RuntimeError('Did not reach ' + phase)


def clean(lease=None, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = status()
        if value['blocked']:
            raise RuntimeError(str(value['blocked']))
        if not value['active']:
            break
        time.sleep(1)
    else:
        raise RuntimeError('Cleanup timed out')
    if not lease:
        return
    manifest = {}
    script = '''import json,pathlib,subprocess
root=pathlib.Path('/var/lib/gpuqueue-daos')
lease=LEASE
assert not (root/'owner.json').exists()
assert not subprocess.check_output(['podman','ps','-a','--filter','label=gpuq.lease='+lease,'--format','{{.Names}}'],universal_newlines=True).strip()
base=root/'jobs'/lease
audit=json.loads((base/'cleanup.json').read_text())
assert audit['reset']=='blkdiscard'
assert all(not (base/d).exists() for d in ('control','scm','run'))
pci=pathlib.Path('/sys/bus/pci/devices/0000:01:00.0')
assert (pci/'driver').resolve().name=='nvme'
cfg=json.load(open('/etc/gpuqueue-daos/node.json'))
for ctrl in (pci/'nvme').glob('nvme*'):
    assert (ctrl/'serial').read_text().strip()==cfg['serial']
    for ns in ctrl.glob(ctrl.name+'n[0-9]*'):
        out=subprocess.check_output(['wipefs','--noheadings','/dev/'+ns.name],universal_newlines=True)
        assert not out.strip()
print(json.dumps(audit))
'''.replace('LEASE', repr(lease))
    for host in ('da1', 'da2', 'da3', 'da4'):
        manifest[host] = json.loads(remote(['python3', '-c', script], host).stdout)
    (RESULTS / (lease + '-cleanup.json')).write_text(json.dumps(manifest, indent=2) + '\n')


def submit(inst, command, seconds=240, name='test'):
    shell = 'cd "$1"; shift; exec gpuq submit "$@"'
    args = ['bash', '-c', shell, 'bash', BASE + '/nvme/' + (inst or 'inst1'),
            '--time', str(seconds) + 's', '--label', 'nvme-' + name]
    if inst:
        args += ['--inst', inst]
    return int(remote(args + ['--', *command]).stdout)


def finish(job, expected, name, lease=None):
    code = remote(['gpuq', 'wait', str(job)], check=False).returncode
    log = remote(['gpuq', 'log', str(job)]).stdout
    (RESULTS / (name + '.log')).write_text(log)
    print(name, 'job', job, 'exit', code, flush=True)
    assert code == expected, log
    match = re.search(r'gpuq DAOS lease ([0-9a-f]{32})', log)
    lease = lease or (match.group(1) if match else None)
    clean(lease)
    return dict(name=name, job_id=job, exit_code=code, lease=lease)


def enable_node(enabled):
    script = '''import json,os
p='/etc/gpuqueue-daos/node.json'
c=json.load(open(p)); c['enabled']=ENABLED
with open(p+'.tmp','w') as f:
    os.fchmod(f.fileno(),0o600); json.dump(c,f,indent=2)
os.replace(p+'.tmp',p)
'''.replace('ENABLED', repr(enabled))
    remote(['python3', '-c', script], 'da4')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--first-job', type=int, help='Use an already submitted inst1 smoke job.')
    args = parser.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    value = status()
    assert value['enabled'] and value['maintenance'] and not value['blocked'], value
    records = []
    try:
        for inst in ('inst1', 'inst2'):
            job = args.first_job if inst == 'inst1' and args.first_job else submit(
                inst, ['bash', BASE + '/smoke.sh'], 300, inst + '-smoke')
            record = finish(job, 0, inst + '-smoke')
            log = (RESULTS / (inst + '-smoke.log')).read_text()
            assert 'FOUR_SERVER_DAOS_ROUNDTRIP_OK' in log and 'GPU_CUDA_SMOKE_OK' in log
            records.append(record)
        job = submit('inst1', ['bash', '-c', 'echo EXPECTED_FAILURE; exit 23'], name='failure')
        records.append(finish(job, 23, 'failure'))
        job = submit('inst2', ['sleep', '120'], seconds=40, name='timeout')
        records.append(finish(job, 124, 'timeout'))
        job = submit('inst1', ['sleep', '120'], name='cancel')
        lease = wait_phase('running')
        remote(['gpuq', 'cancel', str(job)])
        records.append(finish(job, 130, 'cancel', lease))
        job = submit('inst2', ['sleep', '10'], name='cleanup-failure')
        lease = wait_phase('running')
        try:
            enable_node(False)
            code = remote(['gpuq', 'wait', str(job)], check=False).returncode
            assert code == 75
            value = status()
            assert value['blocked'] and value['active']['id'] == lease
            (RESULTS / 'cleanup-held.json').write_text(json.dumps(value, indent=2) + '\n')
            (RESULTS / 'cleanup-failure.log').write_text(remote(['gpuq', 'log', str(job)]).stdout)
            records.append(dict(name='cleanup-failure', job_id=job, exit_code=75, lease=lease))
            marker = submit(None, ['printf', 'NVME_HOLD_RELEASED\n'], seconds=180, name='hold-marker')
            time.sleep(6)
            log = remote(['gpuq', 'log', str(marker)]).stdout
            assert 'NVME_HOLD_RELEASED' not in log, log
        finally:
            enable_node(True)
        remote(['/usr/local/libexec/gpuqueue-daos', 'recover'])
        clean(lease)
        records.append(finish(marker, 0, 'hold-marker'))
        job = submit('inst1', ['sleep', '120'], name='broker-crash')
        lease = wait_phase('running')
        remote(['systemctl', 'kill', '--kill-who=all', '--signal=SIGKILL', 'gpuqueue-daos'])
        code = remote(['gpuq', 'wait', str(job)], check=False).returncode
        assert code == 75
        remote(['systemctl', 'restart', 'gpuqueue-daos'])
        deadline = time.monotonic() + 20
        while True:
            try:
                value = status()
                break
            except (RuntimeError, ValueError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(.2)
        assert value['blocked'] and value['active']['id'] == lease
        (RESULTS / 'crash-held.json').write_text(json.dumps(value, indent=2) + '\n')
        remote(['/usr/local/libexec/gpuqueue-daos', 'recover'])
        clean(lease)
        (RESULTS / 'broker-crash.log').write_text(remote(['gpuq', 'log', str(job)]).stdout)
        records.append(dict(name='broker-crash', job_id=job, exit_code=75, lease=lease))
        print('NVME_ALL_SCENARIOS_PASSED', flush=True)
    finally:
        (RESULTS / 'summary.json').write_text(json.dumps(records, indent=2) + '\n')


if __name__ == '__main__':
    main()
