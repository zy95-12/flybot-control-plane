#!/usr/bin/env python3
"""Root-only coordinator; the HTTP API queues intent, never Docker arguments."""
from __future__ import annotations
import fcntl
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from bridge_client import BridgeClient, BridgeError
from upgrade_build import UpgradeError, latest_commit, build, PACKAGES
from worker import PROJECT, atomic

ROOT = Path('/srv/flybot-platform-botawaken')
CONFIG = ROOT/'manager/config.json'
CANARY = PROJECT+'-runtime-canary-1'

# Readiness checks the plugin's authenticated state API without logging its
# bearer URL, cookie, state contents, credentials or model configuration.
CANARY_PROBE = r"""
const fs=require('fs'),https=require('https');
const ready=JSON.parse(fs.readFileSync('/run/flybot/ready.json','utf8'));
if(JSON.parse(fs.readFileSync('/run/flybot/platform.patch.yml')).find(x=>x.id==='typert-gateway')?.config.websocketHeartbeatIntervalMs!==15000)process.exit(1);
const url=new URL(ready.launch_url),ca=fs.readFileSync('/run/secrets/runtime-ca.pem');
const bind=Object.values(require('os').networkInterfaces()).flat().find(x=>x.family==='IPv4'&&!x.internal).address;
const get=(path,cookie,body)=>new Promise((resolve,reject)=>{
 const req=https.request({hostname:bind,port:3080,servername:url.hostname,ca,path,method:body?'POST':'GET',headers:{Host:url.host,...(cookie?{Cookie:cookie}:{}),...(body?{'Content-Type':'application/json'}:{})},timeout:5000},res=>{
  let data='';res.on('data',c=>{data+=c;if(data.length>8388608)req.destroy(new Error('response_limit'))});res.on('end',()=>resolve({status:res.statusCode,headers:res.headers,data}));res.on('error',reject);
 });req.on('timeout',()=>req.destroy(new Error('timeout')));req.on('error',reject);req.end(body);
});
(async()=>{const r=await get('/'+url.search);if(r.status!==303&&r.status!==302)throw new Error('auth');const cookie=(r.headers['set-cookie']||[]).map(s=>s.split(';',1)[0]).join('; ');const state=await get('/api/bot',cookie,JSON.stringify({endpoint:'state',payload:{watching:false}}));if(state.status!==200||!JSON.parse(state.data).ok)throw new Error('plugin');const wf=await get('/api/bot',cookie,JSON.stringify({endpoint:'flybot/workflow/state',payload:{}}));if(wf.status!==200||!JSON.parse(wf.data).ok)throw new Error('workflow');console.log('ready')})().catch(()=>process.exit(1));
"""


