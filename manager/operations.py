#!/usr/bin/env python3
"""Operator-only consistent runtime snapshots, pinned upgrades and restoration.

Never accepts Docker options, volume paths, shell commands or image tags from
HTTP. Run with the documented systemd resource limits and preserve backup media.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from worker import Worker, ManagerError, PROJECT, atomic, run

SERVICE = 'flybot-platform-manager.service'
CONFIG = Path('/srv/flybot-platform-botawaken/manager/config.json')


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        while chunk := f.read(4*1024*1024): digest.update(chunk)
    return digest.hexdigest()


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


@contextmanager
def paused():
    if subprocess.run(['systemctl','--quiet','is-active','docker.service']).returncode:
        raise ManagerError('docker_service_not_active')
    run(['systemctl', 'stop', SERVICE], timeout=60)
    try:
        with open('/run/flybot-platform-botawaken-manager.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield Worker(CONFIG)
    finally:
        # Never start the shared Docker daemon if an operator stopped it during
        # maintenance. Resume only our manager while Docker is already active.
        if subprocess.run(['systemctl','--quiet','is-active','docker.service']).returncode == 0:
            run(['systemctl', 'start', SERVICE])


def existing(worker, ident):
    if not (worker.state_dir/(ident+'.json')).is_file():
        raise ManagerError('instance_not_recorded')
    return worker.record(ident)


def halt(worker, ident, remove=False):
    worker.withdraw(ident)
    name = PROJECT+'-u-'+ident
    row = worker.inspect('container', name)
    if row:
        worker.assert_owned(row, ident)
        if row['State']['Running']: run(['docker', 'stop', name], timeout=45)
        if remove: run(['docker', 'rm', name])
    return row


def backup_volume(worker):
    name = PROJECT+'-backups'
    row = worker.inspect('volume', name)
    if not row:
        run(['docker', 'volume', 'create', *worker.labels(), name])
        row = worker.inspect('volume', name)
    worker.assert_owned(row)
    path = Path(row['Mountpoint']); os.chmod(path, 0o700)
    return path


def snapshot(worker, ident):
    record = existing(worker, ident)
    row = halt(worker, ident)
    mount = worker.filesystem(ident, worker.resources(record)['storage_gib'])
    run(['umount', str(mount)])
    disk = mount.parent/'disk.img'
    destination = backup_volume(worker)/ident/stamp()
    destination.mkdir(mode=0o700, parents=True)
    if shutil.disk_usage(destination).free < 64*1024**3:
        raise ManagerError('backup_space_reserve_low')
    temporary = destination/'disk.img.partial'; final = destination/'disk.img'
    run(['cp', '--sparse=always', '--reflink=auto', str(disk), str(temporary)], timeout=1800)
    os.chmod(temporary, 0o600)
    with temporary.open('rb') as f: os.fsync(f.fileno())
    os.replace(temporary, final)
    manifest = {'format': 1, 'instance_id': ident, 'record': record,
                'image': row['Image'] if row else record['image'], 'version': record['version'],
                'size': final.stat().st_size, 'sha256': sha(final), 'created_at': stamp()}
    atomic(destination/'manifest.json', json.dumps(manifest, indent=2))
    return destination


def restore(worker, ident, folder):
    # Only backups in the owned backup volume and for this exact instance qualify.
    root = backup_volume(worker).resolve()/ident
    folder = folder.resolve()
    if folder.parent != root or not re.fullmatch(r'\d{8}T\d{12}Z', folder.name):
        raise ManagerError('invalid_backup_path')
    manifest_path = folder/'manifest.json'; disk = folder/'disk.img'
    if manifest_path.is_symlink() or disk.is_symlink(): raise ManagerError('invalid_backup_symlink')
    manifest = json.loads(manifest_path.read_text())
    stored_size = worker.resources(manifest['record'])['storage_gib'] * 1024**3
    if manifest['format'] != 1 or manifest['instance_id'] != ident or manifest['record']['id'] != ident or manifest['size'] != stored_size or disk.stat().st_size != manifest['size'] or sha(disk) != manifest['sha256']:
        raise ManagerError('backup_integrity_failed')
    image = worker.inspect('image', manifest['image'])
    if not image or image['Architecture'] != 'arm64': raise ManagerError('rollback_image_missing')
    existing(worker, ident); halt(worker, ident, remove=True)
    mount = worker.storage_root/ident/'fs'
    if os.path.ismount(mount):
        run(['umount', str(mount)])
    target = mount.parent/'disk.img'; temp = mount.parent/'disk.img.restore'
    run(['cp', '--sparse=always', '--reflink=auto', str(disk), str(temp)], timeout=1800)
    os.chmod(temp, 0o600)
    with temp.open('rb') as f: os.fsync(f.fileno())
    os.replace(temp, target)
    record = manifest['record']; record['image'] = manifest['image']; record['version'] = manifest['version']
    atomic(worker.state_dir/(ident+'.json'), json.dumps(record, indent=2))
    worker.filesystem(ident, worker.resources(record)['storage_gib'])


def wait_ready(worker, ident, image_id, timeout=180):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        row = worker.inspect('container', PROJECT+'-u-'+ident)
        if row and row['Image'] == image_id and row['State']['Running'] and row['State'].get('Health', {}).get('Status') == 'healthy' and (worker.route_dir/('tenant-'+ident+'.toml')).is_file():
            return
        time.sleep(3)
    raise ManagerError('runtime_readiness_timeout')


def upgrade(ident, image_id, version, readiness_timeout=180, include_inactive=False):
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id) or not re.fullmatch(r'[A-Za-z0-9._+-]{1,64}', version):
        raise ManagerError('image_digest_and_release_required')
    backup = None
    changed = False
    running = True
    try:
        with paused() as worker:
            image = worker.inspect('image', image_id)
            if not image or image['Architecture'] != 'arm64': raise ManagerError('arm64_image_required')
            instance = next((r for r in worker.bridge('snapshot')['instances'] if uuid.UUID(r['id']).hex == ident), None)
            if not instance or (instance['desired'] != 'running' and not include_inactive):
                raise ManagerError('running_active_instance_required')
            running = instance['desired'] == 'running'
            record = existing(worker, ident)
            backup = snapshot(worker, ident)
            changed = True
            halt(worker, ident, remove=True)
            record['image'] = image_id; record['version'] = version
            atomic(worker.state_dir/(ident+'.json'), json.dumps(record, indent=2))
        if running: wait_ready(worker, ident, image_id, readiness_timeout)
        print('upgrade_ready instance='+ident+' backup='+str(backup))
    except Exception:
        if not backup or not changed: raise
        with paused() as worker: restore(worker, ident, backup)
        previous = json.loads((backup/'manifest.json').read_text())
        if running: wait_ready(worker, ident, previous['image'], 180)
        raise ManagerError('upgrade_failed_rollback_ready')


def resize(ident, cpus, memory_gib, pids_limit, storage_gib):
    backup = None
    try:
        with paused() as worker:
            record = existing(worker, ident)
            previous = worker.resources(record)
            desired = {'cpus': cpus, 'memory_gib': memory_gib, 'pids_limit': pids_limit, 'storage_gib': storage_gib}
            resources = worker.resources(record | {'resources': desired})
            if resources['storage_gib'] < previous['storage_gib']:
                raise ManagerError('storage_shrink_not_supported')
            instance = next((row for row in worker.bridge('snapshot')['instances'] if uuid.UUID(row['id']).hex == ident), None)
            if not instance:
                raise ManagerError('instance_not_found')
            was_running = instance['desired'] == 'running'
            if resources['storage_gib'] > previous['storage_gib']:
                backup = snapshot(worker, ident)
                folder = worker.storage_root/ident; disk = folder/'disk.img'; mount = folder/'fs'
                if os.path.ismount(mount):
                    run(['umount', str(mount)])
                check = subprocess.run(['e2fsck', '-f', '-p', str(disk)], capture_output=True, text=True, timeout=300)
                if check.returncode not in (0, 1):
                    raise ManagerError('filesystem_check_failed')
                with disk.open('r+b') as stream:
                    stream.truncate(resources['storage_gib'] * 1024**3); stream.flush(); os.fsync(stream.fileno())
                run(['resize2fs', str(disk)], timeout=300)
            record['resources'] = resources
            atomic(worker.state_dir/(ident+'.json'), json.dumps(record, indent=2))
            worker.filesystem(ident, resources['storage_gib'])
            name = PROJECT+'-u-'+ident
            row = worker.inspect('container', name)
            if row:
                worker.assert_owned(row, ident)
                worker.apply_resources(name, row, resources)
        if was_running:
            wait_ready(worker, ident, record['image'])
    except Exception:
        if backup:
            with paused() as worker:
                restore(worker, ident, backup)
            if was_running:
                previous_manifest = json.loads((backup/'manifest.json').read_text())
                wait_ready(worker, ident, previous_manifest['image'])
        raise
    print('resize_complete instance='+ident+' cpus='+str(cpus)+' memory_gib='+str(memory_gib)+' pids_limit='+str(pids_limit)+' storage_gib='+str(storage_gib)+' backup='+str(backup))


def shared_backup():
    worker = Worker(CONFIG); target = backup_volume(worker)/'shared'/stamp(); target.mkdir(mode=0o700, parents=True)
    db = PROJECT+'-db-1'; social = PROJECT+'-bot-social-1'
    worker.assert_owned(worker.inspect('container', db)); row = worker.assert_owned(worker.inspect('container', social))
    with (target/'control-plane.sql').open('xb') as stream:
        subprocess.run(['docker', 'exec', db, 'pg_dump', '-U', 'flybot_control', '-d', 'flybot_control', '--no-owner'], stdout=stream, stderr=subprocess.PIPE, check=True, timeout=90)
    social_data = next(m for m in row['Mounts'] if m['Destination'] == '/data')
    worker.assert_owned(worker.inspect('volume', social_data['Name']))
    was_running = row['State']['Running']
    try:
        if was_running: run(['docker', 'stop', social], timeout=45)
        run(['tar', '--sparse', '-cf', str(target/'social-data.tar'), '-C', social_data['Source'], '.'], timeout=300)
    finally:
        if was_running: run(['docker', 'start', social])
    manifest = {'format': 1, 'files': {p.name: sha(p) for p in target.iterdir() if p.is_file()}, 'social_image': row['Image'], 'control_plane_image': worker.inspect('container', worker.cp)['Image']}
    atomic(target/'manifest.json', json.dumps(manifest, indent=2))
    print('shared_backup_complete path='+str(target))


def main():
    parser = argparse.ArgumentParser(); sub = parser.add_subparsers(dest='action', required=True)
    backup = sub.add_parser('backup'); backup.add_argument('instance')
    rollback = sub.add_parser('restore'); rollback.add_argument('instance'); rollback.add_argument('backup', type=Path)
    change = sub.add_parser('upgrade'); change.add_argument('instance'); change.add_argument('--image', required=True); change.add_argument('--version', required=True)
    change.add_argument('--readiness-timeout', type=int, default=180, choices=range(30,601), metavar='30..600')
    change.add_argument('--include-inactive', action='store_true', help='Upgrade stopped records without starting their containers')
    resize_parser = sub.add_parser('resize'); resize_parser.add_argument('instance')
    resize_parser.add_argument('--cpus', type=float, required=True)
    resize_parser.add_argument('--memory-gib', type=int, required=True)
    resize_parser.add_argument('--pids-limit', type=int, required=True)
    resize_parser.add_argument('--storage-gib', type=int, required=True)
    sub.add_parser('backup-shared')
    args = parser.parse_args()
    if os.geteuid() != 0: raise ManagerError('root_required')
    os.umask(0o077); os.nice(10)
    # A separate operations lock serializes concurrent backups/upgrades as well as
    # the worker lock taken while a runtime is stopped or its storage is copied.
    with open('/run/flybot-platform-botawaken-operations.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'backup-shared': shared_backup(); return
        ident = uuid.UUID(args.instance).hex
        if args.action == 'upgrade': upgrade(ident, args.image, args.version, args.readiness_timeout, args.include_inactive); return
        if args.action == 'resize': resize(ident, args.cpus, args.memory_gib, args.pids_limit, args.storage_gib); return
        with paused() as worker:
            if args.action == 'backup': print('backup_complete path='+str(snapshot(worker, ident)))
            else: restore(worker, ident, args.backup)
        if args.action == 'restore':
            instance = next((r for r in worker.bridge('snapshot')['instances'] if uuid.UUID(r['id']).hex == ident), None)
            if instance and instance['desired'] == 'running': wait_ready(worker, ident, existing(worker,ident)['image'])
            print('restored instance='+ident)


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print(str(exc) if isinstance(exc, ManagerError) else 'operation_failed; inspect owned resources before retrying', file=sys.stderr)
        raise SystemExit(1)
