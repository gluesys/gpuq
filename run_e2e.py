#!/usr/bin/env python3
"""Exercise the installed gpuq against the isolated four-server FILE-backed test broker."""
import json
from pathlib import Path
import shlex
import subprocess
import time

ROOT=Path(__file__).resolve().parent
TEST_SOCKET='/run/gpuqueue-daos-test/control.sock'
BASE='/mnt/nvme1/gpuqueue-daos-e2e'


def remote(argv, check=True):
    p=subprocess.run(['ssh','-o','BatchMode=yes','cxl2',shlex.join(argv)],
                     text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    if check and p.returncode:
        raise RuntimeError(p.stdout)
    return p


def status():
    return json.loads(remote(['env','GPUQ_DAOS_SOCKET='+TEST_SOCKET,'gpuq','daos-status']).stdout)


def wait_broker(timeout=20):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            return status()
        except (RuntimeError,ValueError):
            time.sleep(.2)
    raise RuntimeError('Test broker is not running; provision/start its transient service first')


def wait_phase(phase, timeout=180):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=status()
        if value['active'] and value['active']['phase']==phase:
            return value
        time.sleep(1)
    raise RuntimeError('Did not reach phase '+phase)


def wait_clean(timeout=300):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=status()
        if value['blocked']:
            raise RuntimeError('Recovery hold: '+str(value['blocked']))
        if not value['active']:
            return
        time.sleep(1)
    raise RuntimeError('Cleanup timed out')


def submit(inst, argv, seconds=180, label='test'):
    user='daos-inst'+('1' if inst=='inst1' else '2')+'-check'
    group='daos_'+inst
    workspace=BASE+'/'+inst
    args=['runuser','-u',user,'-g',group,'-G','gpuqueue','--',
          'env','GPUQ_DAOS_SOCKET='+TEST_SOCKET,
          'bash','-c','cd "$1"; shift; exec gpuq submit "$@"','bash',workspace,
          '--time',str(seconds)+'s','--label','lifecycle-'+label]
    if inst:
        args+=['--inst',inst]
    args+=['--',*argv]
    return int(remote(args).stdout.strip())


def finish(job, expected, name):
    code=remote(['gpuq','wait',str(job)],check=False).returncode
    log=remote(['gpuq','log',str(job)]).stdout
    (ROOT/'results'/('e2e-'+name+'.log')).write_text(log)
    print(name, 'job',job,'exit',code,flush=True)
    if code!=expected:
        raise RuntimeError(name+' unexpected exit '+str(code)+'\n'+log)
    return dict(name=name,job_id=job,exit_code=code)


def main():
    records=[]
    try:
        wait_broker()
        for inst in ('inst1','inst2'):
            p=remote(['install','-d','-o','daos-'+inst+'-check','-g','daos_'+inst,'-m','0750',BASE+'/'+inst])
            job=submit(inst,['env','GPUQ_SMOKE_POOL_SIZE=8G','bash',BASE+'/smoke.sh'],seconds=240,label=inst+'-smoke')
            record=finish(job,0,inst+'-smoke')
            wait_clean()
            log=(ROOT/'results'/('e2e-'+inst+'-smoke.log')).read_text()
            assert 'FOUR_SERVER_DAOS_ROUNDTRIP_OK' in log
            assert 'GPU_CUDA_SMOKE_OK' in log
            records.append(record)
        job=submit('inst1',['bash','-c','echo EXPECTED_FAILURE; exit 23'],label='failure')
        records.append(finish(job,23,'failure'))
        wait_clean()
        job=submit('inst2',['sleep','120'],seconds=30,label='timeout')
        records.append(finish(job,124,'timeout'))
        wait_clean()
        job=submit('inst1',['sleep','120'],label='cancel')
        wait_phase('running')
        remote(['gpuq','cancel',str(job)])
        # task-spooler may mark the client done before root cleanup, but the gate holds next jobs.
        result=remote(['gpuq','wait',str(job)],check=False)
        records.append(dict(name='cancel',job_id=job,exit_code=result.returncode))
        wait_clean()
        (ROOT/'results'/'e2e-cancel.log').write_text(remote(['gpuq','log',str(job)]).stdout)
    finally:
        (ROOT/'results'/'e2e-summary.json').write_text(json.dumps(records,indent=2)+'\n')


if __name__=='__main__':
    main()
