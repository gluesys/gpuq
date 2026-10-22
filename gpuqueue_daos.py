#!/usr/bin/python3
"""Privileged lifecycle broker; task-spooler clients never own cleanup."""
import argparse
import concurrent.futures
import fcntl
import grp
import json
import os
from pathlib import Path
import pwd
import re
import select
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import uuid

CONFIG = '/etc/gpuqueue-daos/config.json'
MAINTENANCE = Path('/etc/gpuqueue-daos/maintenance.json')
SOCKET = os.environ.get('GPUQ_DAOS_SOCKET', '/run/gpuqueue-daos/control.sock')
STOR_RE = re.compile(r'(lustre|nfsrdma|nvmeof)([14])')
SAFE_ENV = dict(PATH='/usr/sbin:/usr/bin:/sbin:/bin', HOME='/root', LANG='C.UTF-8')


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        os.chmod(tmp, 0o600)
        json.dump(value, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def command(argv, timeout=90, check=True):
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=timeout, env=SAFE_ENV)
    if check and result.returncode:
        raise RuntimeError('{}: {}'.format(' '.join(argv[:3]), result.stdout[-3000:]))
    return result


class Backend:
    def __init__(self, config):
        self.config = config

    def node(self, host, action, lease):
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+', host):
            raise ValueError('Invalid host')
        result = command(['/usr/bin/ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                          '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2',
                          host, '/usr/local/libexec/gpuqueue-daos-node',
                          *(['--test'] if self.config.get('test_storage') else []), action,
                          lease['id'], lease['inst']], timeout=240)
        return result.stdout.strip()

    def all_nodes(self, action, lease, hosts=None):
        errors = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            calls = {pool.submit(self.node, h, action, lease): h for h in (hosts or self.config['hosts'])}
            for future, host in calls.items():
                try:
                    reply = future.result()
                    if action == 'preflight' and not STOR_RE.fullmatch(lease['inst']):
                        info = json.loads(reply)
                        profile = self.profile(lease)
                        expected = profile.get('server_image', profile['client_image'])
                        if info['image'] != expected or info['system'] != profile['system'] or info['port'] != profile['port']:
                            raise RuntimeError('Server image/system/port does not match the pinned institution profile')
                except Exception as error:
                    errors.append('{}: {}'.format(host, error))
        if errors:
            raise RuntimeError('; '.join(errors))

    def profile(self, lease):
        return self.config['profiles'][lease['inst']]

    def client_name(self, lease):
        return 'gpuq-agent-' + lease['id']

    def ctl(self, lease, *args, check=True):
        return command(['/usr/bin/podman', 'exec', self.client_name(lease), 'dmg',
                        '-o', '/gpuq/daos_control.yml', *args], check=check, timeout=120)

    def start(self, lease, cancelled, emit):
        # Preflight every node before mutating any node. No legacy systems are stopped here.
        self.all_nodes('preflight', lease)
        if cancelled.is_set():
            raise InterruptedError('Cancelled before DAOS startup')
        self.all_nodes('start', lease)
        if cancelled.is_set():
            raise InterruptedError('Cancelled during DAOS startup')
        p = self.profile(lease)
        root = Path(self.config['state_dir']) / 'clients' / lease['id']
        root.mkdir(parents=True, exist_ok=True)
        # JSON is valid YAML; no YAML dependency needed.
        common = {'name': p['system'], 'port': p['port'],
                  'transport_config': {'allow_insecure': True}}
        control_hosts = self.config.get('control_hosts', self.config['hosts'])
        (root / 'daos_control.yml').write_text(json.dumps(dict(common, hostlist=control_hosts)))
        (root / 'daos_agent.yml').write_text(json.dumps(dict(common,
            access_points=[control_hosts[0] + ':' + str(p['port'])],
            runtime_dir='/gpuq/run', log_file='/gpuq/agent.log',
            control_log_mask='INFO', disable_caching=True,
            include_fabric_ifaces=[self.config.get('client_fabric_iface', 'ibp129s0')])))
        (root / 'run').mkdir(mode=0o755, exist_ok=True)
        image = p['client_image']
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
            raise ValueError('Client image must be a pinned local sha256 image ID')
        command(['/usr/bin/podman', 'run', '-d', '--pull=never', '--name', self.client_name(lease),
                 '--label', 'gpuq.lease=' + lease['id'], '--network=host', '--pid=host', '--privileged',
                 '--ulimit', 'memlock=-1:-1', '-v', str(root) + ':/gpuq:rw',
                 '-v', '/etc/passwd:/etc/passwd:ro', '-v', '/etc/group:/etc/group:ro',
                 '-v', lease['cwd'] + ':' + lease['cwd'] + ':rw',
                 '--entrypoint', '/usr/bin/daos_agent', image, '-o', '/gpuq/daos_agent.yml'])
        # Format only this freshly allocated system, never a pre-existing host system.
        deadline = time.monotonic() + self.config.get('startup_timeout', 180)
        while True:
            if cancelled.is_set():
                raise InterruptedError('Cancelled during DAOS format')
            result = self.ctl(lease, 'storage', 'format', check=False)
            if result.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('DAOS format failed: ' + result.stdout[-2500:])
            time.sleep(2)
        while time.monotonic() < deadline:
            if cancelled.is_set():
                raise InterruptedError('Cancelled while waiting for DAOS ranks')
            result = self.ctl(lease, '-j', 'system', 'query', check=False)
            if result.returncode == 0:
                try:
                    members = json.loads(result.stdout)['response']['members']
                    if len(members) == 4 and all(m['state'].lower() == 'joined' for m in members):
                        emit('DAOS ready: 4 joined ranks; create pools in your experiment.\n')
                        return
                except (KeyError, TypeError, ValueError):
                    pass
            time.sleep(2)
        raise RuntimeError('Four DAOS ranks did not join')

    def payload(self, lease, request, env):
        user = pwd.getpwuid(lease['uid'])
        argv = ['/usr/bin/systemd-run', '--quiet', '--wait', '--pipe', '--collect',
                '--unit=gpuq-payload-' + lease['id'], '--uid=' + user.pw_name,
                '--gid=' + str(user.pw_gid), '--working-directory=' + request['cwd'],
                '--property=SupplementaryGroups=gpuqueue',
                '--property=KillMode=control-group', '--property=TimeoutStopSec=20s',
                '--property=RuntimeMaxSec=' + str(request['seconds'])]
        argv += ['--setenv=' + k + '=' + v for k, v in env.items()]
        return subprocess.Popen(argv + ['--'] + request['argv'], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=SAFE_ENV)

    def stop_payload(self, lease):
        unit = 'gpuq-payload-' + lease['id'] + '.service'
        result = command(['/usr/bin/systemctl', 'stop', unit], timeout=45, check=False)
        state = command(['/usr/bin/systemctl', 'show', unit, '-p', 'ActiveState', '--value'],
                        check=False).stdout.strip()
        if state not in ('inactive', 'failed', ''):
            raise RuntimeError('Payload cgroup did not stop: ' + result.stdout)

    def cleanup(self, lease, emit):
        # Stop gracefully if an agent was created, then stop all containers regardless of RPC result.
        name = self.client_name(lease)
        exists = command(['/usr/bin/podman', 'container', 'exists', name], check=False).returncode == 0
        errors = []
        if exists:
            try:
                self.ctl(lease, 'system', 'stop', check=False)
            except Exception as error:
                emit('Graceful system stop unavailable; stopping owned containers: {}\n'.format(error))
            try:
                command(['/usr/bin/podman', 'stop', '--time', '30', name], timeout=50)
                command(['/usr/bin/podman', 'rm', name])
            except Exception as error:
                errors.append(str(error))
        try:
            self.all_nodes('stop', lease)
        except Exception as error:
            errors.append(str(error))
        if errors:
            raise RuntimeError('; '.join(errors))
        # No disk can be wiped until ALL nodes have proved their engines stopped.
        emit('All DAOS engines stopped; resetting {}.\n'.format(
            'isolated test files' if self.config.get('test_storage') else 'allowlisted research NVMe'))
        self.all_nodes('wipe', lease)
        emit('DAOS cleanup verified on all four servers.\n')


