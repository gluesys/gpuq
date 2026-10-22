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
STOR_RE = re.compile(r'(lustre|nfsrdma|nvmeof)([14])')
ZFS_ARC = Path('/sys/module/zfs/parameters/zfs_arc_max')
NVMET = Path('/sys/kernel/config/nvmet')


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


def kernel_device(config, pci, validate=True):
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
                if validate:
                    validate_unused(dev)
                return dev
        if len(matches) > 1:
            raise RuntimeError('Multiple namespaces; refusing automatic wipe')
        time.sleep(0.1)
    raise RuntimeError('Approved NVMe namespace did not appear')


def discard(dev):
    # blkdiscard invalidates the whole namespace, not a secure erase. No unsafe fallback.
    run(['blkdiscard', str(dev)], timeout=180)
    run(['wipefs', '--all', str(dev)])
    signatures = run(['wipefs', '--noheadings', str(dev)]).stdout.strip()
    if signatures:
        raise RuntimeError('Unexpected filesystem signatures after reset')


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
        sys.exit('Root only: gpuqueue-daos-node preflight|start|stop|wipe|claim-reset|cache-cold|cache-warm '
                 'LEASE inst1|inst2|lustre1|lustre4|nfsrdma1|nvmeof1|nvmeof4')
    action, lease, inst = sys.argv[1:]
    if action not in ('preflight', 'start', 'stop', 'wipe', 'claim-reset', 'cache-cold', 'cache-warm') \
            or not re.fullmatch('[0-9a-f]{32}', lease):
        sys.exit('Invalid action or lease')
    meta = CONFIG.stat()
    if meta.st_uid != 0 or meta.st_mode & 0o022:
        sys.exit('Node config must be root-owned and not group/world writable')
    cfg = json.loads(CONFIG.read_text())
    if not cfg.get('enabled'):
        sys.exit('Node lifecycle disabled; preserving existing data')
    if STOR_RE.fullmatch(inst):
        if test_mode:
            sys.exit('Kernel storage lifecycle has no isolated test mode')
        return stor_main(action, lease, inst, cfg)
    if inst not in cfg['profiles']:
        sys.exit('Unknown profile')
    if action.startswith('cache-'):
        sys.exit('Cache control applies to kernel storage leases only')
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
            if not file_mode:
                # A kernel storage lease that failed mid-cleanup must never be overlaid by SPDK.
                problems = residue(cfg, pci)
                if problems:
                    raise RuntimeError('Kernel storage residue: ' + '; '.join(problems))
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
            if file_mode:
                image_file = base / 'data.img'
                if image_file.exists():
                    image_file.unlink()
            else:
                discard(dev)
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


# ---------------------------------------------------------------------------
# Kernel storage leases (lustre1|lustre4|nfsrdma1|nvmeof1|nvmeof4).
# Same owner.json, node.lock and disk allowlist as DAOS leases, so the two kinds exclude each other.
# Every teardown step checks real kernel state, so stop/wipe can be repeated after any interruption.

def stor_names(lease, index):
    return dict(pool='gq' + lease[:8], fsname='g' + lease[:7],
                nqn='nqn.2026-10.com.gluesys:gpuq:{}:{}'.format(lease, index))


def zpools():
    p = run(['zpool', 'list', '-H', '-o', 'name'], check=False)
    return p.stdout.split() if p.returncode == 0 else []


def lustre_mounts():
    found = []
    for line in Path('/proc/mounts').read_text().splitlines():
        fields = line.split()
        if len(fields) > 2 and fields[2] == 'lustre':
            found.append(fields[1])
    return found


def nvmet_subsystems():
    root = NVMET / 'subsystems'
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


def gpuq_exports():
    p = run(['exportfs', '-v'], check=False)
    return [line.split()[0] for line in p.stdout.splitlines()
            if line.startswith(str(ROOT))] if p.returncode == 0 else []


def residue(cfg, pci):
    """Kernel state a gpuq storage lease may leave behind. Empty list means clean."""
    problems = ['zpool ' + n for n in zpools() if n.startswith('gq')]
    problems += ['lustre mount ' + m for m in lustre_mounts() if m.startswith(str(ROOT))]
    problems += ['nvmet subsystem ' + n for n in nvmet_subsystems() if ':gpuq:' in n]
    problems += ['nfs export ' + e for e in gpuq_exports()]
    if (ROOT / 'lnet.gpuq').exists():
        problems.append('gpuq-configured LNet')
    default = cfg.get('stor', {}).get('arc_max_default')
    if default is not None and ZFS_ARC.exists() and int(ZFS_ARC.read_text()) != int(default):
        problems.append('zfs_arc_max {} != {}'.format(ZFS_ARC.read_text().strip(), default))
    for ctrl in (pci / 'nvme').glob('nvme*') if pci is not None and (pci / 'nvme').is_dir() else []:
        for ns in ctrl.glob(ctrl.name + 'n[0-9]*'):
            if not re.fullmatch(r'nvme[0-9]+n[0-9]+', ns.name):
                continue
            block = Path('/sys/class/block') / ns.name
            if list(block.glob(ns.name + 'p*')):
                problems.append(ns.name + ' has partitions')
            if run(['wipefs', '--noheadings', '/dev/' + ns.name], check=False).stdout.strip():
                problems.append(ns.name + ' has signatures')
    return problems


