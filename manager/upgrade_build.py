"""Fetch npm package files from an operator-selected repository and immutable commit."""
from __future__ import annotations
import base64
import concurrent.futures
import fnmatch
import hashlib
import io
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import urllib.error
import urllib.parse
import tarfile
import tempfile
import time
import urllib.request

def source_repository():
    repository = os.environ.get('FLYBOT_SOURCE_REPOSITORY', 'zy95-12/FlyBot')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or any(
            part in {'.', '..'} for part in repository.split('/')):
        raise UpgradeError('source_invalid')
    return repository


def source_ref():
    ref = os.environ.get('FLYBOT_SOURCE_REF', 'main')
    if not ref or len(ref) > 200 or not re.fullmatch(r'[A-Za-z0-9_./-]+', ref) or '..' in ref:
        raise UpgradeError('source_invalid')
    return ref
PACKAGES = [('core', 'ds-bot', 'core-package', 'flybot-core.tgz'),
            ('plugins/social', '@flybot/social', 'social-package', 'flybot-social.tgz'),
            ('plugins/tree', '@flybot/tree', 'tree-package', 'flybot-tree.tgz'),
            ('plugins/workflow', '@flybot/workflow', 'workflow-package', 'flybot-workflow.tgz')]


class UpgradeError(Exception):
    pass


