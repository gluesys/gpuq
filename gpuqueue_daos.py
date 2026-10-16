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

    def all_nodes(self, action, lease):
        errors = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            calls = {pool.submit(self.node, h, action, lease): h for h in self.config['hosts']}
            for future, host in calls.items():
                try:
                    reply = future.result()
                    if action == 'preflight':
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


class Broker:
    def __init__(self, config, backend=None):
        self.config = config
        self.backend = backend or Backend(config)
        self.root = Path(config['state_dir'])
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.active = self.root / 'active.json'
        self.blocked = self.root / 'blocked.json'
        if self.active.exists():
            atomic_json(self.blocked, {'reason': 'Interrupted lease; root recovery required'})

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
        if request.get('inst') not in self.config['profiles']:
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
            lease = {'id': uuid.uuid4().hex, 'uid': uid, 'inst': request['inst'],
                     'cwd': request['cwd'], 'phase': 'starting', 'started': time.time(),
                     'profile': dict(self.config['profiles'][request['inst']])}
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
                emit('gpuq DAOS lease {} ({}) starting\n'.format(lease['id'], lease['inst']))
                self.backend.start(lease, self.cancelled, emit)
                if self.cancelled.is_set():
                    raise InterruptedError('Cancelled or time limit reached during startup')
                lease['phase'] = 'running'
                atomic_json(self.active, lease)
                user = pwd.getpwuid(uid)
                env = dict(request.get('env', {}), HOME=user.pw_dir, USER=user.pw_name,
                           LOGNAME=user.pw_name, CUDA_VISIBLE_DEVICES='0',
                           GPUQ_LEASE_ID=lease['id'], GPUQ_INST=lease['inst'],
                           DAOS_SYSTEM=self.config['profiles'][lease['inst']]['system'])
                env.pop('DAOS_POOL', None)
                env.pop('DAOS_CONTAINER', None)
                env.pop('DAOS_POSIX_CONTAINER', None)
                # Host client matches the baseline version; daos-exec uses the profile image.
                env['DAOS_AGENT_DRPC_DIR'] = str(self.root / 'clients' / lease['id'] / 'run')
                env['DAOS_CONTROL_CONFIG'] = str(self.root / 'clients' / lease['id'] / 'daos_control.yml')
                proc = self.backend.payload(lease, request, env)
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
                emit('DAOS job failed: {}\n'.format(error))
                code = 1
            finally:
                watchdog_stop.set()
                watcher.join()
                lease['phase'] = 'cleaning'
                atomic_json(self.active, lease)
                try:
                    # A startup preflight failure has no node ownership. The node helper's stop/wipe
                    # actions are no-ops without this exact lease; foreign leases always fail closed.
                    self.backend.stop_payload(lease)
                    if proc is not None:
                        proc.wait(timeout=30)
                        proc.stdout.close()
                    self.backend.cleanup(lease, emit)
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
                self.backend.stop_payload(lease)
                self.backend.cleanup(lease, emit)
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
    v.add_argument('inst', choices=['inst1', 'inst2'])
    r = subs.add_parser('run')
    r.add_argument('inst', choices=['inst1', 'inst2'])
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
