"""Admin-only durable upgrade queue. No caller-controlled build commands."""
from __future__ import annotations
import re
import time
import uuid
from fastapi import Depends, HTTPException
from sqlalchemy import select
from .models import UpgradeJob, User, Instance, AuditEvent
from .store import get_db

ACTIVE = ('queued', 'running')
PHASES = {'queued', 'checking', 'fetching', 'building', 'canary', 'upgrading', 'promoting', 'complete', 'failed', 'current'}
ERRORS = {'source_unavailable', 'source_invalid', 'dependency_review_required', 'build_failed', 'canary_failed',
          'upgrade_failed', 'upgrade_failed_rollback_ready', 'maintenance_busy', 'interrupted', 'manager_unavailable',
          'disk_reserve_low', 'operation_timeout', 'rollback_failed', 'release_unavailable'}


def public(job):
    if not job:
        return None
    return {key: getattr(job, key) for key in ('id', 'status', 'phase', 'commit', 'version', 'completed', 'total', 'error_code', 'updated_at')} | {'created_at': job.created_at.isoformat()}


def install(app, require_admin, require_csrf):
    @app.get('/api/admin/upgrades')
    def latest_upgrade(user=Depends(require_admin), db=Depends(get_db)):
        job = db.scalar(select(UpgradeJob).order_by(UpgradeJob.created_at.desc()).limit(1))
        versions = db.scalars(select(Instance.flybot_version).distinct()).all()
        return {'job': public(job), 'versions': sorted(v for v in versions if v)}

    @app.post('/api/admin/upgrades', status_code=202)
    def request_upgrade(user=Depends(require_csrf), db=Depends(get_db)):
        if user.role != 'admin':
            raise HTTPException(403, 'Administrator role required')
        # Every administrator serializes on the same stable row, including retries.
        db.scalar(select(User).where(User.role == 'admin').order_by(User.id).limit(1).with_for_update())
        job = db.scalar(select(UpgradeJob).where(UpgradeJob.status.in_(ACTIVE)).order_by(UpgradeJob.created_at).limit(1))
        if job:
            return {'job': public(job)}
        job = UpgradeJob(id=str(uuid.uuid4()), created_by=user.id, status='queued', phase='queued',
                         completed=0, total=0, updated_at=int(time.time()))
        db.add(job)
        db.add(AuditEvent(actor_id=user.id, action='runtime.upgrade.requested', target=job.id))
        db.commit()
        return {'job': public(job)}

    @app.get('/api/admin/bot-social/upgrades')
    def latest_social_upgrade(user=Depends(require_admin), db=Depends(get_db)):
        job = db.scalar(select(UpgradeJob).where(UpgradeJob.version.like('BotSocial-%')).order_by(UpgradeJob.created_at.desc()).limit(1))
        return {'job': public(job)}

    @app.post('/api/admin/bot-social/upgrades', status_code=202)
    def request_social_upgrade(user=Depends(require_csrf), db=Depends(get_db)):
        if user.role != 'admin':
            raise HTTPException(403, 'Administrator role required')
        db.scalar(select(User).where(User.role == 'admin').order_by(User.id).limit(1).with_for_update())
        job = db.scalar(select(UpgradeJob).where(UpgradeJob.status.in_(ACTIVE), UpgradeJob.version.like('BotSocial-%')).order_by(UpgradeJob.created_at).limit(1))
        if job:
            return {'job': public(job)}
        job = UpgradeJob(id=str(uuid.uuid4()), created_by=user.id, status='queued', phase='queued',
                         version='BotSocial-queued', completed=0, total=1, updated_at=int(time.time()))
        db.add(job)
        db.add(AuditEvent(actor_id=user.id, action='bot_social.upgrade.requested', target=job.id))
        db.commit()
        return {'job': public(job)}


def handle(data, db, now):
    operation = data['operation']
    if operation == 'upgrade_recover':
        rows = db.scalars(select(UpgradeJob).where(UpgradeJob.status == 'running').with_for_update()).all()
        for job in rows:
            job.status = 'failed'; job.phase = 'failed'; job.error_code = 'interrupted'; job.updated_at = now
        return {'recovered': len(rows)}
    if operation == 'upgrade_claim':
        if db.scalar(select(UpgradeJob.id).where(UpgradeJob.status == 'running').limit(1)):
            return {'job': None}
        job = db.scalar(select(UpgradeJob).where(UpgradeJob.status == 'queued').order_by(UpgradeJob.created_at).limit(1).with_for_update(skip_locked=True))
        if job:
            job.status = 'running'; job.phase = 'checking'; job.updated_at = now
        return {'job': public(job)}
    job = db.scalar(select(UpgradeJob).where(UpgradeJob.id == str(uuid.UUID(data['id']))).with_for_update())
    if not job or job.status != 'running':
        return {'accepted': False}
    if operation == 'upgrade_version':
        instance = db.get(Instance, str(uuid.UUID(data['instance'])))
        if instance and job.version:
            instance.flybot_version = job.version
        return {'accepted': instance is not None}
    if operation != 'upgrade_progress':
        raise ValueError('Invalid upgrade operation')
    phase = data.get('phase', job.phase)
    if phase not in PHASES:
        raise ValueError('Invalid phase')
    for key, pattern in [('commit', r'[0-9a-f]{40}'), ('version', r'[A-Za-z0-9._+-]{1,64}')]:
        if key in data:
            if not re.fullmatch(pattern, data[key]):
                raise ValueError('Invalid release')
            setattr(job, key, data[key])
    total = data.get('total', job.total); completed = data.get('completed', job.completed)
    if type(total) is not int or type(completed) is not int or not 0 <= completed <= total <= 16:
        raise ValueError('Invalid progress')
    error = data.get('error_code')
    if error is not None and error not in ERRORS:
        raise ValueError('Invalid error code')
    job.phase = phase; job.total = total; job.completed = completed; job.updated_at = now
    if phase in {'complete', 'current', 'failed'}:
        job.status = 'failed' if phase == 'failed' else 'complete'; job.error_code = error
        db.add(AuditEvent(actor_id=job.created_by, action='runtime.upgrade.'+job.status, target=job.id))
    return {'accepted': True}
