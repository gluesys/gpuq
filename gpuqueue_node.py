#!/usr/bin/python3
"""Root-only node helper. Disk identity and paths come ONLY from root-owned config."""
import fcntl
import glob
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time

CONFIG = Path('/etc/gpuqueue-daos/node.json')
ROOT = Path('/var/lib/gpuqueue-daos')


def run(argv, check=True, timeout=90):
    p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, timeout=timeout,
                       env=dict(PATH='/usr/sbin:/usr/bin:/sbin:/bin', HOME='/root', LANG='C'))
    if check and p.returncode:
        raise RuntimeError('{}: {}'.format(' '.join(argv[:3]), p.stdout[-3000:]))
    return p


def save(path, data):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        os.chmod(str(tmp), 0o600)
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(path))
    fd = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def identity(config):
    if run(['hostname', '-s']).stdout.strip() != config['host']:
        raise RuntimeError('Hostname does not match disk allowlist')
    pci = Path('/sys/bus/pci/devices') / config['pci']
    if (pci / 'vendor').read_text().strip() != config['vendor'] or (pci / 'device').read_text().strip() != config['device']:
        raise RuntimeError('PCI identity changed; refusing device access')
    return pci


def ensure_stopped(config, name):
    if run(['podman', 'container', 'exists', name], check=False).returncode == 0:
        raise RuntimeError('Managed container still exists')
    if config.get('storage_mode') == 'file':
        return  # Isolated test-only container; physical DAOS engines are never managed.
    if run(['pgrep', '-x', 'daos_engine'], check=False).returncode == 0:
        raise RuntimeError('DAOS engine still running on host')
    for unit in config['conflicting_units']:
        if run(['systemctl', 'is-active', unit], check=False).returncode == 0:
            raise RuntimeError('Conflicting host service active: ' + unit)


def kernel_device(config, pci):
    run(['modprobe', 'nvme'])
    driver = (pci / 'driver').resolve().name if (pci / 'driver').exists() else ''
    if driver != 'nvme':
        if driver not in ('', 'uio_pci_generic', 'vfio-pci'):
            raise RuntimeError('Unexpected NVMe driver: ' + driver)
        # Refuse to unbind a device still owned by any SPDK/UIO/VFIO process.
        char_devices = [Path('/dev') / p.name for p in (pci / 'uio').glob('uio*')]
        if (pci / 'iommu_group').exists():
            char_devices.append(Path('/dev/vfio') / (pci / 'iommu_group').resolve().name)
        rdevs = {p.stat().st_rdev for p in char_devices if p.exists()}
        for filename in glob.glob('/proc/[0-9]*/fd/*'):
            try:
                s = os.stat(filename)
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if stat.S_ISCHR(s.st_mode) and s.st_rdev in rdevs:
                raise RuntimeError('PCI device still open by process: ' + filename)
        (pci / 'driver_override').write_text('nvme')
        try:
            if driver:
                (pci / 'driver' / 'unbind').write_text(config['pci'])
            Path('/sys/bus/pci/drivers_probe').write_text(config['pci'])
        finally:
            (pci / 'driver_override').write_text('\n')
    for _ in range(50):
        controllers = list((pci / 'nvme').glob('nvme*'))
        matches = []
        for ctrl in controllers:
            # sysfs appears before the asynchronous NVMe identify probe completes.
            # Wait for a live controller; an empty serial is not an identity match.
            state = ctrl / 'state'
            if state.exists() and state.read_text().strip() != 'live':
                continue
            serial = (ctrl / 'serial').read_text().strip()
            if not serial:
                continue
            if serial != config['serial']:
                raise RuntimeError('NVMe serial changed ({}); refusing wipe'.format(serial))
            matches.extend(ctrl.glob(ctrl.name + 'n[0-9]*'))
        matches = [p for p in matches if re.fullmatch(r'nvme[0-9]+n[0-9]+', p.name)]
        if len(matches) == 1:
            dev = Path('/dev') / matches[0].name
            if dev.exists():
                validate_unused(dev)
                return dev
        if len(matches) > 1:
            raise RuntimeError('Multiple namespaces; refusing automatic wipe')
        time.sleep(0.1)
    raise RuntimeError('Approved NVMe namespace did not appear')


def validate_unused(dev):
    if not stat.S_ISBLK(dev.stat().st_mode):
        raise RuntimeError('Wipe target is not a block device')
    sysdev = Path('/sys/class/block') / dev.name
    if list(sysdev.glob(dev.name + 'p*')) or list((sysdev / 'holders').iterdir()):
        raise RuntimeError('NVMe has partitions or holders')
    major_minor = (sysdev / 'dev').read_text().strip()
    # Check all process mount namespaces, not only this SSH process's namespace.
    for filename in glob.glob('/proc/[0-9]*/mountinfo'):
        try:
            lines = Path(filename).read_text().splitlines()
        except (FileNotFoundError, ProcessLookupError):
            continue
        for line in lines:
            if line.split()[2] == major_minor:
                raise RuntimeError('NVMe mounted in process namespace: ' + filename)
    rdev = dev.stat().st_rdev
    for filename in glob.glob('/proc/[0-9]*/fd/*'):
        try:
            s = os.stat(filename)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if stat.S_ISBLK(s.st_mode) and s.st_rdev == rdev:
            raise RuntimeError('NVMe open by another process: ' + filename)