class StorBackend(Backend):
    """Lustre / ZFS NFSoRDMA / NVMe-oF on the research NVMe of the first N nodes.

    The node helper owns the servers; this class owns the cxl2 side (mount, connect, md)
    and the order: client torn down, then every node stopped, and only then wiped.
    """

    def scfg(self):
        return self.config['stor']

    def hosts(self, lease):
        return self.config['hosts'][:lease['profile']['nodes']]

    def mount_path(self, lease):
        return Path(self.scfg().get('client_mount_root', '/mnt/gpuq')) / lease['id']

    def state_path(self, lease):
        return Path(self.config['state_dir']) / 'clients' / lease['id'] / 'stor.json'

    def nqn(self, lease, index):
        return 'nqn.2026-10.com.gluesys:gpuq:{}:{}'.format(lease['id'], index)

    def fsname(self, lease):
        return 'g' + lease['id'][:7]

    def record(self, lease, **state):
        path = self.state_path(lease)
        old = json.loads(path.read_text()) if path.exists() else {}
        old.update(state)
        atomic_json(path, old)
        return old

    def start(self, lease, cancelled, emit):
        hosts = self.hosts(lease)
        self.all_nodes('preflight', lease, hosts)
        if cancelled.is_set():
            raise InterruptedError('Cancelled before storage startup')
        # Node 0 carries the Lustre MGS/MDT; OSTs register with it, so it starts first.
        self.node(hosts[0], 'start', lease)
        if len(hosts) > 1:
            self.all_nodes('start', lease, hosts[1:])
        if cancelled.is_set():
            raise InterruptedError('Cancelled during storage startup')
        self.client_setup(lease)
        state = json.loads(self.state_path(lease).read_text())
        emit('Storage ready: {} on {} node(s) at {}{}\n'.format(
            lease['profile']['kind'], len(hosts), state['path'],
            ' (device ' + state['device'] + ')' if state.get('device') else ''))

    def client_setup(self, lease):
        kind, scfg = lease['profile']['kind'], self.scfg()
        ips = [scfg['server_ips'][h] for h in self.hosts(lease)]
        mnt = self.mount_path(lease)
        mnt.mkdir(parents=True, exist_ok=True)
        self.record(lease, path=str(mnt), phase='client-setup')
        device = None
        if kind == 'lustre':
            command(['/usr/sbin/modprobe', 'lustre'])
            command(['/usr/sbin/lnetctl', 'lnet', 'configure', '--all'], check=False)
            command(['/usr/bin/mount', '-t', 'lustre', '{}@o2ib:/{}'.format(ips[0], self.fsname(lease)), str(mnt)],
                    timeout=180)
            if len(ips) > 1:
                command(['/usr/bin/lfs', 'setstripe', '-c', '-1', str(mnt)])
        elif kind == 'nfsrdma':
            command(['/usr/sbin/modprobe', 'rpcrdma'])
            command(['/usr/bin/mount', '-t', 'nfs', '-o', scfg.get(
                'nfs_mount_options', 'rdma,port=20049,vers=3,rsize=1048576,wsize=1048576,lookupcache=positive,hard'),
                '{}:{}/jobs/{}/nfs'.format(ips[0], scfg.get('server_state_dir', '/var/lib/gpuqueue-daos'), lease['id']),
                str(mnt)], timeout=180)
        else:
            transport = scfg.get('nvmeof_transport', 'rdma')
            command(['/usr/sbin/modprobe', 'nvme-' + transport])
            devices = []
            for index, ip in enumerate(ips):
                command(['/usr/sbin/nvme', 'connect', '-t', transport, '-a', ip,
                         '-s', str(scfg.get('nvmeof_port', 4420)), '-n', self.nqn(lease, index)], timeout=120)
                devices.append(self.nvme_device(self.nqn(lease, index)))
            if len(devices) == 1:
                device = devices[0]
            else:
                # device-mapper stripe, 1 MiB chunks; no on-disk metadata, unlike md (whose newer
                # mdadm also expects md_mod parameters this kernel lacks).
                name = 'gq' + lease['id'][:8]
                sectors = min(int(command(['/usr/sbin/blockdev', '--getsz', dev]).stdout) for dev in devices)
                chunk = 2048
                length = sectors // chunk * chunk * len(devices)
                table = '0 {} striped {} {} {}'.format(length, len(devices), chunk,
                                                       ' '.join(dev + ' 0' for dev in devices))
                self.record(lease, dm=name)
                command(['/usr/sbin/dmsetup', 'create', name, '--table', table])
                device = '/dev/mapper/' + name
            command(['/usr/sbin/mkfs.xfs', '-f', '-q', device], timeout=300)
            command(['/usr/bin/mount', device, str(mnt)], timeout=120)
        if not os.path.ismount(str(mnt)):
            raise RuntimeError('Storage mount did not appear at ' + str(mnt))
        user = pwd.getpwuid(lease['uid'])
        os.chown(str(mnt), user.pw_uid, user.pw_gid)
        os.chmod(str(mnt), 0o755)
        self.record(lease, phase='mounted', device=device)

    def nvme_device(self, nqn):
        """Namespace block device for a connected subsystem (native multipath head or plain)."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for root in ('/sys/class/nvme-subsystem', '/sys/class/nvme'):
                for item in Path(root).glob('nvme*'):
                    try:
                        if (item / 'subsysnqn').read_text().strip() != nqn:
                            continue
                    except OSError:
                        continue
                    names = [p.name for p in item.iterdir() if re.fullmatch(r'nvme[0-9]+n[0-9]+', p.name)]
                    if len(names) == 1 and Path('/dev', names[0]).exists():
                        return '/dev/' + names[0]
            time.sleep(0.5)
        raise RuntimeError('NVMe-oF namespace did not appear for ' + nqn)

    def payload_env(self, lease):
        state = json.loads(self.state_path(lease).read_text())
        return dict(GPUQ_STOR_KIND=lease['profile']['kind'], GPUQ_STOR_NODES=str(lease['profile']['nodes']),
                    GPUQ_STOR_PATH=state['path'], GPUQ_STOR_DEV=state.get('device') or '')

    def client_teardown(self, lease):
        mnt = self.mount_path(lease)
        if os.path.ismount(str(mnt)):
            command(['/usr/bin/sync'], timeout=300, check=False)
            if command(['/usr/bin/umount', str(mnt)], timeout=300, check=False).returncode:
                command(['/usr/bin/umount', '-f', str(mnt)], timeout=120, check=False)
            if os.path.ismount(str(mnt)):
                raise RuntimeError('Client mount still busy: ' + str(mnt))
        dm = '/dev/mapper/gq' + lease['id'][:8]
        if os.path.exists(dm):
            command(['/usr/sbin/dmsetup', 'remove', '--retry', os.path.basename(dm)], timeout=120)
        prefix = 'nqn.2026-10.com.gluesys:gpuq:{}:'.format(lease['id'])
        for ctrl in Path('/sys/class/nvme').glob('nvme*'):
            try:
                nqn = (ctrl / 'subsysnqn').read_text().strip()
            except OSError:
                continue
            if nqn.startswith(prefix):
                command(['/usr/sbin/nvme', 'disconnect', '-n', nqn], timeout=120)
        left = [c.name for c in Path('/sys/class/nvme').glob('nvme*')
                if (c / 'subsysnqn').exists() and (c / 'subsysnqn').read_text().strip().startswith(prefix)]
        if left:
            raise RuntimeError('NVMe-oF controllers still connected: ' + ', '.join(left))
        if mnt.is_dir():
            mnt.rmdir()
        if self.state_path(lease).exists():
            self.record(lease, phase='client-clean')

    def cleanup(self, lease, emit):
        self.client_teardown(lease)
        hosts = self.hosts(lease)
        errors = []
        if len(hosts) > 1:
            try:
                self.all_nodes('stop', lease, hosts[1:])
            except Exception as error:
                errors.append(str(error))
        try:
            self.node(hosts[0], 'stop', lease)
        except Exception as error:
            errors.append('{}: {}'.format(hosts[0], error))
        if errors:
            raise RuntimeError('; '.join(errors))
        # No disk can be wiped until ALL participating nodes have proved their servers stopped.
        emit('Storage servers stopped; resetting research NVMe on {} node(s).\n'.format(len(hosts)))
        self.all_nodes('wipe', lease, hosts)
        emit('Storage cleanup verified on {} node(s).\n'.format(len(hosts)))

    def control(self, lease, op):
        if op == 'status':
            return json.loads(self.state_path(lease).read_text())
        mnt = self.mount_path(lease)
        command(['/usr/bin/sync'], timeout=300, check=False)
        if lease['profile']['kind'] == 'lustre':
            command(['/usr/sbin/lctl', 'set_param', '-n', 'ldlm.namespaces.*.lru_size=clear'], check=False)
        Path('/proc/sys/vm/drop_caches').write_text('3\n')
        self.all_nodes(op, lease, self.hosts(lease))
        return dict(op=op, path=str(mnt))


class Broker:
    def __init__(self, config, backend=None, stor_backend=None):
        self.config = config
        self.backend = backend or Backend(config)
        self.stor_backend = stor_backend or StorBackend(config)
        self.root = Path(config['state_dir'])
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.active = self.root / 'active.json'
        self.blocked = self.root / 'blocked.json'
        if self.active.exists():
            atomic_json(self.blocked, {'reason': 'Interrupted lease; root recovery required'})

    def backend_for(self, lease):
        return self.stor_backend if STOR_RE.fullmatch(lease['inst']) else self.backend

    def status(self):
        return {'enabled': self.config.get('enabled', False),
                'maintenance': json.loads(MAINTENANCE.read_text()) if MAINTENANCE.exists() else None,
                'active': json.loads(self.active.read_text()) if self.active.exists() else None,
                'blocked': json.loads(self.blocked.read_text()) if self.blocked.exists() else None}

    def acquire(self):
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('Another lifecycle owns the GPU/DAOS lease')
        guard = (self.root / 'lease.lock').open('a')
        try:
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return guard
        except OSError:
            guard.close()
            self.lock.release()
            raise RuntimeError('Another lifecycle or migration owns the GPU/DAOS lease')

    def release(self, guard):
        guard.close()  # Releases the process-shared flock as well as the thread lock.
        self.lock.release()

    def validate(self, uid, request):
        if uid != 0 and MAINTENANCE.exists():
            raise ValueError('Maintenance locked: ' + json.loads(MAINTENANCE.read_text())['reason'])
        if not self.config.get('enabled', False):
            raise ValueError('DAOS lifecycle is not enabled; existing DAOS data is preserved')
        stor = STOR_RE.fullmatch(request.get('inst') or '')
        if stor:
            if not self.config.get('stor', {}).get('enabled'):
                raise ValueError('Kernel storage lifecycle is not enabled')
            if stor.group(1) == 'nfsrdma' and stor.group(2) != '1':
                raise ValueError('NFSoRDMA is a single-server path; use --nodes 1')
        elif request.get('inst') not in self.config['profiles']:
            raise ValueError('Unknown institution profile')
        if request.get('action') == 'validate':
            return
        seconds = request.get('seconds')
        if type(seconds) is not int or not 1 <= seconds <= 28800:
            raise ValueError('Duration must be 1..28800 seconds')
        argv = request.get('argv')
        if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or '\0' in a for a in argv):
            raise ValueError('Invalid command')
        cwd = request.get('cwd')
        if not isinstance(cwd, str) or not os.path.isabs(cwd) or ':' in cwd or not Path(cwd).is_dir():
            raise ValueError('Invalid working directory')
        # Do not let users mount arbitrary host trees into a privileged client container.
        allowed = [Path(p).resolve() for p in self.config['workspace_roots']]
        actual = Path(cwd).resolve()
        if not any(root == actual or root in actual.parents for root in allowed):
            raise ValueError('Working directory must be inside an approved workspace')
        env = request.get('env', {})
        if not isinstance(env, dict) or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k)
                or not isinstance(v, str) or '\0' in v for k, v in env.items()):
            raise ValueError('Invalid environment')
        pwd.getpwuid(uid)

    def run(self, uid, request, emit, disconnected):
        self.validate(uid, request)
        guard = self.acquire()
        lease = None
        proc = None
        cleaned = False
        try:
            if self.blocked.exists() or self.active.exists():
                raise RuntimeError('Queue held: DAOS recovery required')
            self.cancelled.clear()
            stor = STOR_RE.fullmatch(request['inst'])
            lease = {'id': uuid.uuid4().hex, 'uid': uid, 'inst': request['inst'],
                     'cwd': request['cwd'], 'phase': 'starting', 'started': time.time(),
                     'profile': dict(kind=stor.group(1), nodes=int(stor.group(2))) if stor
                     else dict(self.config['profiles'][request['inst']])}
            backend = self.backend_for(lease)
            atomic_json(self.active, lease)
            deadline = time.monotonic() + request['seconds']
            watchdog_stop = threading.Event()

            def watch():
                while not watchdog_stop.wait(0.1):
                    if disconnected() or time.monotonic() >= deadline:
                        self.cancelled.set()

            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            code = 1
            try:
                emit('gpuq {} lease {} ({}) starting\n'.format('storage' if stor else 'DAOS',
                                                               lease['id'], lease['inst']))
                backend.start(lease, self.cancelled, emit)
                if self.cancelled.is_set():
                    raise InterruptedError('Cancelled or time limit reached during startup')
                lease['phase'] = 'running'
                atomic_json(self.active, lease)
                user = pwd.getpwuid(uid)
                env = dict(request.get('env', {}), HOME=user.pw_dir, USER=user.pw_name,
                           LOGNAME=user.pw_name, CUDA_VISIBLE_DEVICES='0',
                           GPUQ_LEASE_ID=lease['id'], GPUQ_INST=lease['inst'])
                env.pop('DAOS_POOL', None)
                env.pop('DAOS_CONTAINER', None)
                env.pop('DAOS_POSIX_CONTAINER', None)
                if stor:
                    env.update(backend.payload_env(lease))
                else:
                    env['DAOS_SYSTEM'] = self.config['profiles'][lease['inst']]['system']
                    # Host client matches the baseline version; daos-exec uses the profile image.
                    env['DAOS_AGENT_DRPC_DIR'] = str(self.root / 'clients' / lease['id'] / 'run')
                    env['DAOS_CONTROL_CONFIG'] = str(self.root / 'clients' / lease['id'] / 'daos_control.yml')
                proc = backend.payload(lease, request, env)
                os.set_blocking(proc.stdout.fileno(), False)
                while True:
                    if self.cancelled.is_set():
                        code = 124 if time.monotonic() >= deadline else 130
                        break
                    if select.select([proc.stdout], [], [], 0.1)[0]:
                        data = os.read(proc.stdout.fileno(), 65536)
                        if data:
                            emit(data.decode('utf-8', 'replace'))
                    if proc.poll() is not None:
                        code = proc.returncode if proc.returncode >= 0 else 128 - proc.returncode
                        while True:
                            data = proc.stdout.read(65536)
                            if not data:
                                break
                            emit(data.decode('utf-8', 'replace'))
                        break
            except InterruptedError as error:
                emit(str(error) + '\n')
                code = 124 if time.monotonic() >= deadline else 130
            except Exception as error:
                emit('{} job failed: {}\n'.format('Storage' if stor else 'DAOS', error))
                code = 1
            finally:
                watchdog_stop.set()
                watcher.join()
                lease['phase'] = 'cleaning'
                atomic_json(self.active, lease)
                try:
                    # A startup preflight failure has no node ownership. The node helper's stop/wipe
                    # actions are no-ops without this exact lease; foreign leases always fail closed.
                    backend.stop_payload(lease)
                    if proc is not None:
                        proc.wait(timeout=30)
                        proc.stdout.close()
                    backend.cleanup(lease, emit)
                    cleaned = True
                except Exception as error:
                    atomic_json(self.blocked, {'reason': str(error), 'lease': lease['id']})
                    emit('QUEUE HELD: cleanup failed: {}\n'.format(error))
                    code = 75
                atomic_json(self.root / (lease['id'] + '.json'), dict(lease, exit_code=code,
                                                                  cleanup_complete=cleaned))
                if cleaned:
                    self.active.unlink()
            return code
        finally:
            self.release(guard)

    def recover(self, uid, emit):
        if uid != 0:
            raise PermissionError('Recovery requires root')
        guard = self.acquire()
        try:
            if self.active.exists():
                lease = json.loads(self.active.read_text())
                if lease.get('activation') and lease['phase'] != 'cleaning':
                    raise RuntimeError('Activation incomplete: claim all four nodes before recovery')
                self.backend_for(lease).stop_payload(lease)
                self.backend_for(lease).cleanup(lease, emit)
                atomic_json(self.root / (lease['id'] + '.json'), dict(lease, cleanup_complete=True,
                                                                    recovered=True, recovered_at=time.time()))
                self.active.unlink()
            self.blocked.unlink(missing_ok=True)
        finally:
            self.release(guard)

    def client_exec(self, uid, request, emit, disconnected=lambda: False):
        if not self.active.exists():
            raise RuntimeError('No active DAOS lease')
        lease = json.loads(self.active.read_text())
        if lease['uid'] != uid or lease['id'] != request.get('lease') or lease['phase'] != 'running':
            raise PermissionError('Command must belong to the active institution job')
        if STOR_RE.fullmatch(lease['inst']):
            raise ValueError('daos-exec is not available in a kernel storage job')
        argv = request.get('argv')
        if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or '\0' in v for v in argv):
            raise ValueError('Invalid client command')
        user = pwd.getpwuid(uid)
        argv = ['/usr/bin/podman', 'exec', '--user', '{}:{}'.format(uid, user.pw_gid),
                          '--workdir', lease['cwd'], '--env', 'DAOS_AGENT_DRPC_DIR=/gpuq/run',
                          '--env', 'DAOS_SYSTEM=' + self.config['profiles'][lease['inst']]['system'],
                          '--env', 'DAOS_CONTROL_CONFIG=/gpuq/daos_control.yml',
                          self.backend.client_name(lease), *argv]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=SAFE_ENV)
        try:
            while proc.poll() is None:
                if disconnected():
                    proc.terminate()
                    return 130
                if select.select([proc.stdout], [], [], .2)[0]:
                    data = os.read(proc.stdout.fileno(), 65536)
                    if data:
                        emit(data.decode('utf-8', 'replace'))
            remainder = proc.stdout.read()
            if remainder:
                emit(remainder.decode('utf-8', 'replace'))
            return proc.returncode
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.stdout.close()


def stor_control(broker, uid, request):
    if request.get('op') not in ('cache-cold', 'cache-warm', 'status'):
        raise ValueError('Unknown storage control')
    if not broker.active.exists():
        raise RuntimeError('No active storage lease')
    lease = json.loads(broker.active.read_text())
    if lease['uid'] != uid or lease['id'] != request.get('lease') or lease['phase'] != 'running':
        raise PermissionError('Storage control must come from the running job')
    if not STOR_RE.fullmatch(lease['inst']):
        raise ValueError('Not a kernel storage job')
    return broker.stor_backend.control(lease, request['op'])


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        uid = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
        disconnected = threading.Event()

        def send(value):
            if disconnected.is_set():
                return
            try:
                self.wfile.write((json.dumps(value) + '\n').encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                disconnected.set()

        def gone():
            if disconnected.is_set():
                return True
            if select.select([self.request], [], [], 0)[0]:
                # The protocol has one request; EOF or subsequent data cancels the lease.
                disconnected.set()
            return disconnected.is_set()

        emit = lambda value: send({'log': value})
        try:
            line = self.rfile.readline(1024 * 1024 + 1)
            if len(line) > 1024 * 1024:
                raise ValueError('Request too large')
            request = json.loads(line)
            action = request.get('action')
            broker = self.server.broker
            if action == 'status':
                send({'status': broker.status(), 'exit_code': 0})
            elif action == 'validate':
                broker.validate(uid, request)
                send({'exit_code': 0})
            elif action == 'run':
                send({'exit_code': broker.run(uid, request, emit, gone)})
            elif action == 'recover':
                broker.recover(uid, emit)
                send({'exit_code': 0})
            elif action == 'exec':
                send({'exit_code': broker.client_exec(uid, request, emit, gone)})
            elif action == 'stor-ctl':
                send({'log': json.dumps(stor_control(broker, uid, request)) + '\n', 'exit_code': 0})
            else:
                raise ValueError('Unknown action')
        except Exception as error:
            send({'error': str(error), 'exit_code': 75})


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = False
    block_on_close = True


def rpc(request, path=SOCKET, output=True):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(path)
        conn.sendall((json.dumps(request) + '\n').encode())
        with conn.makefile('r') as reader:
            for line in reader:
                item = json.loads(line)
                if output and 'log' in item:
                    print(item['log'], end='', flush=True)
                if 'error' in item:
                    print('gpuq: ' + item['error'], file=sys.stderr)
                if 'status' in item:
                    if output:
                        print(json.dumps(item['status'], ensure_ascii=False, indent=2))
                    return item['status']
                if 'exit_code' in item:
                    return item['exit_code']
    raise RuntimeError('Lifecycle broker disconnected; queue recovery may be required')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=CONFIG)
    parser.add_argument('--socket', default=SOCKET)
    subs = parser.add_subparsers(dest='action', required=True)
    subs.add_parser('serve')
    subs.add_parser('status')
    subs.add_parser('recover')
    e = subs.add_parser('exec')
    e.add_argument('command', nargs=argparse.REMAINDER)
    v = subs.add_parser('validate')
    v.add_argument('inst')
    r = subs.add_parser('run')
    r.add_argument('inst')
    c = subs.add_parser('stor-ctl')
    c.add_argument('op', choices=['cache-cold', 'cache-warm', 'status'])
    r.add_argument('seconds', type=int)
    r.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == 'serve':
        if os.getuid() != 0:
            parser.error('Broker must run as root')
        config = json.loads(Path(args.config).read_text())
        broker = Broker(config)
        path = Path(args.socket)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        with Server(str(path), Handler) as server:
            server.broker = broker
            os.chmod(path, 0o660)
            os.chown(path, 0, grp.getgrnam('gpuqueue').gr_gid)

            def stop(*_):
                broker.cancelled.set()
                threading.Thread(target=server.shutdown, daemon=True).start()

            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            server.serve_forever(poll_interval=0.2)
        path.unlink(missing_ok=True)
    else:
        request = {'action': args.action}
        if args.action == 'exec':
            argv = args.command[1:] if args.command[:1] == ['--'] else args.command
            request.update(lease=os.environ.get('GPUQ_LEASE_ID'), argv=argv)
        if args.action == 'stor-ctl':
            request.update(lease=os.environ.get('GPUQ_LEASE_ID'), op=args.op)
        if args.action in ('run', 'validate'):
            request['inst'] = args.inst
        if args.action == 'run':
            argv = args.command
            if argv[:1] == ['--']:
                argv = argv[1:]
            # systemd environment names exclude bash's exported BASH_FUNC_name%% entries.
            env = {k: v for k, v in os.environ.items() if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k)}
            request.update(seconds=args.seconds, argv=argv, cwd=os.getcwd(), env=env)
            # SIGTERM closes this client. The root broker notices EOF and owns cleanup to completion.
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))
        try:
            result = rpc(request, args.socket)
        except (OSError, RuntimeError, ValueError) as error:
            print('gpuq: ' + str(error), file=sys.stderr)
            sys.exit(75)
        if isinstance(result, int):
            sys.exit(result)


if __name__ == '__main__':
    main()