def set_arc(value):
    if ZFS_ARC.exists():
        ZFS_ARC.write_text(str(int(value)))


def lnet_up(scfg):
    nid = scfg['fabric_ip'] + '@o2ib'
    run(['modprobe', 'lnet'])
    marker = ROOT / 'lnet.gpuq'
    # Written before configuring: an interrupted start is still undone by stop.
    marker.write_text('o2ib NI for gpuq Lustre lease\n')
    if nid in run(['lctl', 'list_nids'], check=False).stdout.split():
        return
    if run(['lnetctl', 'net', 'show'], check=False).returncode:
        run(['lnetctl', 'lnet', 'configure'])
    run(['lnetctl', 'net', 'add', '--net', 'o2ib0', '--if', scfg['fabric_iface']])


def lnet_down(scfg):
    marker = ROOT / 'lnet.gpuq'
    if not marker.exists():
        return
    nid = scfg['fabric_ip'] + '@o2ib'
    if nid in run(['lctl', 'list_nids'], check=False).stdout.split():
        # ptlrpc pins LNet (net del -> EINVAL, unconfigure -> EBUSY) until the Lustre modules
        # unload. Unload them only when no Lustre device or mount of any kind remains.
        if lustre_mounts() or run(['lctl', 'dl'], check=False).stdout.strip():
            raise RuntimeError('Lustre devices still present; refusing to unload LNet')
        run(['lustre_rmmod'], check=False, timeout=180)
        if nid in run(['lctl', 'list_nids'], check=False).stdout.split():
            raise RuntimeError('LNet o2ib NI still configured after module unload')
    marker.unlink()


def make_pool(names, dev, base, kind):
    run(['zpool', 'create', '-f', '-o', 'ashift=12', '-O', 'compression=off', '-O', 'atime=off',
         '-O', 'recordsize=1M', '-O', 'mountpoint=none', '-O', 'canmount=off', names['pool'], str(dev)],
        timeout=120)
    if kind == 'nfsrdma':
        run(['zfs', 'create', '-o', 'mountpoint=' + str(base / 'nfs'), '-o', 'sync=disabled',
             names['pool'] + '/nfs'])
        os.chmod(str(base / 'nfs'), 0o777)


def destroy_pool(names):
    # nfsd keeps export references for a moment after it stops; flush and retry before failing.
    for attempt in range(10):
        if names['pool'] not in zpools():
            return
        run(['exportfs', '-f'], check=False)
        result = run(['zpool', 'destroy', '-f', names['pool']], check=False, timeout=120)
        if result.returncode == 0:
            return
        time.sleep(3)
    raise RuntimeError('zpool destroy {}: {}'.format(names['pool'], result.stdout[-1000:]))


def lustre_start(scfg, names, index, base):
    if index == 0:
        target = names['pool'] + '/mdt0'
        run(['mkfs.lustre', '--backfstype=zfs', '--fsname=' + names['fsname'], '--mgs', '--mdt',
             '--index=0', target], timeout=120)
        (base / 'mdt0').mkdir(exist_ok=True)
        run(['mount', '-t', 'lustre', target, str(base / 'mdt0')], timeout=120)
    target = '{}/ost{}'.format(names['pool'], index)
    run(['mkfs.lustre', '--backfstype=zfs', '--fsname=' + names['fsname'], '--ost',
         '--index=' + str(index), '--mgsnode=' + scfg['mgs_nid'], target], timeout=120)
    (base / 'ost').mkdir(exist_ok=True)
    run(['mount', '-t', 'lustre', target, str(base / 'ost')], timeout=180)


def lustre_stop(scfg, base):
    for d in ('ost', 'mdt0'):
        path = base / d
        if os.path.ismount(str(path)):
            run(['umount', str(path)], timeout=300)
    lnet_down(scfg)


def nfs_start(scfg, base):
    export = '{}:{}'.format(scfg['client_ip'], base / 'nfs')
    run(['systemctl', 'start', 'nfs-server'])
    portlist = Path('/proc/fs/nfsd/portlist')
    if 'rdma {}'.format(scfg.get('nfs_rdma_port', 20049)) not in portlist.read_text():
        portlist.write_text('rdma {}\n'.format(scfg.get('nfs_rdma_port', 20049)))
    run(['exportfs', '-o', 'rw,no_root_squash,async,no_subtree_check', export])