def main():
    global CONFIG, ROOT
    test_mode = sys.argv[1:2] == ['--test']
    if test_mode:
        CONFIG = Path('/etc/gpuqueue-daos/test-node.json')
        ROOT = Path('/var/lib/gpuqueue-daos-test')
        sys.argv.pop(1)
    if os.getuid() != 0 or len(sys.argv) != 4:
        sys.exit('Root only: gpuqueue-daos-node preflight|start|stop|wipe|claim-reset LEASE inst1|inst2')
    action, lease, inst = sys.argv[1:]
    if action not in ('preflight', 'start', 'stop', 'wipe', 'claim-reset') or not re.fullmatch('[0-9a-f]{32}', lease):
        sys.exit('Invalid action or lease')
    meta = CONFIG.stat()
    if meta.st_uid != 0 or meta.st_mode & 0o022:
        sys.exit('Node config must be root-owned and not group/world writable')
    cfg = json.loads(CONFIG.read_text())
    if not cfg.get('enabled'):
        sys.exit('Node lifecycle disabled; preserving existing data')
    if inst not in cfg['profiles']:
        sys.exit('Unknown profile')
    file_mode = cfg.get('storage_mode') == 'file'
    if file_mode and not test_mode:
        sys.exit('File-backed storage only permitted in isolated --test mode')
    if test_mode and not file_mode:
        sys.exit('Test mode must never access physical NVMe')
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (ROOT / 'node.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        owner_path = ROOT / 'owner.json'
        owner = json.loads(owner_path.read_text()) if owner_path.exists() else None
        if owner and (owner['lease'] != lease or owner['inst'] != inst):
            raise RuntimeError('Foreign lease owns this node; recovery required')
        if owner and (owner['serial'] != cfg['serial'] or owner['pci'] != cfg['pci']):
            raise RuntimeError('Disk allowlist changed during lease; refusing cleanup')
        def record(phase):
            save(owner_path, dict(lease=lease, inst=inst, phase=phase,
                                  serial=cfg['serial'], pci=cfg['pci']))
        name = ('gpuqt-engine-' if file_mode else 'gpuq-engine-') + lease
        base = ROOT / 'jobs' / lease
        p = cfg['profiles'][inst]
        pci = None if file_mode else identity(cfg)
        if action == 'claim-reset':
            if file_mode:
                raise RuntimeError('Bootstrap reset is physical-mode only')
            ensure_stopped(cfg, name)
            kernel_device(cfg, pci)  # Verify serial again even when resuming an existing claim.
            if owner:
                if owner['phase'] == 'stopped':
                    print('Already claimed for bootstrap reset')
                    return
                raise RuntimeError('Existing lease requires recovery')
            record('stopped')
        elif action == 'preflight':
            ensure_stopped(cfg, name)
            if owner:
                raise RuntimeError('Existing lease metadata; recovery required')
            image = p['image']
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
                raise RuntimeError('Image must be pinned to a local sha256 ID')
            run(['podman', 'image', 'exists', image])
            # Read-only inspection: namespace serial is verified again before any wipe.
            run(['test', '-d', '/dev/hugepages'])
        elif action == 'start':
            ensure_stopped(cfg, name)
            if owner:
                raise RuntimeError('Lease already allocated')
            # Record ownership BEFORE preparation/start; interrupted starts can be recovered.
            record('starting')
            if not file_mode:
                kernel_device(cfg, pci)  # identity and in-use checks only, no deletion
                run(['/usr/bin/daos_server', 'nvme', 'prepare', '--ignore-config', '--disable-vfio',
                     '--hugepages=' + str(p['hugepages']), '--target-user=root', cfg['pci']], timeout=120)
            base.mkdir(parents=True, exist_ok=True)
            for d in ('run', 'scm', 'control', 'logs'):
                (base / d).mkdir(exist_ok=True)
            huge_mount = Path('/dev/hugepages')
            if file_mode:
                # Share the page pool, but never expose existing engines' hugepage files.
                huge_mount = base / 'hugepages'
                huge_mount.mkdir(exist_ok=True)
                run(['mount', '-t', 'hugetlbfs', '-o', 'pagesize=2M,mode=1777', 'none', str(huge_mount)])
                (base / 'empty-pci').mkdir(exist_ok=True)
            common = {'name': p['system'], 'mgmt_svc_replicas': p['access_points'],
                'port': p['port'], 'provider': 'ofi+verbs;ofi_rxm', 'socket_dir': '/gpuq/run',
                'disable_vfio': True, 'nr_hugepages': p['hugepages'], 'system_ram_reserved': 6,
                'control_log_file': '/gpuq/logs/server.log',
                'control_log_mask': 'DEBUG', 'helper_log_file': '/gpuq/logs/helper.log',
                'control_metadata': {'path': '/gpuq/control'},
                'transport_config': {'allow_insecure': True},
                'engines': [{'targets': p['targets'], 'nr_xs_helpers': 0,
                    'pinned_numa_node': 0, 'first_core': p.get('first_core', 0), 'fabric_iface': cfg['fabric_iface'],
                    'fabric_iface_port': p['fabric_port'], 'log_file': '/gpuq/logs/engine.log',
                    'log_mask': 'INFO', 'storage': [
                        {'class': 'ram', 'scm_mount': '/gpuq/scm', 'scm_size': p['scm_gib']},
                        {'class': 'nvme', 'bdev_list': [cfg['pci']], 'bdev_roles': ['wal', 'meta', 'data']}]}]}
            if file_mode:
                common['bdev_exclude'] = [cfg['pci']]
                common['engines'][0]['storage'][1] = {'class': 'file', 'bdev_list': ['/gpuq/data.img'],
                    'bdev_size': 16, 'bdev_roles': ['wal', 'meta', 'data']}
            (base / 'server.yml').write_text(json.dumps(common))
            # Test containers cannot access /dev/uio* or raw NVMe block devices.
            caps = ['--cap-add=SYS_ADMIN', '--cap-add=IPC_LOCK', '--cap-add=SYS_NICE', '--cap-add=SYS_PTRACE',
                    '--device=/dev/infiniband', '--security-opt=label=disable',
                    '--security-opt=seccomp=unconfined'] if file_mode else ['--privileged']
            run(['podman', 'run', '-d', '--pull=never', '--name', name,
                 '--label', 'gpuq.lease=' + lease, '--network=host', '--ipc=host', *caps,
                 '--ulimit', 'memlock=-1:-1', '--ulimit', 'nofile=1048576:1048576',
                 # DAOS startup resets and rebinds PCI. A private /dev snapshot misses
                 # the NVMe/UIO nodes created by host udev during that transition.
                 *([] if file_mode else ['-v', '/dev:/dev:rw']),
                 '-v', str(huge_mount) + ':/dev/hugepages:rw',
                 '-v', '/sys/devices/system/node:/sys/devices/system/node:rw',
                 '-v', '/lib/modules:/lib/modules:ro', '-v', str(base) + ':/gpuq:rw',
                 *(['--mount', 'type=bind,src=' + str(base / 'empty-pci') +
                    ',target=/sys/bus/pci/devices/' + cfg['pci'] + ',readonly=true'] if file_mode else []),
                 '--entrypoint', '/usr/bin/daos_server', p['image'], 'start', '-o', '/gpuq/server.yml'])
            record('running')
        elif action == 'stop':
            if not owner:
                # No ownership means start never happened on this host. Never stop foreign services.
                print('No resources for this lease')
                return
            if run(['podman', 'container', 'exists', name], check=False).returncode == 0:
                info = json.loads(run(['podman', 'inspect', name]).stdout)[0]
                if info['Config']['Labels'].get('gpuq.lease') != lease:
                    raise RuntimeError('Container label mismatch')
                run(['podman', 'stop', '--time', '60', name], timeout=80)
                logs = run(['podman', 'logs', name], check=False).stdout
                base.mkdir(parents=True, exist_ok=True)
                (base / 'container.log').write_text(logs)
                run(['podman', 'rm', name])
            ensure_stopped(cfg, name)
            if file_mode and os.path.ismount(str(base / 'hugepages')):
                run(['umount', str(base / 'hugepages')])
            record('stopped')
        elif action == 'wipe':
            if not owner:
                print('No resources for this lease')
                return
            if owner['phase'] not in ('stopped', 'wiping'):
                raise RuntimeError('Stop must complete before wipe')
            ensure_stopped(cfg, name)
            dev = None if file_mode else kernel_device(cfg, pci)
            record('wiping')
            # blkdiscard invalidates the whole namespace, not a secure erase. No unsafe fallback.
            if file_mode:
                image_file = base / 'data.img'
                if image_file.exists():
                    image_file.unlink()
            else:
                run(['blkdiscard', str(dev)], timeout=180)
                run(['wipefs', '--all', str(dev)])
                signatures = run(['wipefs', '--noheadings', str(dev)]).stdout.strip()
                if signatures:
                    raise RuntimeError('Unexpected filesystem signatures after reset')
            # Old control metadata MUST NOT survive an NVMe reset. Keep logs as an audit record.
            for d in ('control', 'scm', 'run'):
                path = base / d
                if path.exists():
                    if os.path.ismount(str(path)):
                        raise RuntimeError('Metadata path still mounted: ' + str(path))
                    shutil.rmtree(str(path))
            base.mkdir(parents=True, exist_ok=True)
            save(base / 'cleanup.json', dict(lease=lease, serial=cfg['serial'], pci=cfg['pci'],
                                             reset='file-unlink' if file_mode else 'blkdiscard', completed=time.time()))
            owner_path.unlink()
        print(json.dumps(dict(host=cfg['host'], action=action, lease=lease, success=True,
                              image=p['image'], system=p['system'], port=p['port'])))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        sys.exit(str(error))
