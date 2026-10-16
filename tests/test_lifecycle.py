import importlib.util
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gpuqueue_daos as d
import gpuqueue_node as node


class FixtureBackend:
    """Four directory-backed targets; actual child processes, no physical devices."""
    def __init__(self, root):
        self.root = root
        self.events = []
        self.proc = None
        self.fail_cleanup = False
        self.fail_start = False
        self.slow_start = False

    def start(self, lease, cancel, emit):
        for i in range(4):
            path = self.root / ('node' + str(i))
            path.mkdir(exist_ok=True)
            (path / 'data').write_bytes(b'prior experiment')
            (path / 'metadata').write_text(lease['id'])
            self.events.append('start' + str(i))
            if self.fail_start and i == 1:
                raise RuntimeError('Partial node startup failed')
        if self.slow_start:
            if cancel.wait(4):
                raise InterruptedError('Startup cancelled')

    def payload(self, lease, request, env):
        self.events.append('payload')
        self.proc = subprocess.Popen(request['argv'], cwd=request['cwd'], env=env,
                                     start_new_session=True, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
        return self.proc

    def stop_payload(self, lease):
        self.events.append('stop-payload')
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait(timeout=5)

    def cleanup(self, lease, emit):
        if self.fail_cleanup:
            raise RuntimeError('node3 unreachable')
        for i in range(4):
            self.events.append('stop' + str(i))
        for i in range(4):
            path = self.root / ('node' + str(i))
            for name in ('data', 'metadata'):
                (path / name).unlink(missing_ok=True)
            self.events.append('wipe' + str(i))


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = dict(enabled=True, state_dir=str(self.root / 'state'),
                           workspace_roots=[str(self.root)], profiles={'inst1': {'system': 'one'},
                           'inst2': {'system': 'two'}})
        self.backend = FixtureBackend(self.root)
        self.broker = d.Broker(self.config, self.backend)
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, code='print("experiment ok")', inst='inst1', seconds=10):
        return dict(action='run', inst=inst, seconds=seconds, cwd=str(self.root),
                    argv=[sys.executable, '-c', code], env=dict(PATH=os.environ['PATH']))

    def run_job(self, request=None, gone=lambda: False):
        return self.broker.run(os.getuid(), request or self.request(), self.logs.append, gone)

    def clean(self):
        self.assertFalse(self.broker.active.exists())
        self.assertFalse(self.broker.blocked.exists())
        for i in range(4):
            self.assertFalse((self.root / ('node' + str(i)) / 'data').exists())
            self.assertFalse((self.root / ('node' + str(i)) / 'metadata').exists())

    def test_normal_output_exit_and_all_four_reset(self):
        self.assertEqual(self.run_job(), 0)
        self.assertIn('experiment ok', ''.join(self.logs))
        self.assertLess(self.backend.events.index('stop3'), self.backend.events.index('wipe0'))
        self.clean()

    def test_maintenance_rejects_user_before_lease_or_workload(self):
        marker = self.root / 'maintenance.json'
        marker.write_text(json.dumps(dict(reason='approved migration')))
        with patch.object(d, 'MAINTENANCE', marker):
            with self.assertRaisesRegex(ValueError, 'Maintenance locked'):
                self.broker.run(1004, self.request(), self.logs.append, lambda: False)
            self.assertEqual(self.broker.status()['maintenance']['reason'], 'approved migration')
        self.assertEqual(self.backend.events, [])
        self.clean()

    def test_payload_failure_preserves_exit_and_resets(self):
        self.assertEqual(self.run_job(self.request('raise SystemExit(23)')), 23)
        self.clean()

    def test_timeout_kills_process_and_resets(self):
        self.assertEqual(self.run_job(self.request('import time; time.sleep(60)', seconds=1)), 124)
        self.assertIsNotNone(self.backend.proc.poll())
        self.clean()

    def test_disconnect_cancels_and_resets(self):
        result = []
        gone = threading.Event()
        thread = threading.Thread(target=lambda: result.append(self.run_job(
            self.request('import time; time.sleep(60)'), gone.is_set)))
        thread.start()
        while self.backend.proc is None:
            time.sleep(.01)
        gone.set()
        thread.join(5)
        self.assertEqual(result, [130])
        self.clean()

    def test_startup_cancel_never_runs_payload(self):
        self.backend.slow_start = True
        self.assertEqual(self.run_job(self.request(seconds=1)), 124)
        self.assertNotIn('payload', self.backend.events)
        self.clean()

    def test_partial_start_failure_resets_started_nodes(self):
        self.backend.fail_start = True
        # Fixture cleanup tolerates nodes that never started, like the real node helper.
        for i in range(4):
            (self.root / ('node' + str(i))).mkdir()
        self.assertEqual(self.run_job(), 1)
        self.clean()

    def test_cleanup_failure_holds_next_job_and_root_recovery(self):
        self.backend.fail_cleanup = True
        self.assertEqual(self.run_job(), 75)
        self.assertTrue(self.broker.status()['blocked'])
        with self.assertRaisesRegex(RuntimeError, 'recovery required'):
            self.run_job(self.request(inst='inst2'))
        with self.assertRaises(PermissionError):
            self.broker.recover(12345, self.logs.append)
        self.backend.fail_cleanup = False
        self.broker.recover(0, self.logs.append)
        self.clean()
        self.assertEqual(self.run_job(self.request(inst='inst2')), 0)

    def test_daemon_restart_holds_interrupted_lease(self):
        self.backend.fail_cleanup = True
        self.run_job()
        restarted = d.Broker(self.config, self.backend)
        self.assertIn('Interrupted', restarted.status()['blocked']['reason'])
        self.backend.fail_cleanup = False
        restarted.recover(0, self.logs.append)
        self.clean()

    def test_sequential_institution_handoff(self):
        self.assertEqual(self.run_job(), 0)
        self.assertEqual(self.run_job(self.request(inst='inst2')), 0)
        self.clean()
        histories = [json.loads(p.read_text()) for p in (self.root / 'state').glob('*.json')]
        self.assertEqual({v['inst'] for v in histories}, {'inst1', 'inst2'})
        self.assertTrue(all(v['cleanup_complete'] for v in histories))

    def test_concurrent_lease_rejected(self):
        self.broker.lock.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, 'Another lifecycle'):
                self.run_job()
        finally:
            self.broker.lock.release()

    def test_cross_process_migration_lock_prevents_lease_start(self):
        with (self.broker.root/'lease.lock').open('a') as migration:
            fcntl.flock(migration.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError,'migration owns'):
                self.run_job()
        self.assertFalse(self.broker.active.exists())
        self.assertEqual(self.backend.events,[])
        self.assertEqual(self.run_job(),0)

    def test_disabled_unknown_and_invalid_requests_never_start(self):
        self.broker.config['enabled'] = False
        with self.assertRaisesRegex(ValueError, 'not enabled'):
            self.run_job()
        self.broker.config['enabled'] = True
        for changes in [dict(inst='wrong'), dict(seconds=0), dict(seconds=28801),
                        dict(seconds=True), dict(cwd='/etc'), dict(argv=[]),
                        dict(env={'LD_X\0': 'x'})]:
            req = self.request()
            req.update(changes)
            with self.assertRaises(ValueError):
                self.run_job(req)
        self.assertEqual(self.backend.events, [])

    def test_client_exec_rejects_foreign_owner(self):
        d.atomic_json(self.broker.active, dict(id='a'*32, uid=1234, inst='inst1', phase='running'))
        with self.assertRaises(PermissionError):
            self.broker.client_exec(5678, dict(lease='a'*32, argv=['true']), self.logs.append)

    def test_incomplete_migration_cannot_bypass_four_node_claim(self):
        d.atomic_json(self.broker.active,dict(id='a'*32,inst='inst1',phase='activating',activation=True))
        with self.assertRaisesRegex(RuntimeError,'claim all four nodes'):
            self.broker.recover(0,self.logs.append)
        self.assertEqual(self.backend.events,[])

    def test_socket_peer_credentials_and_disconnect_cleanup(self):
        path = str(self.root / 'broker.sock')
        with d.Server(path, d.Handler) as server:
            server.broker = self.broker
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.connect(path)
                request = self.request('import time; time.sleep(60)')
                request['uid'] = 12345  # Must be ignored; identity comes from SO_PEERCRED.
                conn.sendall((json.dumps(request)+'\n').encode())
                deadline = time.time()+5
                while self.backend.proc is None and time.time()<deadline:
                    time.sleep(.01)
                self.assertEqual(self.broker.status()['active']['uid'], os.getuid())
                conn.close()
                while self.broker.active.exists() and time.time()<deadline:
                    time.sleep(.01)
                self.clean()
            finally:
                server.shutdown()
                thread.join()