def nfs_stop(scfg, base):
    if str(base / 'nfs') in gpuq_exports():
        run(['exportfs', '-u', '{}:{}'.format(scfg['client_ip'], base / 'nfs')])
    if run(['systemctl', 'is-active', 'nfs-server'], check=False).stdout.strip() == 'active':
        if [e for e in run(['exportfs', '-v'], check=False).stdout.splitlines() if e.startswith('/')]:
            raise RuntimeError('nfs-server exports foreign paths; refusing to stop it')
        run(['systemctl', 'stop', 'nfs-server'], timeout=120)


def nvmet_start(scfg, names, dev):
    transport = scfg.get('nvmeof_transport', 'rdma')
    run(['modprobe', 'nvmet'])
    run(['modprobe', 'nvmet-' + transport])
    sub = NVMET / 'subsystems' / names['nqn']
    sub.mkdir()
    (sub / 'attr_allow_any_host').write_text('0')
    host = NVMET / 'hosts' / scfg['client_hostnqn']
    if not host.exists():
        host.mkdir()
    (sub / 'allowed_hosts' / scfg['client_hostnqn']).symlink_to(host)
    ns = sub / 'namespaces' / '1'
    ns.mkdir()
    (ns / 'device_path').write_text(str(dev))
    (ns / 'enable').write_text('1')
    port = NVMET / 'ports' / str(scfg.get('nvmet_port_id', 1))
    port.mkdir()
    for attr, value in (('addr_trtype', transport), ('addr_adrfam', 'ipv4'),
                        ('addr_traddr', scfg['fabric_ip']), ('addr_trsvcid', str(scfg.get('nvmeof_port', 4420)))):
        (port / attr).write_text(value)
    (port / 'subsystems' / names['nqn']).symlink_to(sub)


def nvmet_stop(scfg, names):
    port = NVMET / 'ports' / str(scfg.get('nvmet_port_id', 1))
    link = port / 'subsystems' / names['nqn']
    if os.path.islink(str(link)):
        link.unlink()
    sub = NVMET / 'subsystems' / names['nqn']
    if sub.is_dir():
        ns = sub / 'namespaces' / '1'
        if ns.is_dir():
            (ns / 'enable').write_text('0')
            ns.rmdir()
        for allowed in (sub / 'allowed_hosts').iterdir():
            allowed.unlink()
        sub.rmdir()
    if port.is_dir() and not list((port / 'subsystems').iterdir()):
        port.rmdir()
    host = NVMET / 'hosts' / scfg['client_hostnqn']
    if host.is_dir() and not [s for s in nvmet_subsystems()]:
        host.rmdir()
    if NVMET.is_dir() and not nvmet_subsystems() and not list((NVMET / 'ports').iterdir()):
        run(['modprobe', '-r', 'nvmet-' + scfg.get('nvmeof_transport', 'rdma'), 'nvmet'], check=False)


def clear_partitions(dev):
    """ZFS whole-disk vdevs leave a GPT and labels inside p1; DAOS must see a bare namespace."""
    for part in sorted((Path('/sys/class/block') / dev.name).glob(dev.name + 'p*')):
        run(['zpool', 'labelclear', '-f', '/dev/' + part.name], check=False)
    run(['wipefs', '--all', '--quiet', str(dev)])
    run(['blockdev', '--rereadpt', str(dev)], check=False)
    run(['udevadm', 'settle'], check=False, timeout=60)


