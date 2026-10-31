#!/usr/bin/env python3
"""Root-only Bot Social upgrade worker used by the durable admin queue."""
from __future__ import annotations
import json, os, pathlib, re, shutil, subprocess, sys, tarfile, tempfile, time, urllib.request

ROOT = pathlib.Path('/srv/flybot-platform-botawaken')
REPO = 'https://github.com/Birfy/bot-social.git'
BASE = 'sha256:6f8a77b02dbd1e82b537f0012b2958a077d45b6b5128517c1dbd98aaaa6a6361'

def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs).stdout.strip()

def run_rollout(args):
    try:
        return run(args)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or '').strip().splitlines()
        marker = detail[-1] if detail else 'rollout_failed'
        if 'already_current' in marker:
            return 'already_current'
        raise RuntimeError('rollout_failed:' + marker[-48:]) from exc

def fetch_commit() -> str:
    last = None
    for attempt in range(3):
        try:
            value = run(['git', '-c', 'http.version=HTTP/1.1', 'ls-remote', REPO, 'HEAD'], timeout=10).split()[0]
            if len(value) == 40: return value
        except Exception as exc:
            last = exc
        time.sleep(2 ** attempt)
    # A completed/failed job may already contain a verified source archive and
    # its immutable revision. Use that cache during a temporary GitHub outage.
    candidates = []
    for rollout in ROOT.glob('upgrades/jobs/*/bot-social/rollout.py'):
        match = re.search(r"org\.opencontainers\.image\.revision=([0-9a-f]{40})", rollout.read_text(errors='ignore'))
        if match: candidates.append((rollout.stat().st_mtime, match.group(1)))
    if candidates: return max(candidates)[1]
    raise RuntimeError('source_unavailable') from last

def fetch_archive(url: str, target: pathlib.Path) -> None:
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as response, target.open('wb') as stream:
                shutil.copyfileobj(response, stream)
            if target.stat().st_size > 0: return
        except Exception as exc:
            last = exc
        time.sleep(2 ** attempt)
    commit = pathlib.PurePosixPath(url).stem.removesuffix('.tar')
    for archive in ROOT.glob('upgrades/jobs/*/bot-social/source.tgz'):
        rollout = archive.parent / 'rollout.py'
        if rollout.exists() and commit in rollout.read_text(errors='ignore'):
            shutil.copy2(archive, target)
            return
    raise RuntimeError('source_unavailable') from last

def main(job_id: str) -> None:
    stage = ROOT / 'upgrades/jobs' / job_id / 'bot-social'
    stage.mkdir(mode=0o700, parents=True, exist_ok=True)
    commit = fetch_commit()
    archive = stage / 'source.tgz'
    fetch_archive(f'https://github.com/Birfy/bot-social/archive/{commit}.tar.gz', archive)
    extract = stage / 'source'; extract.mkdir()
    with tarfile.open(archive, 'r:gz') as tf:
        for member in tf.getmembers():
            target = (extract / member.name).resolve()
            if not str(target).startswith(str(extract.resolve()) + os.sep): raise RuntimeError('source_invalid')
        tf.extractall(extract)
    source = next(extract.iterdir())
    context = stage / 'context'; context.mkdir()
    for name in ('package.json', 'package-lock.json', 'src', 'scripts', 'public', 'docs'):
        src = source / name
        if src.exists(): shutil.copytree(src, context / name) if src.is_dir() else shutil.copy2(src, context / name)
    (context / 'Dockerfile').write_text(f'''FROM {BASE}\nWORKDIR /app\nCOPY package.json package-lock.json ./\nCOPY src ./src\nCOPY scripts ./scripts\nCOPY public ./public\nCOPY docs ./docs\nUSER root\nRUN node --import tsx src/botctl-build.ts\nUSER 10001:10001\nLABEL org.opencontainers.image.revision="{commit}"\n''')
    image = run(['docker', 'build', '--network', 'none', '-q', '-t', f'flybot-platform-botawaken/bot-social:{commit[:8]}', str(context)])
    image = run(['docker', 'image', 'inspect', f'flybot-platform-botawaken/bot-social:{commit[:8]}', '--format', '{{.Id}}'])
    private = ROOT / 'private.env'
    old_image = next(line.split('=', 1)[1] for line in private.read_text().splitlines() if line.startswith('BOTSOCIAL_IMAGE='))
    backup = run(['/usr/bin/python3', str(ROOT / 'manager/operations.py'), 'backup-shared'])
    (stage / 'backup-path').write_text(backup.split('path=', 1)[-1].strip())
    template = ROOT / 'bot-social-upgrade-20261024-8c64f330/rollout.py'
    rollout = (stage / 'rollout.py')
    text = template.read_text()
    # Names include the queue job, so a retry of the same upstream commit can
    # never collide with an older rollback/canary container.
    replacements = {'bot-social-upgrade-20261024-8c64f330': f'upgrades/jobs/{job_id}/bot-social', '20261024-8c64f330': f'job-{job_id[:8]}-social', '8c64f330c880998423909255b8767cecb974249e': commit,
                    'bot-social:8c64f330': f'bot-social:{commit[:8]}', 'rollback-8c64f330': f'rollback-job-{job_id[:8]}',
                    'canary-8c64f330': f'canary-job-{job_id[:8]}'}
    for old, new in replacements.items(): text = text.replace(old, new)
    # The compose manager can briefly restart the old container after a
    # health probe. Wait for a stable healthy state before taking the lock,
    # while keeping a hard upper bound so a genuinely broken release fails.
    one_shot = "old=inspect(NAME)\nif old['Config']['Labels'].get('com.docker.compose.project')!=PROJECT or not old['State']['Running'] or old['State']['Health']['Status']!='healthy':raise RuntimeError('old_not_healthy_or_owned')"
    bounded = "old=None\nfor _ in range(30):\n old=inspect(NAME)\n if old['Config']['Labels'].get('com.docker.compose.project')==PROJECT and old['State']['Running'] and old.get('State',{}).get('Health',{}).get('Status')=='healthy':break\n time.sleep(2)\nelse:raise RuntimeError('old_not_healthy_or_owned')"
    text = text.replace(one_shot, bounded)
    rollout.write_text(text)
    run_rollout(['/usr/bin/python3', str(rollout)])

if __name__ == '__main__':
    if len(sys.argv) != 2: raise SystemExit('job_id_required')
    main(sys.argv[1])