class Runner:
    def __init__(self):
        self.root = ROOT; self.project = PROJECT
        self.bridge = BridgeClient(PROJECT+'-control-plane-1')
        self.job = None; self.phase = 'checking'; self.last_pulse = 0
        self.stopping = False; self.sequence = 0
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(self, 'stopping', True))

    def inspect(self, kind, name, missing=False):
        p = subprocess.run(['docker', kind, 'inspect', name], capture_output=True, text=True, timeout=15)
        if p.returncode:
            if missing and 'no such' in p.stderr.lower(): return None
            raise UpgradeError('release_unavailable')
        row = json.loads(p.stdout)[0]
        if kind != 'image':
            labels = row.get('Config', {}).get('Labels', {}) if kind == 'container' else row.get('Labels', {})
            if not labels or labels.get('com.docker.compose.project') != PROJECT:
                raise UpgradeError('release_unavailable')
        return row

    @contextmanager
    def maintenance(self):
        # Certificate renewal and manual snapshots use this same lock. Do not
        # hold it while invoking operations.py, which acquires it itself.
        with open('/run/flybot-platform-botawaken-operations.lock', 'a') as lock:
            deadline = time.monotonic()+60
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline: raise UpgradeError('maintenance_busy') from None
                    self.pulse(); time.sleep(1)
            yield

    def progress(self, phase=None, **values):
        if phase: self.phase = phase
        result = self.bridge.request({'operation': 'upgrade_progress', 'id': self.job['id'], 'phase': self.phase, **values})
        if not result.get('accepted'): raise UpgradeError('interrupted')
        self.last_pulse = time.monotonic()

    def pulse(self):
        if self.job and not self.job.get('recovering') and time.monotonic()-self.last_pulse >= 8:
            try: self.progress()
            except BridgeError:
                # A panel replacement may briefly interrupt the private bridge.
                # Finish the bounded command before deciding the job outcome.
                self.last_pulse = time.monotonic()-4

    def command(self, args, timeout, error):
        if self.stopping: raise UpgradeError('interrupted')
        self.sequence += 1
        logfile = self.stage/('step-'+str(self.sequence)+'.log')
        errorfile = self.stage/('step-'+str(self.sequence)+'.stderr.log')
        with logfile.open('wb') as stream, errorfile.open('wb') as errors:
            os.chmod(logfile, 0o600)
            os.chmod(errorfile, 0o600)
            process = subprocess.Popen(args, stdout=stream, stderr=errors)
            deadline = time.monotonic()+timeout
            try:
                while process.poll() is None:
                    self.pulse()
                    if time.monotonic() >= deadline:
                        if str(ROOT/'manager/operations.py') in args:
                            # The snapshot/rollback tool has its own bounded
                            # subprocess and readiness timeouts. Killing it mid
                            # restore can leave storage unmounted; let it finish.
                            deadline = float('inf')
                            continue
                        process.kill(); process.wait(timeout=10)
                        raise UpgradeError('operation_timeout')
                    time.sleep(.4)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=10)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=10)
        data = logfile.read_bytes()[:2*1024*1024].decode('utf8', errors='replace')
        if process.returncode:
            failure = errorfile.read_bytes()[:2*1024*1024].decode('utf8', errors='replace')
            if 'upgrade_failed_rollback_ready' in data+failure:
                raise UpgradeError('upgrade_failed_rollback_ready')
            raise UpgradeError(error)
        return data

    def canary(self, image):
        old = self.inspect('container', CANARY)
        if old['Image'] == image:
            self.command(['docker', 'exec', CANARY, 'node', '-e', CANARY_PROBE], 20, 'canary_failed')
            return None
        if not old['State']['Running'] or len(old['NetworkSettings']['Networks']) != 1 or old['HostConfig'].get('PortBindings'):
            raise UpgradeError('canary_failed')
        network, options = next(iter(old['NetworkSettings']['Networks'].items()))
        if network != PROJECT+'_runtime_canary': raise UpgradeError('canary_failed')
        backup = CANARY+'-rollback-'+self.job['id'][:8]
        state = {'backup': backup, 'network': network, 'ip': options['IPAddress'], 'image': old['Image']}
        atomic(self.stage/'canary.json', json.dumps(state))
        env = self.stage/'canary.env'
        atomic(env, '\n'.join(old['Config']['Env'])+'\n')
        try:
            self.command(['docker', 'stop', CANARY], 45, 'canary_failed')
            self.command(['docker', 'rename', CANARY, backup], 15, 'canary_failed')
            self.command(['docker', 'network', 'disconnect', network, backup], 15, 'canary_failed')
            host = old['HostConfig']
            args = ['docker', 'create', '--name', CANARY, '--network', network, '--ip', options['IPAddress'], '--network-alias', 'runtime-canary',
                    '--env-file', str(env), '--restart', 'unless-stopped', '--read-only', '--init', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                    '--cpus', str(host['NanoCpus']/1e9), '--memory', str(host['Memory']), '--pids-limit', str(host['PidsLimit']),
                    '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3', '--stop-timeout', '30']
            for path, flags in host.get('Tmpfs', {}).items(): args.extend(['--tmpfs', path+':'+flags])
            for mount in old['Mounts']:
                if mount['Type'] == 'tmpfs': continue
                kind = mount['Type']; source = mount['Name'] if kind == 'volume' else mount['Source']
                if kind == 'volume': self.inspect('volume', source)
                elif kind != 'bind' or not source.startswith(str(ROOT)+'/'): raise UpgradeError('canary_failed')
                args.extend(['--mount', 'type='+kind+',src='+source+',dst='+mount['Destination']+(',readonly' if not mount['RW'] else '')])
            for key, value in old['Config']['Labels'].items(): args.extend(['--label', key+'='+value])
            args.extend(['--label', 'io.flybot.upgrade-job='+self.job['id'], image])
            self.command(args, 20, 'canary_failed')
            self.command(['docker', 'start', CANARY], 15, 'canary_failed')
            deadline = time.monotonic()+150
            while time.monotonic() < deadline:
                row = self.inspect('container', CANARY)
                if not row['State']['Running']: raise UpgradeError('canary_failed')
                if row['State'].get('Health', {}).get('Status') == 'healthy':
                    self.command(['docker', 'exec', CANARY, 'node', '-e', CANARY_PROBE], 20, 'canary_failed')
                    return state
                self.pulse(); time.sleep(1)
            raise UpgradeError('canary_failed')
        except Exception:
            self.restore_canary(state)
            raise
        finally:
            env.unlink(missing_ok=True)

    def restore_canary(self, state):
        # Recovery uses only recorded, owned names and never caller-selected paths.
        if not state: return
        backup = self.inspect('container', state['backup'], missing=True)
        if not backup:
            current = self.inspect('container', CANARY, missing=True)
            if current and current['Image'] == state['image'] and not current['State']['Running']:
                subprocess.run(['docker', 'start', CANARY], check=True, capture_output=True, timeout=20)
            return
        current = self.inspect('container', CANARY, missing=True)
        if current:
            if current['Config']['Labels'].get('io.flybot.upgrade-job') != self.job['id']:
                raise UpgradeError('rollback_failed')
            subprocess.run(['docker', 'rm', '-f', CANARY], check=True, capture_output=True, timeout=45)
        if state['network'] not in backup['NetworkSettings']['Networks']:
            subprocess.run(['docker', 'network', 'connect', '--ip', state['ip'], '--alias', 'runtime-canary', state['network'], state['backup']], check=True, capture_output=True, timeout=20)
        subprocess.run(['docker', 'rename', state['backup'], CANARY], check=True, capture_output=True, timeout=15)
        subprocess.run(['docker', 'start', CANARY], check=True, capture_output=True, timeout=20)

    def promote(self, image, version, commit):
        if self.stopping: raise UpgradeError('interrupted')
        self.progress('promoting')
        path = ROOT/'manager/config.json'; config = json.loads(path.read_text())
        atomic(self.stage/'manager.before.json', path.read_text())
        config.update(runtime_image=image, version=version)
        env = ROOT/'private.env'; atomic(self.stage/'private.env.before', env.read_text())
        lines = env.read_text().splitlines()
        if not any(line.startswith('FLYBOT_RUNTIME_IMAGE=') for line in lines): raise UpgradeError('release_unavailable')
        lines = ['FLYBOT_RUNTIME_IMAGE='+image if line.startswith('FLYBOT_RUNTIME_IMAGE=') else line for line in lines]
        atomic(path, json.dumps(config, indent=2)+'\n'); atomic(env, '\n'.join(lines)+'\n')
        atomic(ROOT/'runtime-image-id', image+'\n')
        atomic(ROOT/'upgrades/current.json', json.dumps({'commit': commit, 'image': image, 'version': version}))
        release = ROOT/'release-20261020/deploy'
        source = release/'sources.json'
        if source.exists():
            data = json.loads(source.read_text()); data['flybot']['commit'] = commit; atomic(source, json.dumps(data, indent=2)+'\n')
        built = self.stage/'build/context/package-lock.json'
        if built.exists(): atomic(release/'runtime/package-lock.json', built.read_text(), mode=0o644)
        self.command(['systemctl', 'restart', 'flybot-platform-manager.service'], 60, 'manager_unavailable')

    def process(self, job):
        self.job = job; self.phase = 'checking'; self.last_pulse = 0
        self.stage = ROOT/'upgrades/jobs'/str(uuid.UUID(job['id'])); self.stage.mkdir(mode=0o700, parents=True, exist_ok=True)
        if str(job.get('version') or '').startswith('BotSocial-'):
            try:
                self.progress('checking', total=1, completed=0)
                self.command(['/usr/bin/python3', str(ROOT/'manager/bot_social_upgrade.py'), job['id']], 3600, 'upgrade_failed')
                self.progress('complete', total=1, completed=1)
            except Exception as error:
                code = str(error) if isinstance(error, UpgradeError) else 'upgrade_failed'
                try: self.progress('failed', error_code=code)
                except Exception: print('upgrade_status_unavailable', flush=True)
            finally:
                self.job = None
            return
        canary_state = None
        try:
            if shutil.disk_usage(self.stage).free < 2*1024**3: raise UpgradeError('disk_reserve_low')
            # Do not overlap manual maintenance, even on another instance.
            with open('/run/flybot-platform-botawaken-operations.lock', 'a') as lock:
                try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: raise UpgradeError('maintenance_busy') from None
            if subprocess.run(['systemctl', 'is-active', '--quiet', 'flybot-platform-manager.service']).returncode:
                raise UpgradeError('maintenance_busy')
            commit = latest_commit()
            config = json.loads(CONFIG.read_text())
            suffix = config['version'].split('_DSH-', 1)[1]
            version = 'FlyBot-'+commit[:8]+'_DSH-'+suffix
            self.progress('checking', commit=commit, version=version)
            base = json.loads(CONFIG.read_text())['runtime_image']
            image_row = self.inspect('image', base)
            def packs_present(image_id):
                # Rebuild when PACKAGES gained a managed plugin the image lacks.
                code = ("const fs=require('fs');const names="+json.dumps([p[1] for p in PACKAGES])+";"
                        "for (const n of names){try{fs.accessSync('/opt/flybot/node_modules/'+n+'/package.json')}"
                        "catch(e){process.exit(2)}}")
                try:
                    self.command(['docker', 'run', '--rm', '--network', 'none', '--read-only',
                                  '--memory', '64m', '--pids-limit', '32', '--cap-drop', 'ALL',
                                  '--entrypoint', 'node', image_id, '-e', code], 30, 'build_failed')
                    return True
                except UpgradeError:
                    return False
            image = None
            if image_row.get('Config', {}).get('Labels', {}).get('org.opencontainers.image.revision') == commit and packs_present(base):
                image = base
            else:
                cached = ROOT/'upgrades/releases'/(commit+'.json')
                image = json.loads(cached.read_text())['image'] if cached.exists() else None
                if image:
                    candidate = self.inspect('image', image)
                    if candidate.get('Config', {}).get('Labels', {}).get('org.opencontainers.image.revision') != commit or not packs_present(image):
                        image = None
                if image is None:
                    build_dir = self.stage/'build'; build_dir.mkdir(mode=0o755)
                    image = build(self, build_dir, commit, base)
                    atomic(cached, json.dumps({'image': image, 'commit': commit}))
            self.progress('canary')
            with self.maintenance():
                canary_state = self.canary(image)
            instances = self.bridge.request({'operation': 'snapshot'})['instances']
            self.progress('upgrading', total=len(instances), completed=0)
            changed = False
            for number, instance in enumerate(instances, 1):
                if self.stopping: raise UpgradeError('interrupted')
                ident = uuid.UUID(instance['id']).hex
                record = json.loads((ROOT/'manager-state'/(ident+'.json')).read_text())
                if record['image'] != image:
                    # The administrator instance may require a full 100 GiB
                    # snapshot. Allow the copy and verification to finish
                    # without treating a healthy operation as failed.
                    self.command(['/usr/bin/python3', str(ROOT/'manager/operations.py'), 'upgrade', ident, '--image', image, '--version', version, '--include-inactive'], 1800, 'upgrade_failed')
                    changed = True
                self.bridge.request({'operation': 'upgrade_version', 'id': job['id'], 'instance': instance['id']})
                self.progress('upgrading', total=len(instances), completed=number)
            if changed or canary_state or config['runtime_image'] != image or config['version'] != version:
                with self.maintenance():
                    self.promote(image, version, commit)
            if canary_state:
                self.inspect('container', canary_state['backup'])
                self.command(['docker', 'rm', canary_state['backup']], 20, 'release_unavailable')
            self.progress('complete' if changed or canary_state else 'current')
        except Exception as error:
            code = str(error) if isinstance(error, UpgradeError) else 'upgrade_failed'
            try:
                if canary_state:
                    with self.maintenance(): self.restore_canary(canary_state)
            except Exception:
                code = 'rollback_failed'
            try: self.progress('failed', error_code=code)
            except Exception:
                # Startup recovery turns abandoned running jobs into interrupted.
                print('upgrade_status_unavailable', flush=True)
        finally:
            for suffix in ('core-package', 'social-package', 'tree-package', 'workflow-package'):
                name = 'flybot-upgrade-pack-'+job['id'][:8]+'-'+suffix
                try:
                    row = self.inspect('container', name, missing=True)
                    if row and row['Config']['Labels'].get('io.flybot.upgrade-job') == job['id']:
                        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=20, check=True)
                except Exception:
                    print('upgrade_pack_cleanup_pending', flush=True)
            self.job = None

    def recover(self):
        # The exclusive coordinator lock guarantees no other runner is alive.
        # Restore a recorded canary swap if an earlier process was interrupted.
        for path in (ROOT/'upgrades/jobs').glob('*/canary.json'):
            state = json.loads(path.read_text())
            if self.inspect('container', state['backup'], missing=True):
                self.job = {'id': str(uuid.UUID(path.parent.name)), 'recovering': True}
                with self.maintenance(): self.restore_canary(state)
                self.job = None
        self.bridge.request({'operation': 'upgrade_recover'})

    def run(self):
        (ROOT/'upgrades/jobs').mkdir(mode=0o700, parents=True, exist_ok=True)
        self.recover()
        while not self.stopping:
            try:
                result = self.bridge.request({'operation': 'upgrade_claim'})
                if result.get('job'): self.process(result['job'])
            except Exception:
                # Never print bridge/subprocess data, which may contain secrets.
                print('upgrade_poll_unavailable', flush=True)
            for _ in range(3):
                if self.stopping: break
                time.sleep(1)
        self.bridge.close()


def main():
    if os.geteuid() != 0: raise SystemExit('root_required')
    os.umask(0o077); os.nice(10)
    with open('/run/flybot-platform-upgrader.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        Runner().run()


if __name__ == '__main__': main()