class ProductionBackendTest(unittest.TestCase):
    def test_preflight_rejects_mixed_code_versions_before_start(self):
        profile=dict(system='test',port=10401,client_image='sha256:'+'a'*64)
        backend=d.Backend(dict(hosts=['da1','da2','da3','da4'],profiles={'inst1':profile}))
        def reply(host, action, lease):
            return json.dumps(dict(image='sha256:'+('b' if host=='da3' else 'a')*64,
                                   system='test',port=10401))
        with patch.object(backend,'node',reply):
            with self.assertRaisesRegex(RuntimeError,'da3.*does not match'):
                backend.all_nodes('preflight',dict(inst='inst1'))

    def test_client_filters_exported_module_functions_preserves_venv(self):
        with patch.dict(os.environ, {'BASH_FUNC_module%%': '() { :; }', 'VIRTUAL_ENV': '/tmp/venv'}), \
             patch.object(sys, 'argv', ['broker', 'run', 'inst1', '60', '--', 'true']), \
             patch.object(d.signal, 'signal'), \
             patch.object(d, 'rpc', return_value=0) as rpc:
            with self.assertRaises(SystemExit) as stopped:
                d.main()
        self.assertEqual(stopped.exception.code, 0)
        env = rpc.call_args.args[0]['env']
        self.assertNotIn('BASH_FUNC_module%%', env)
        self.assertEqual(env['VIRTUAL_ENV'], '/tmp/venv')

    def test_stop_failure_never_wipes_any_node(self):
        backend = d.Backend({'hosts': ['da1','da2','da3','da4']})
        lease = {'id': 'a'*32, 'inst': 'inst1'}
        actions = []
        def all_nodes(action, value):
            actions.append(action)
            if action == 'stop':
                raise RuntimeError('da4 did not stop')
        with patch.object(backend, 'all_nodes', all_nodes), patch.object(d, 'command') as cmd:
            cmd.return_value.returncode = 1
            with self.assertRaisesRegex(RuntimeError, 'da4'):
                backend.cleanup(lease, lambda _: None)
        self.assertEqual(actions, ['stop'])

    def test_partial_fanout_collects_all_errors(self):
        backend = d.Backend({'hosts': ['da1','da2','da3','da4']})
        seen = []
        def call(host, action, lease):
            seen.append(host)
            if host in ('da2', 'da4'):
                raise RuntimeError('unreachable')
        with patch.object(backend, 'node', call):
            with self.assertRaisesRegex(RuntimeError, 'da2.*da4'):
                backend.all_nodes('stop', {})
        self.assertEqual(set(seen), {'da1','da2','da3','da4'})


class NodeGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / 'config.json'
        self.cfg = dict(enabled=True, profiles={'inst1': {'image': 'sha256:'+'a'*64,
                        'system': 'one', 'port': 10401}}, host='da1', serial='SERIAL', pci='0000:01:00.0')
        self.config_path.write_text(json.dumps(self.cfg))
        self.config_path.chmod(0o600)
        self.lease = 'a'*32
        self.patches = [patch.object(node,'CONFIG',self.config_path), patch.object(node,'ROOT',self.root),
                        patch.object(node.os,'getuid',return_value=0), patch.object(node,'identity',return_value=Path('/fake'))]
        # stat ownership check is exercised with actual files when tests run as root; emulate it otherwise.
        if os.getuid() != 0:
            actual_stat = Path.stat
            def config_stat(path, *args, **kwargs):
                s = actual_stat(path, *args, **kwargs)
                if path == self.config_path:
                    class Meta:
                        st_uid=0
                        st_mode=s.st_mode
                    return Meta()
                return s
            self.patches.append(patch.object(Path,'stat',config_stat))
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def call(self, action):
        with patch.object(sys,'argv',['node',action,self.lease,'inst1']):
            node.main()

    def owner(self, **changes):
        value = dict(lease=self.lease,inst='inst1',phase='stopped',serial='SERIAL',pci='0000:01:00.0')
        value.update(changes)
        (self.root/'owner.json').write_text(json.dumps(value))

    def test_nvme_probe_waits_for_live_controller_and_nonempty_serial(self):
        pci = self.root / 'pci'
        ctrl = pci / 'nvme' / 'nvme0'
        ctrl.mkdir(parents=True)
        (self.root / 'nvme').mkdir()
        (pci / 'driver').symlink_to(self.root / 'nvme')
        (ctrl / 'state').write_text('new')
        (ctrl / 'serial').write_text('')
        (ctrl / 'nvme0n1').touch()
        devroot = self.root / 'dev'
        devroot.mkdir()
        (devroot / 'nvme0n1').touch()
        def probing(_):
            if (ctrl / 'state').read_text() == 'new':
                (ctrl / 'state').write_text('live')
            else:
                (ctrl / 'serial').write_text('SERIAL')
        with patch.object(node, 'run'), patch.object(node, 'validate_unused') as unused, \
             patch.object(node, 'Path', side_effect=lambda p: devroot if p == '/dev' else Path(p)), \
             patch.object(node.time, 'sleep', side_effect=probing) as sleep:
            self.assertEqual(node.kernel_device(self.cfg, pci), devroot / 'nvme0n1')
            self.assertEqual(sleep.call_count, 2)
            unused.assert_called_once_with(devroot / 'nvme0n1')
        (ctrl / 'serial').write_text('WRONG')
        with patch.object(node, 'run'), patch.object(node, 'validate_unused') as unused:
            with self.assertRaisesRegex(RuntimeError, 'serial changed'):
                node.kernel_device(self.cfg, pci)
            unused.assert_not_called()

    def test_no_owner_wipe_does_not_touch_disk(self):
        with patch.object(node,'kernel_device') as dev, patch.object(node,'run') as cmd:
            self.call('wipe')
            dev.assert_not_called()
            cmd.assert_not_called()

    def test_foreign_lease_and_changed_serial_rejected(self):
        for changes in [dict(lease='b'*32),dict(serial='WRONG')]:
            self.owner(**changes)
            with patch.object(node,'kernel_device') as dev:
                with self.assertRaises(RuntimeError):
                    self.call('wipe')
                dev.assert_not_called()

    def test_running_engine_blocks_before_disk_access(self):
        self.owner()
        with patch.object(node,'ensure_stopped',side_effect=RuntimeError('engine alive')), \
             patch.object(node,'kernel_device') as dev:
            with self.assertRaisesRegex(RuntimeError,'engine alive'):
                self.call('wipe')
            dev.assert_not_called()

    def test_discard_failure_keeps_owner_and_metadata_for_recovery(self):
        self.owner()
        base = self.root/'jobs'/self.lease
        (base/'control').mkdir(parents=True)
        (base/'control'/'superblock').write_text('old')
        with patch.object(node,'ensure_stopped'), patch.object(node,'kernel_device',return_value=Path('/fake/nvme')), \
             patch.object(node,'run',side_effect=RuntimeError('discard unsupported')):
            with self.assertRaisesRegex(RuntimeError,'discard unsupported'):
                self.call('wipe')
        self.assertTrue((base/'control'/'superblock').exists())
        self.assertEqual(json.loads((self.root/'owner.json').read_text())['phase'],'wiping')

    def test_successful_reset_removes_metadata_and_owner_keeps_audit(self):
        self.owner()
        base = self.root/'jobs'/self.lease
        (base/'control').mkdir(parents=True)
        (base/'control'/'superblock').write_text('old')
        result = subprocess.CompletedProcess([],0,stdout='')
        with patch.object(node,'ensure_stopped'), patch.object(node,'kernel_device',return_value=Path('/fake/nvme')), \
             patch.object(node,'run',return_value=result) as cmd:
            self.call('wipe')
        self.assertEqual(cmd.call_args_list[0].args[0],['blkdiscard','/fake/nvme'])
        self.assertFalse((base/'control').exists())
        self.assertFalse((self.root/'owner.json').exists())
        self.assertTrue((base/'cleanup.json').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
