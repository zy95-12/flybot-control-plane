#!/usr/bin/env python3
"""Single privileged reconciler. HTTP callers cannot supply Docker options.

Only named, labelled project resources are mutated. Never log subprocess output:
ready.json and the private CLI protocol can contain runtime launch credentials.
"""
from __future__ import annotations
import argparse
import fcntl
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from bridge_client import BridgeClient, BridgeError

PROJECT = 'flybot-platform-botawaken'
PROJECT_LABEL = 'com.docker.compose.project'
INSTANCE_LABEL = 'io.flybot.instance'
MANAGED_LABEL = 'io.flybot.managed'
DENIED = ['0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24', '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4']
# Exact upstreams configured by this host's cloud network. Docker's embedded
# resolver forwards queries to these addresses from the tenant network.
DNS_UPSTREAMS = tuple(x.strip() for x in os.environ.get('FLYBOT_DNS_UPSTREAMS', '1.1.1.1,8.8.8.8').split(',') if x.strip())
DEFAULT_RESOURCES = {'cpus': 2, 'memory_gib': 2, 'pids_limit': 256}


class ManagerError(Exception):
    pass


def run(args, payload=None, timeout=30):
    result = subprocess.run(args, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=timeout)
    if result.returncode:
        raise ManagerError('command_failed')
    return result.stdout


def atomic(path, content, uid=0, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.manager-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(content); f.flush(); os.fsync(f.fileno())
        os.chmod(name, mode); os.chown(name, uid, uid)
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name): os.unlink(name)