def stor_main(action, lease, inst, cfg):
    kind, nodes = STOR_RE.fullmatch(inst).groups()
    nodes = int(nodes)
    scfg = cfg.get('stor', {})
    if not scfg.get('enabled'):
        sys.exit('Kernel storage lifecycle disabled on this node')
    if kind == 'nfsrdma' and nodes != 1:
        sys.exit('NFSoRDMA is a single-server path')
    index = int(scfg['index'])
    if index >= nodes:
        print(json.dumps(dict(host=cfg['host'], action=action, lease=lease, success=True, participant=False)))
        return
    names = stor_names(lease, index)
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = ROOT / 'jobs' / lease
    with (ROOT / 'node.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        owner_path = ROOT / 'owner.json'
        owner = json.loads(owner_path.read_text()) if owner_path.exists() else None
        if owner and (owner['lease'] != lease or owner['inst'] != inst):
            raise RuntimeError('Foreign lease owns this node; recovery required')
        if owner and (owner['serial'] != cfg['serial'] or owner['pci'] != cfg['pci']):
            raise RuntimeError('Disk allowlist changed during lease; refusing cleanup')

        def record(phase, **extra):
            state = dict(owner or {})
            state.update(extra, lease=lease, inst=inst, phase=phase, serial=cfg['serial'], pci=cfg['pci'])
            save(owner_path, state)
            return state

        name = 'gpuq-engine-' + lease
        pci = identity(cfg)
        if action == 'preflight':
            ensure_stopped(cfg, name)
            if owner:
                raise RuntimeError('Existing lease metadata; recovery required')
            problems = residue(cfg, pci)
            if problems:
                raise RuntimeError('Kernel storage residue: ' + '; '.join(problems))
            if kind == 'nfsrdma':
                if run(['systemctl', 'is-active', 'nfs-ganesha'], check=False).stdout.strip() == 'active':
                    raise RuntimeError('nfs-ganesha holds the NFS port')
                if run(['systemctl', 'is-active', 'nfs-server'], check=False).stdout.strip() == 'active':
                    raise RuntimeError('nfs-server already active outside gpuq')
            if kind == 'nvmeof' and (NVMET / 'ports' / str(scfg.get('nvmet_port_id', 1))).exists():
                raise RuntimeError('nvmet port already configured outside gpuq')
        elif action == 'start':
            ensure_stopped(cfg, name)
            if owner:
                raise RuntimeError('Lease already allocated')
            arc = int(ZFS_ARC.read_text()) if ZFS_ARC.exists() else None
            owner = record('starting', arc_prev=arc)  # Ownership first: an interrupted start is recoverable.
            dev = kernel_device(cfg, pci)
            base.mkdir(parents=True, exist_ok=True)
            if kind in ('lustre', 'nfsrdma'):
                if scfg.get('arc_max_job'):
                    set_arc(scfg['arc_max_job'])
                make_pool(names, dev, base, kind)
            if kind == 'lustre':
                lnet_up(scfg)
                lustre_start(scfg, names, index, base)
            elif kind == 'nfsrdma':
                nfs_start(scfg, base)
            else:
                nvmet_start(scfg, names, dev)
            record('running')
        elif action == 'stop':
            if not owner:
                print('No resources for this lease')
                return
            if kind == 'lustre':
                lustre_stop(scfg, base)
            elif kind == 'nfsrdma':
                nfs_stop(scfg, base)
            else:
                nvmet_stop(scfg, names)
            destroy_pool(names)
            if owner.get('arc_prev') is not None:
                set_arc(scfg.get('arc_max_default') or owner['arc_prev'])
            record('stopped')
        elif action == 'wipe':
            if not owner:
                print('No resources for this lease')
                return
            if owner['phase'] not in ('stopped', 'wiping'):
                raise RuntimeError('Stop must complete before wipe')
            ensure_stopped(cfg, name)
            if names['pool'] in zpools() or nvmet_subsystems().count(names['nqn']):
                raise RuntimeError('Storage still exported; stop must complete before wipe')
            record('wiping')
            clear_partitions(kernel_device(cfg, pci, validate=False))
            dev = kernel_device(cfg, pci)
            discard(dev)
            problems = residue(cfg, pci)
            if problems:
                raise RuntimeError('Residue after reset: ' + '; '.join(problems))
            for d in ('mdt0', 'ost', 'nfs'):
                path = base / d
                if path.is_dir() and not os.path.ismount(str(path)):
                    path.rmdir()
            base.mkdir(parents=True, exist_ok=True)
            save(base / 'cleanup.json', dict(lease=lease, inst=inst, serial=cfg['serial'], pci=cfg['pci'],
                                             reset='labelclear+blkdiscard', completed=time.time()))
            owner_path.unlink()
        elif action in ('cache-cold', 'cache-warm'):
            if not owner or owner['phase'] != 'running':
                raise RuntimeError('No running storage lease')
            if kind in ('lustre', 'nfsrdma'):
                cold = action == 'cache-cold'
                run(['zfs', 'set', 'primarycache=' + ('metadata' if cold else 'all'), names['pool']])
                if cold and ZFS_ARC.exists():
                    # primarycache only stops new data caching. Shrinking the ARC evicts what is
                    # already cached; ZFS ignores a ceiling below zfs_arc_min, so lower that first.
                    arc_min = ZFS_ARC.with_name('zfs_arc_min')
                    job, floor = ZFS_ARC.read_text().strip(), arc_min.read_text().strip()
                    try:
                        arc_min.write_text(str(32 << 20))
                        set_arc(64 << 20)
                        time.sleep(3)
                    finally:
                        set_arc(job)
                        arc_min.write_text(floor)
        else:
            raise RuntimeError('Claim-reset is DAOS-only')
        print(json.dumps(dict(host=cfg['host'], action=action, lease=lease, success=True,
                              participant=True, kind=kind, nodes=nodes, nqn=names['nqn'],
                              fsname=names['fsname'], pool=names['pool'])))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        sys.exit(str(error))