def _github_headers(url):
    # URLs are constructed only from the operator repository, hex commit and
    # validated Git tree paths; no HTTP caller supplies a URL or command.
    headers = {'User-Agent': 'FlyBot-Platform-Upgrader/1'}
    if urllib.parse.urlsplit(url).hostname == 'api.github.com':
        headers['Accept'] = 'application/vnd.github+json'
        # Unauthenticated calls from this host hit GitHub's IP rate limit
        # (HTTP 403 -> source_unavailable). Prefer an optional local token.
        token = (os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN') or '').strip()
        if not token:
            token_path = Path(os.environ.get('FLYBOT_GITHUB_TOKEN_FILE',
                str(Path(os.environ.get('FLYBOT_PLATFORM_ROOT', '/srv/flybot-platform-botawaken'))/'secrets/github-api-token')))
            try:
                if token_path.is_file():
                    token = token_path.read_text(encoding='utf-8').strip()
            except OSError:
                token = ''
        if token:
            headers['Authorization'] = 'Bearer ' + token
    return headers


def download(url, limit=16*1024*1024):
    headers = _github_headers(url)
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                data = response.read(limit+1)
            if len(data) > limit:
                raise UpgradeError('source_invalid')
            return data
        except UpgradeError:
            raise
        except Exception as error:
            # Only fixed public source locations and error types enter the
            # journal; never response bodies, request headers or credentials.
            location = urllib.parse.urlsplit(url)
            print('source_fetch_retry '+location.hostname+location.path+' '+
                  type(error).__name__+' '+str(getattr(error, 'code', '')), flush=True)
            if attempt < 2:
                time.sleep(attempt+1)
    # A second HTTPS client handles transient resolver/transport failures.
    # Keep certificate verification, bounded output and Git blob validation.
    curl_headers = ['--header', 'User-Agent: '+headers['User-Agent']]
    if 'Accept' in headers:
        curl_headers.extend(['--header', 'Accept: '+headers['Accept']])
    if 'Authorization' in headers:
        curl_headers.extend(['--header', 'Authorization: '+headers['Authorization']])
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(['curl', '--fail', '--silent', '--show-error',
                '--location', '--proto', '=https', '--proto-redir', '=https',
                '--connect-timeout', '10', '--max-time', '45', '--retry', '2',
                '--max-filesize', str(limit), *curl_headers,
                url], stdout=output, stderr=subprocess.DEVNULL, timeout=150)
            output.seek(0)
            data = output.read(limit+1)
            if len(data) > limit:
                raise UpgradeError('source_invalid')
            if result.returncode == 0:
                return data
        except (OSError, subprocess.TimeoutExpired):
            pass
    raise UpgradeError('source_unavailable') from None


def latest_commit():
    import re
    result = json.loads(download('https://api.github.com/repos/'+source_repository()+'/commits/'+urllib.parse.quote(source_ref(), safe=''), 2*1024*1024))
    commit = result.get('sha', '')
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise UpgradeError('source_invalid')
    return commit


def selected(path, files):
    if path in {'package.json', 'README.md', 'README', 'LICENSE', 'LICENSE.md'}:
        return True
    positive = [p.rstrip('/') for p in files if isinstance(p, str) and not p.startswith('!')]
    negative = [p[1:].rstrip('/') for p in files if isinstance(p, str) and p.startswith('!')]
    def match(pattern):
        return fnmatch.fnmatchcase(path, pattern) or path.startswith(pattern+'/')
    return any(match(p) for p in positive) and not any(match(p) for p in negative)


def fetch_sources(commit, destination, cache, pulse):
    # Prefer codeload: raw.githubusercontent.com often times out from this host
    # while api.github.com/codeload stay reachable (source_unavailable on c5a6fffa).
    try:
        return fetch_sources_archive(commit, destination, cache, pulse)
    except UpgradeError:
        pass
    try:
        tree = json.loads(download('https://api.github.com/repos/'+source_repository()+'/git/trees/'+commit+'?recursive=1', 8*1024*1024))
    except UpgradeError:
        # GitHub's API/raw endpoints can fail independently of codeload. The
        # archive endpoint contains the same immutable commit and lets an
        # upgrade proceed without changing the selected revision.
        return fetch_sources_archive(commit, destination, cache, pulse)
    if tree.get('truncated') or not isinstance(tree.get('tree'), list):
        raise UpgradeError('source_invalid')
    entries = {row['path']: row for row in tree['tree'] if row.get('type') == 'blob'}
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    def fetch(path):
        row = entries.get(path)
        if not row or row.get('mode') not in {'100644', '100755'}:
            raise UpgradeError('source_invalid')
        safe = PurePosixPath(path)
        if safe.is_absolute() or '..' in safe.parts or len(path) > 500:
            raise UpgradeError('source_invalid')
        digest = row['sha']
        if not re.fullmatch(r'[a-f0-9]{40}', digest):
            raise UpgradeError('source_invalid')
        item = cache/digest
        data = item.read_bytes() if item.is_file() else download('https://raw.githubusercontent.com/'+source_repository()+'/'+commit+'/'+path)
        if hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest() != digest:
            raise UpgradeError('source_invalid')
        if not item.exists():
            fd, temporary = tempfile.mkstemp(prefix='.blob-', dir=cache)
            try:
                with os.fdopen(fd, 'wb') as stream: stream.write(data)
                os.replace(temporary, item)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)
        target = destination/path; target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        target.write_bytes(data); target.chmod(0o644)
        return data
    manifests = {}
    paths = []
    for prefix, name, _, _ in PACKAGES:
        manifest = json.loads(fetch(prefix+'/package.json'))
        if manifest.get('name') != name or not isinstance(manifest.get('files'), list):
            raise UpgradeError('source_invalid')
        manifests[name] = manifest
        paths.extend(p for p in entries if p.startswith(prefix+'/') and selected(p[len(prefix)+1:], manifest['files']))
    if len(paths) > 1200 or sum(entries[p].get('size', 0) for p in paths) > 64*1024*1024:
        raise UpgradeError('source_invalid')
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch, p) for p in paths]
        pending = set(futures)
        while pending:
            done, pending = concurrent.futures.wait(pending, timeout=5, return_when=concurrent.futures.FIRST_COMPLETED)
            pulse()
            for f in done:
                f.result()
    return manifests