class Worker:
    def __init__(self, config):
        self.cfg = json.loads(config.read_text())
        if self.cfg['project'] != PROJECT or not re.fullmatch(r'sha256:[0-9a-f]{64}', self.cfg['runtime_image']):
            raise ManagerError('invalid_config')
        self.root = Path(self.cfg['root'])
        if self.root != Path('/srv/flybot-platform-botawaken'):
            raise ManagerError('invalid_root')
        self.pool = ipaddress.ip_network(self.cfg['subnet_pool'])
        if self.pool.version != 4 or not self.pool.is_private or self.pool.prefixlen != 20:
            raise ManagerError('invalid_pool')
        self.ca = Path(self.cfg['ca'])
        self.domain = self.cfg['domain']
        if self.domain != 'botawaken.com' or not 1 <= self.cfg['max_instances'] <= 16 or self.cfg['storage_gib'] != 8:
            raise ManagerError('invalid_limits')
        self.cp = PROJECT + '-control-plane-1'; self.ingress = PROJECT + '-ingress-1'
        self.bridge_client = BridgeClient(self.cp)
        self.route_dir = self.root/'config/tunnel-dynamic'
        self.state_dir = self.root/'manager-state'; self.state_dir.mkdir(mode=0o700, exist_ok=True)
        self.assert_owned(self.inspect('container', self.cp))
        self.assert_owned(self.inspect('container', self.ingress))
        self.preflight_pool()
        self.storage_root = self.ensure_pool_volume()
        spec = importlib.util.spec_from_file_location('flybot_routes', self.root/'release-20261020/deploy/ingress/generate_routes.py')
        self.routes = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.routes)
        # Public host addresses are denied as well as RFC1918, to prevent reaching
        # host services by routing through the host's external address.
        addresses = json.loads(run(['ip', '-j', '-4', 'addr', 'show']))
        self.host_ips = {entry['local'] for dev in addresses for entry in dev.get('addr_info', []) if ipaddress.ip_address(entry['local']).is_global}
        if os.environ.get('FLYBOT_HOST_PUBLIC_IP'):
            self.host_ips.add(os.environ['FLYBOT_HOST_PUBLIC_IP'])

    def inspect(self, kind, name):
        result = subprocess.run(['docker', kind, 'inspect', name], capture_output=True, text=True, timeout=15)
        if result.returncode:
            # Treat a daemon failure differently from a genuinely absent object.
            run(['docker', 'info', '--format', '{{.ServerVersion}}'])
            if 'no such' not in result.stderr.lower() and 'not found' not in result.stderr.lower():
                raise ManagerError('docker_inspection_failed')
            return None
        return json.loads(result.stdout)[0]

    def assert_owned(self, row, ident=None):
        if row is None: raise ManagerError('resource_missing')
        labels = row.get('Config', {}).get('Labels', {}) if 'Config' in row else row.get('Labels', {})
        if labels.get(PROJECT_LABEL) != PROJECT:
            raise ManagerError('resource_ownership_conflict')
        if ident and (labels.get(INSTANCE_LABEL) != ident or labels.get(MANAGED_LABEL) != 'true'):
            raise ManagerError('instance_ownership_conflict')
        return row

    def labels(self, ident=None):
        pairs = {PROJECT_LABEL: PROJECT, MANAGED_LABEL: 'true'}
        if ident: pairs[INSTANCE_LABEL] = ident
        return [item for k, v in pairs.items() for item in ['--label', k+'='+v]]

    def bridge(self, operation, **fields):
        # This fixed exec target is verified every time before crossing the trust boundary.
        self.assert_owned(self.inspect('container', self.cp))
        try:
            return self.bridge_client.request({'operation': operation, **fields})
        except BridgeError:
            raise ManagerError('private_bridge_unavailable') from None

    def preflight_pool(self):
        networks = run(['docker', 'network', 'ls', '-q']).split()
        for name in networks:
            row = self.inspect('network', name)
            for entry in row.get('IPAM', {}).get('Config') or []:
                if entry.get('Subnet'):
                    subnet = ipaddress.ip_network(entry['Subnet'])
                    if subnet.version == 4 and subnet.overlaps(self.pool):
                        if row.get('Labels', {}).get(PROJECT_LABEL) != PROJECT or row.get('Labels', {}).get(MANAGED_LABEL) != 'true':
                            raise ManagerError('subnet_collision')
        for entry in json.loads(run(['ip', '-j', '-4', 'route', 'show', 'table', 'all'])):
            if entry.get('dst', 'default') == 'default': continue
            net = ipaddress.ip_network(entry['dst'], strict=False)
            if net.overlaps(self.pool) and not entry.get('dev', '').startswith('fb-'):
                raise ManagerError('host_route_collision')

    def ensure_pool_volume(self):
        name = PROJECT+'-runtime-storage'
        row = self.inspect('volume', name)
        if row is None:
            run(['docker', 'volume', 'create', *self.labels(), name])
            row = self.inspect('volume', name)
        self.assert_owned(row)
        path = Path(row['Mountpoint'])
        # This is a Docker-created, task-owned volume, on the large data filesystem.
        # Admission checks live in filesystem() when allocating a new disk.
        # Low reserve must not prevent existing runtimes or TLS renewal.
        return path

    def identity(self, instance):
        ident = uuid.UUID(instance['id']).hex
        if instance['id'] != str(uuid.UUID(instance['id'])):
            raise ManagerError('invalid_instance_id')
        return ident

    def record(self, ident):
        path = self.state_dir/(ident+'.json')
        if path.exists():
            row = json.loads(path.read_text())
            if row['id'] != ident or row['subnet'] not in [str(n) for n in self.pool.subnets(new_prefix=29)]:
                raise ManagerError('invalid_record')
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', row.get('image', self.cfg['runtime_image'])):
                raise ManagerError('invalid_record_image')
            row.setdefault('image', self.cfg['runtime_image'])
            row.setdefault('version', self.cfg['version'])
            return row
        records = [json.loads(p.read_text()) for p in self.state_dir.glob('*.json')]
        if len(records) >= self.cfg['max_instances']:
            raise ManagerError('instance_capacity_reached')
        used = {r['subnet'] for r in records}
        subnet = next(n for n in self.pool.subnets(new_prefix=29) if str(n) not in used)
        row = {'id': ident, 'subnet': str(subnet), 'bridge': 'fb-'+ident[:10], 'chain': 'FB_'+ident[:16].upper(), 'image': self.cfg['runtime_image'], 'version': self.cfg['version']}
        if any(r['bridge'] == row['bridge'] or r['chain'] == row['chain'] for r in records):
            raise ManagerError('identity_collision')
        atomic(path, json.dumps(row))
        return row

    def resources(self, record):
        configured = record.get('resources', {})
        if not isinstance(configured, dict) or set(configured) - (set(DEFAULT_RESOURCES) | {'storage_gib'}):
            raise ManagerError('invalid_record_resources')
        values = DEFAULT_RESOURCES | {'storage_gib': self.cfg['storage_gib']} | configured
        cpus = values['cpus']; memory_gib = values['memory_gib']; pids_limit = values['pids_limit']; storage_gib = values['storage_gib']
        if (isinstance(cpus, bool) or not isinstance(cpus, (int, float)) or not 0.5 <= cpus <= 16 or
                isinstance(memory_gib, bool) or type(memory_gib) is not int or not 1 <= memory_gib <= 64 or
                isinstance(pids_limit, bool) or type(pids_limit) is not int or not 64 <= pids_limit <= 1024 or
                isinstance(storage_gib, bool) or type(storage_gib) is not int or not self.cfg['storage_gib'] <= storage_gib <= 1024):
            raise ManagerError('invalid_record_resources')
        return {'cpus': cpus, 'memory_gib': memory_gib, 'pids_limit': pids_limit, 'storage_gib': storage_gib}

    def apply_resources(self, name, row, resources):
        expected_memory = resources['memory_gib'] * 1024**3
        host = row['HostConfig']
        if (host.get('NanoCpus') == int(resources['cpus'] * 1_000_000_000) and
                host.get('Memory') == expected_memory and host.get('MemorySwap') == expected_memory and
                host.get('PidsLimit') == resources['pids_limit']):
            return row
        run(['docker', 'update', '--cpus', str(resources['cpus']), '--memory', str(resources['memory_gib'])+'g',
             '--memory-swap', str(resources['memory_gib'])+'g', '--pids-limit', str(resources['pids_limit']), name])
        return self.inspect('container', name)

    def network(self, record):
        ident = record['id']; name = PROJECT+'-u-'+ident
        row = self.inspect('network', name)
        if row is None:
            run(['docker', 'network', 'create', *self.labels(ident), '--subnet', record['subnet'],
                 '--opt', 'com.docker.network.bridge.name='+record['bridge'], name])
            row = self.inspect('network', name)
        self.assert_owned(row, ident)
        if row['Options'].get('com.docker.network.bridge.name') != record['bridge'] or row['IPAM']['Config'][0]['Subnet'] != record['subnet'] or row.get('EnableIPv6'):
            raise ManagerError('network_config_conflict')
        net = ipaddress.ip_network(record['subnet'])
        self.firewall(record, str(net[3]))
        # A running owned ingress may gain a new private interface; no restart.
        self.assert_owned(self.inspect('container', self.ingress))
        if self.ingress not in [r['Name'] for r in row.get('Containers', {}).values()]:
            run(['docker', 'network', 'connect', '--ip', str(net[3]), '--alias', 'social.'+self.domain, name, self.ingress])
        return name, str(net[2])

    def firewall(self, record, ingress_ip):
        chain = record['chain']; bridge = record['bridge']
        def ipt(*args): return ['iptables', '-w', '5', *args]
        # Never flush an existing chain. On restart compare its complete ordered
        # rule specification before allowing a container to start.
        rules = [['-d', ingress_ip+'/32', '-m', 'conntrack', '--ctstate', 'RELATED,ESTABLISHED', '-j', 'RETURN'],
                 ['-d', ingress_ip+'/32', '-p', 'tcp', '--dport', '443', '-j', 'RETURN']]
        for target in sorted(self.host_ips): rules.append(['-d', target+'/32', '-j', 'DROP'])
        dns_rules = [['-d', target+'/32', '-p', protocol, '--dport', '53', '-j', 'RETURN']
                     for target in DNS_UPSTREAMS for protocol in ('udp', 'tcp')]
        rules.extend(dns_rules)
        for target in DENIED: rules.append(['-d', target, '-j', 'DROP'])
        rules.append(['-j', 'RETURN'])
        exists = subprocess.run(ipt('-S', chain), capture_output=True, text=True, timeout=10)
        if exists.returncode:
            run(ipt('-N', chain))
            # Install deny by default while assembling, even if interrupted.
            run(ipt('-A', chain, '-j', 'DROP'))
            for rule in rules: run(ipt('-I', chain, str(len(run(ipt('-S', chain)).splitlines())-1), *rule))
            run(ipt('-D', chain, '-j', 'DROP'))
        expected = ['-N '+chain] + ['-A '+chain+' '+' '.join(r) for r in rules]
        # Permit only the exact previous owned ruleset (or an interrupted DNS
        # insertion) to migrate. Unknown rules still fail closed; never flush.
        def normalized(lines):
            return [line.replace(' -m tcp', '').replace(' -m udp', '').replace('RELATED,ESTABLISHED', 'ESTABLISHED,RELATED') for line in lines]
        actual = normalized(run(ipt('-S', chain)).strip().splitlines())
        expected = normalized(expected)
        dns_lines = normalized(['-A '+chain+' '+' '.join(rule) for rule in dns_rules])
        present = set(actual) & set(dns_lines)
        if actual != expected:
            if actual != [line for line in expected if line not in dns_lines or line in present]:
                raise ManagerError('firewall_config_conflict')
            for position, rule in enumerate(rules, 1):
                line = normalized(['-A '+chain+' '+' '.join(rule)])[0]
                if line in dns_lines and line not in present:
                    run(ipt('-I', chain, str(position), *rule))
            actual = normalized(run(ipt('-S', chain)).strip().splitlines())
        if actual != expected:
            raise ManagerError('firewall_config_conflict')
        for parent, rule in [('DOCKER-USER', ['-i', bridge, '-s', str(ipaddress.ip_network(record['subnet'])[2])+'/32', '-j', chain]), ('INPUT', ['-i', bridge, '-j', 'DROP'])]:
            if subprocess.run(ipt('-C', parent, *rule), capture_output=True, timeout=10).returncode:
                run(ipt('-I', parent, '1', *rule))

    def filesystem(self, ident, storage_gib=None):
        storage_gib = self.cfg['storage_gib'] if storage_gib is None else storage_gib
        if type(storage_gib) is not int or not self.cfg['storage_gib'] <= storage_gib <= 1024:
            raise ManagerError('invalid_record_resources')
        folder = self.storage_root/ident; folder.mkdir(mode=0o700, exist_ok=True)
        disk = folder/'disk.img'; mount = folder/'fs'; mount.mkdir(mode=0o700, exist_ok=True)
        if not disk.exists():
            if shutil.disk_usage(folder).free < 64 * 1024**3:
                raise ManagerError('storage_reserve_low')
            with disk.open('xb') as f:
                os.chmod(disk, 0o600); f.truncate(storage_gib * 1024**3)
            # A durable marker distinguishes an incomplete format from user data.
            atomic(folder/'format-pending', 'new\n')
        if (folder/'format-pending').exists():
            if os.path.ismount(mount): raise ManagerError('storage_format_conflict')
            run(['mkfs.ext4', '-q', '-F', '-m', '0', str(disk)], timeout=60)
            (folder/'format-pending').unlink()
        if disk.stat().st_size != storage_gib*1024**3:
            raise ManagerError('storage_size_conflict')
        if not os.path.ismount(mount):
            run(['mount', '-o', 'loop,nosuid,nodev', str(disk), str(mount)], timeout=30)
        # Verify the backing file; never chmod/create data in a foreign mount.
        source = run(['findmnt', '-n', '-o', 'SOURCE', '--mountpoint', str(mount)]).strip()
        backing = run(['losetup', '-n', '-O', 'BACK-FILE', source]).strip()
        if Path(backing).resolve() != disk.resolve():
            raise ManagerError('storage_mount_conflict')
        for name in ['data', 'workspace']:
            p = mount/name
            if p.is_symlink(): raise ManagerError('storage_symlink_conflict')
            p.mkdir(mode=0o700, exist_ok=True)
            os.chown(p, 10001, 10001)
        return mount

    def certificate(self, ident):
        cert = self.root/'certificates'/('runtime-'+ident)
        if not cert.exists():
            run(['python3', str(self.root/'release-20261020/deploy/runtime/certificates.py'), 'issue', '--ca', str(self.ca), '--hostname', 'u-'+ident+'.'+self.domain, '--output', str(cert)])
            os.chown(cert/'key.pem', 10001, 10001)
        # Fail closed before expiry, renewal is a separately versioned operation.
        result = subprocess.run(['openssl', 'x509', '-checkend', '86400', '-noout', '-in', str(cert/'cert.pem')], capture_output=True, timeout=10)
        if result.returncode: raise ManagerError('certificate_renewal_required')
        return cert

    def container(self, instance, record, network, ip, mount, cert):
        ident = record['id']; name = PROJECT+'-u-'+ident
        resources = self.resources(record)
        row = self.inspect('container', name)
        if row:
            self.assert_owned(row, ident)
            if row['Image'] != record['image']:
                raise ManagerError('runtime_image_mismatch')
            row = self.apply_resources(name, row, resources)
            if not row['State']['Running']:
                run(['docker', 'start', name])
            return self.assert_owned(self.inspect('container', name), ident)
        args = ['docker', 'create', '--name', name, *self.labels(ident), '--user', '10001:10001',
                '--network', network, '--ip', ip, '--cpus', str(resources['cpus']),
                '--memory', str(resources['memory_gib'])+'g', '--memory-swap', str(resources['memory_gib'])+'g',
                '--pids-limit', str(resources['pids_limit']), '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                '--init', '--restart', 'no', '--stop-timeout', '30', '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3',
                '--sysctl', 'net.ipv6.conf.all.disable_ipv6=1',
                '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=128m',
                '--tmpfs', '/run/flybot:rw,noexec,nosuid,nodev,size=4m,uid=10001,gid=10001,mode=0700']
        env = {'INSTANCE_ID': ident, 'PLATFORM_DOMAIN': self.domain, 'BOT_SOCIAL_PUBLIC_URL': 'https://social.'+self.domain,
               'BOT_SOCIAL_DISPLAY_NAME': 'FlyBot '+ident[:8], 'NODE_EXTRA_CA_CERTS': '/run/secrets/runtime-ca.pem',
               'NARB_DISABLE_NATIVE_CACHE': '1', 'PUBLIC_HTTPS_PORT': '443'}
        for k, v in env.items(): args.extend(['--env', k+'='+v])
        for src, dst, readonly in [(mount/'data', '/data', False), (mount/'workspace', '/workspace', False),
                                   (cert/'cert.pem', '/run/secrets/runtime-cert.pem', True),
                                   (cert/'key.pem', '/run/secrets/runtime-key.pem', True),
                                   (self.ca/'cert.pem', '/run/secrets/runtime-ca.pem', True)]:
            args.extend(['--mount', 'type=bind,src='+str(src)+',dst='+dst+(',readonly' if readonly else '')])
        args.append(record['image']); run(args); run(['docker', 'start', name])
        return self.assert_owned(self.inspect('container', name), ident)

    def withdraw(self, ident):
        (self.route_dir/('tenant-'+ident+'.toml')).unlink(missing_ok=True)

    def reconcile(self, instance):
        ident = self.identity(instance); desired = instance['desired']; name = PROJECT+'-u-'+ident
        if desired == 'running' and (self.root/'renewal-state'/(ident+'.json')).exists():
            self.withdraw(ident)
            self.bridge('observe', id=instance['id'], revision=instance['revision'], desired=desired,
                        status='starting', version=self.cfg['version'])
            return
        status = 'starting'; error = None
        try:
            if desired != 'running':
                self.withdraw(ident)
                row = self.inspect('container', name)
                if row:
                    self.assert_owned(row, ident)
                    if row['State']['Running']: run(['docker', 'stop', name], timeout=45)
                status = 'suspended' if desired == 'suspended' else 'stopped'
            else:
                record = self.record(ident)
                network, ip = self.network(record)
                mount = self.filesystem(ident, self.resources(record)['storage_gib']); cert = self.certificate(ident)
                row = self.container(instance, record, network, ip, mount, cert)
                healthy = row['State'].get('Health', {}).get('Status')
                if healthy == 'healthy': status = 'ready'
                elif healthy == 'unhealthy': raise ManagerError('runtime_unhealthy')
                if status == 'ready':
                    accepted = self.bridge('observe', id=instance['id'], revision=instance['revision'], desired=desired, status=status, version=record['version'])
                    if not accepted['accepted']:
                        self.withdraw(ident); return
                    text = self.routes.render(self.domain, ipaddress.ip_network(record['subnet']), [{'instance_id': ident, 'status': 'ready', 'ipv4': ip}], http_entrypoint='webtunnel')
                    # The shared security middleware is defined once in canary routes.
                    text = text[text.index('[http.routers.'):]
                    text = text.replace('middlewares = ["flybot-security"]', 'middlewares = ["flybot-security", "flybot-account-gate"]')
                    path = self.route_dir/('tenant-'+ident+'.toml')
                    if not path.exists() or path.read_text() != text: atomic(path, text, uid=10001)
                    return
                else:
                    self.withdraw(ident)
        except (ManagerError, subprocess.TimeoutExpired, OSError, ValueError, KeyError) as exc:
            self.withdraw(ident)
            # Failure must not leave a runtime running without enforced policy.
            row = self.inspect('container', name)
            if row:
                self.assert_owned(row, ident)
                if row['State']['Running']: run(['docker', 'stop', name], timeout=45)
            status = 'failed'; error = str(exc) if isinstance(exc, ManagerError) else 'manager_operation_failed'
            print('instance='+ident+' state=failed code='+error, flush=True)
        self.bridge('observe', id=instance['id'], revision=instance['revision'], desired=desired, status=status, version=record['version'] if status == 'ready' else self.cfg['version'], error_code=error)

    def launch(self, request):
        ident = uuid.UUID(request['instance_id']).hex; name = PROJECT+'-u-'+ident
        row = self.assert_owned(self.inspect('container', name), ident)
        if row['State'].get('Health', {}).get('Status') != 'healthy': return
        ready = json.loads(run(['docker', 'exec', name, 'node', '-e', "process.stdout.write(require('fs').readFileSync('/run/flybot/ready.json','utf8'))"]))
        if ready['instance_id'] != ident: raise ManagerError('launch_identity_mismatch')
        self.bridge('launch', request_id=request['id'], url=ready['launch_url'])

    def once(self):
        snapshot = self.bridge('snapshot')
        for instance in snapshot['instances']: self.reconcile(instance)
        # Re-read after state changes; expired/deleted requests must not be used.
        for request in self.bridge('snapshot')['launches']:
            try: self.launch(request)
            except (ManagerError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
                print('launch_handoff_unavailable', flush=True)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--config', type=Path, required=True); parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0: raise ManagerError('root_required')
    with open('/run/flybot-platform-botawaken-manager.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = Worker(args.config)
        while True:
            try: worker.once()
            except Exception:
                # No traceback/subprocess output, which could contain credentials.
                print('manager_cycle_failed', flush=True)
                if args.once: raise SystemExit(1)
            if args.once: break
            time.sleep(1)


if __name__ == '__main__': main()
