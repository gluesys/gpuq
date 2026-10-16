#!/usr/bin/env python3
"""Recovery/queue-hold tests against the disposable file-backed four-node environment."""
import json
from pathlib import Path
import shlex
import subprocess
import time
from run_e2e import ROOT, TEST_SOCKET, remote, status, submit, finish, wait_phase, wait_clean, wait_broker


def node_enabled(enabled):
    code='import json; p="/etc/gpuqueue-daos/test-node.json"; c=json.load(open(p)); c["enabled"]='+repr(enabled)+'; f=open(p,"w"); json.dump(c,f); f.close()'
    subprocess.run(['ssh','-o','BatchMode=yes','da4',shlex.join(['python3','-c',code])],check=True)


def recover():
    remote(['/usr/local/libexec/gpuqueue-daos','--socket',TEST_SOCKET,'recover'])
    wait_clean()


def main():
    results=[]
    try:
        wait_broker()
        job=submit('inst1',['sleep','8'],label='cleanup-failure')
        wait_phase('running')
        node_enabled(False)
        results.append(finish(job,75,'cleanup-failure'))
        assert status()['blocked'], 'Cleanup failure must hold the queue'
        probe=int(remote(['env','GPUQ_DAOS_SOCKET='+TEST_SOCKET,'gpuq','submit','--time','120s',
                          '--label','lifecycle-hold-probe','--','printf','HOLD_RELEASED\n']).stdout.strip())
        time.sleep(3)
        log=remote(['gpuq','log',str(probe)],check=False).stdout
        assert 'HOLD_RELEASED' not in log, 'Next payload started before cleanup'
        node_enabled(True)
        recover()
        results.append(finish(probe,0,'hold-probe'))
        assert 'HOLD_RELEASED' in (ROOT/'results/e2e-hold-probe.log').read_text()
        job=submit('inst2',['sleep','120'],label='broker-crash')
        running=wait_phase('running')['active']
        remote(['systemctl','kill','--kill-whom=all','--signal=KILL','gpuqueue-daos-test'])
        # The workload lives in its own cgroup; recovery must stop it explicitly.
        remote(['systemctl','restart','gpuqueue-daos-test'])
        for _ in range(20):
            try:
                crashed=status()
                break
            except Exception:
                time.sleep(.2)
        assert crashed['blocked'] and crashed['active']['id']==running['id']
        recover()
        state=remote(['systemctl','show','gpuq-payload-'+running['id']+'.service',
                      '-p','ActiveState','--value']).stdout.strip()
        assert state in ('inactive','failed'), state
        code=remote(['gpuq','wait',str(job)],check=False).returncode
        results.append(dict(name='broker-crash-recovery',job_id=job,exit_code=code,cleanup_complete=True))
        (ROOT/'results/e2e-broker-crash.log').write_text(remote(['gpuq','log',str(job)]).stdout)
        print('broker crash recovery passed',flush=True)
    finally:
        node_enabled(True)
        (ROOT/'results/fault-e2e-summary.json').write_text(json.dumps(results,indent=2)+'\n')


if __name__=='__main__':
    main()