def fetch_sources_archive(commit, destination, cache, pulse):
    data = download('https://codeload.github.com/'+source_repository()+'/tar.gz/'+commit, 64*1024*1024)
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        members = archive.getmembers()
        roots = {m.name.split('/', 1)[0] for m in members if '/' in m.name}
        if len(roots) != 1: raise UpgradeError('source_invalid')
        root = next(iter(roots))
        files = {}
        for prefix, name, _, _ in PACKAGES:
            member = archive.getmember(root+'/'+prefix+'/package.json')
            manifest = json.loads(archive.extractfile(member).read())
            if manifest.get('name') != name or not isinstance(manifest.get('files'), list):
                raise UpgradeError('source_invalid')
            files[name] = manifest
        for prefix, name, _, _ in PACKAGES:
            manifest = files[name]
            for member in members:
                path = member.name[len(root+'/'+prefix+'/'):] if member.name.startswith(root+'/'+prefix+'/') else None
                if not path or not member.isfile() or not selected(path, manifest['files']): continue
                if len(path) > 500 or '..' in PurePosixPath(path).parts: raise UpgradeError('source_invalid')
                target = destination/prefix/path; target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read()); target.chmod(0o644)
        pulse()
    return files


def build(runner, stage, commit, base):
    runner.progress('fetching', commit=commit)
    source = stage/'source'; source.mkdir(mode=0o755, exist_ok=True)
    manifests = fetch_sources(commit, source, runner.root/'upgrades/blob-cache', runner.pulse)
    # Read only immutable package metadata from the base image.
    # Missing names are null so a newly registered managed pack can be introduced.
    code = ("const fs=require('fs');const names="+json.dumps([p[1] for p in PACKAGES])+";"
            "const out={};for (const n of names){try{out[n]=JSON.parse(fs.readFileSync("
            "'/opt/flybot/node_modules/'+n+'/package.json','utf8'))}catch(e){out[n]=null}}"
            "console.log(JSON.stringify(out))")
    previous = json.loads(runner.command(['docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--memory', '128m', '--pids-limit', '32', '--entrypoint', 'node', base, '-e', code], 30, 'build_failed'))
    for name, manifest in manifests.items():
        prev = previous.get(name)
        if prev is None:
            # First introduction of this managed pack into the image lineage.
            continue
        for field in ('dependencies', 'peerDependencies', 'optionalDependencies', 'peerDependenciesMeta', 'engines', 'os', 'cpu'):
            if manifest.get(field) != prev.get(field):
                raise UpgradeError('dependency_review_required')
    source.chmod(0o755)
    for path in source.rglob('*'):
        if path.is_dir(): path.chmod(0o755)
    context = stage/'context'; context.mkdir(mode=0o755, exist_ok=True)
    vendor = context/'vendor'; vendor.mkdir(mode=0o700, exist_ok=True)
    os.chown(vendor, 10001, 10001)
    lock = json.loads(runner.command(['docker', 'run', '--rm', '--network', 'none', '--read-only', '--memory', '128m', '--pids-limit', '32', '--cap-drop', 'ALL', '--entrypoint', 'node', base, '-e', "process.stdout.write(require('fs').readFileSync('/opt/flybot/package-lock.json','utf8'))"], 30, 'build_failed'))
    runner.progress('building')
    for prefix, name, out, filename in PACKAGES:
        container = 'flybot-upgrade-pack-'+runner.job['id'][:8]+'-'+out
        args = ['docker', 'run', '--rm', '--name', container, '--label', 'com.docker.compose.project='+runner.project,
                '--label', 'io.flybot.upgrade-job='+runner.job['id'], '--user', '10001:10001',
                '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                '--cpus', '1', '--memory', '512m', '--pids-limit', '64', '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001',
                '--mount', 'type=bind,src='+str(source)+',dst=/src,readonly', '--mount', 'type=bind,src='+str(vendor)+',dst=/out',
                '--workdir', '/src/'+prefix, '--env', 'npm_config_cache=/tmp/npm-cache', '--env', 'npm_config_update_notifier=false',
                '--entrypoint', 'npm', base, 'pack', '--ignore-scripts', '--json', '--pack-destination', '/out']
        packed = json.loads(runner.command(args, 90, 'build_failed'))
        original = vendor/Path(packed[0]['filename']).name; target = vendor/filename
        original.rename(target); data = target.read_bytes()
        key = 'node_modules/'+name
        entry = lock['packages'].get(key) or {
            'version': manifests[name]['version'],
            'resolved': 'file:vendor/'+filename,
            'license': manifests[name].get('license') or 'Apache-2.0',
        }
        entry['version'] = manifests[name]['version']
        entry['resolved'] = 'file:vendor/'+filename
        entry['integrity'] = 'sha512-'+base64.b64encode(hashlib.sha512(data).digest()).decode()
        if manifests[name].get('peerDependencies') is not None:
            entry['peerDependencies'] = manifests[name]['peerDependencies']
        if manifests[name].get('dependencies') is not None:
            entry['dependencies'] = manifests[name]['dependencies']
        lock['packages'][key] = entry
        root = lock['packages'].setdefault('', {})
        root.setdefault('dependencies', {})[name] = 'file:vendor/'+filename
        dest = context/out; dest.mkdir(exist_ok=True)
        with tarfile.open(target) as archive:
            for row in archive:
                path = PurePosixPath(row.name)
                if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'package' or not (row.isdir() or row.isfile()):
                    raise UpgradeError('source_invalid')
                file = dest.joinpath(*path.parts[1:])
                if row.isdir(): file.mkdir(parents=True, exist_ok=True)
                else:
                    file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(archive.extractfile(row).read())
    # Keep hosted runtime manifests in sync with PACKAGES (deps, bundles, MANAGED).
    overlay = runner.root/'build-20261020'/'runtime'
    for name in ('package.json', 'profile.json', 'supervisor.mjs'):
        shutil.copy2(overlay/name, context/name)
    (context/'package-lock.json').write_text(json.dumps(lock, indent=2)+'\n')
    (context/'provenance.json').write_text(json.dumps({'flybot_repository': source_repository(), 'flybot_ref': source_ref(), 'flybot_commit': commit, 'base_image': base, 'dependency_manifests_unchanged': True})+'\n')
    rm_paths = ' '.join('/opt/flybot/node_modules/'+name for _, name, _, _ in PACKAGES)
    # BuildKit resolves bare FROM sha256:… as docker.io/library/sha256:… and can
    # Hub-timeout even with --pull=false when the digest is only local. Retag onto
    # a project-local name and FROM that tag instead.
    base_from = base
    if isinstance(base, str) and base.startswith('sha256:') and len(base) >= 15:
        base_from = runner.project + '/runtime:overlay-base-' + base[7:19]
        runner.command(['docker', 'tag', base, base_from], 30, 'build_failed')
    dockerfile = 'FROM '+base_from+'\nUSER root\nRUN rm -rf '+rm_paths+' && rm -f /opt/flybot/node_modules/.package-lock.json\n'
    for _, name, out, _ in PACKAGES:
        dockerfile += 'COPY '+out+'/ /opt/flybot/node_modules/'+name+'/\n'
    dockerfile += 'COPY package.json profile.json supervisor.mjs package-lock.json provenance.json /opt/flybot/\nCOPY vendor/ /opt/flybot/vendor/\nLABEL org.opencontainers.image.revision="'+commit+'"\nUSER 10001:10001\n'
    (context/'Dockerfile').write_text(dockerfile)
    # The coordinator uses umask 077, while immutable package files must be
    # readable by UID 10001 inside the resulting image.
    context.chmod(0o755)
    for path in context.rglob('*'):
        path.chmod(0o755 if path.is_dir() else 0o644)
    tag = runner.project+'/runtime:'+commit
    runner.command(['docker', 'build', '--pull=false', '--network', 'none', '-t', tag, str(context)], 180, 'build_failed')
    return runner.inspect('image', tag)['Id']
