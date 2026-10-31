"""Owner-scoped desired-state API. No Docker access in the web process."""
from __future__ import annotations
import os
import time
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, func, delete
from sqlalchemy.orm import Session
from .models import Instance, RuntimeState, RuntimeLaunch, User, AuditEvent
from .security import digest
from .store import get_db

ENABLED = os.environ.get('RUNTIME_MANAGEMENT_ENABLED', 'false').lower() == 'true'
DOMAIN = os.environ.get('PLATFORM_DOMAIN', 'botawaken.com')
MAX_INSTANCES = min(16, int(os.environ.get('MAX_RUNTIME_INSTANCES', '16')))


def install(app, session_user, require_csrf):
    router = APIRouter(prefix='/api')

    @router.get('/internal/runtime-access', include_in_schema=False)
    def runtime_access(request: Request, db: Session = Depends(get_db)):
        # This endpoint grants no DSH session. It is an additional account-state
        # gate used by Traefik before DSH's own owner-token/cookie authentication.
        # Never accept caller-selected URLs or forward an authentication header.
        host = request.headers.get('X-Forwarded-Host', '')
        try:
            label, domain = host.split('.', 1)
            ident = uuid.UUID(hex=label.removeprefix('u-'))
            if host != f'u-{ident.hex}.{DOMAIN}':
                raise ValueError()
        except (ValueError, AttributeError):
            raise HTTPException(403, 'Instance unavailable')
        instance = db.get(Instance, str(ident))
        state = db.get(RuntimeState, str(ident))
        owner = db.get(User, instance.user_id) if instance else None
        if not ENABLED or not owner or owner.status != 'active' or not state or state.desired != 'running' or instance.status != 'ready' or state.observed_revision != state.revision or state.heartbeat_at < int(time.time()) - 90:
            raise HTTPException(403, 'Instance unavailable')
        return Response(status_code=204, headers={'Cache-Control': 'no-store'})

    def own(db, user, locked=False):
        query = select(Instance).where(Instance.user_id == user.id)
        if locked:
            query = query.with_for_update()
        instance = db.scalar(query)
        state = db.get(RuntimeState, instance.id) if instance else None
        return instance, state

    def summary(instance, state):
        if not instance:
            return None
        return {'id': instance.id, 'status': instance.status, 'desired': state.desired if state else 'stopped',
                'revision': state.revision if state else 0,
                'observed_revision': state.observed_revision if state else 0,
                'error_code': state.error_code if state else None,
                'worker_online': bool(state and state.heartbeat_at > int(time.time()) - 90),
                'version': instance.flybot_version,
                'url': f'https://u-{uuid.UUID(instance.id).hex}.{DOMAIN}/',
                'limits': {'cpus': 2, 'memory_gib': 2, 'storage_gib': 8}}

    def enabled():
        if not ENABLED:
            raise HTTPException(503, 'Runtime management is not enabled yet')

    @router.get('/instance')
    def get_instance(user: User = Depends(session_user), db: Session = Depends(get_db)):
        instance, state = own(db, user)
        return {'enabled': ENABLED, 'instance': summary(instance, state)}

    @router.post('/instance', status_code=202)
    def create_instance(user: User = Depends(require_csrf), db: Session = Depends(get_db)):
        enabled()
        # Serialize capacity checks and concurrent creates without locking user input identifiers.
        db.scalar(select(User).where(User.role == 'admin').order_by(User.id).limit(1).with_for_update())
        instance, state = own(db, user, True)
        if instance:
            return {'instance': summary(instance, state)}
        # The upgrade queue takes the same administrator-row lock above. A new
        # instance cannot slip into the fleet after the upgrade takes its list.
        from .models import UpgradeJob
        if db.scalar(select(UpgradeJob.id).where(UpgradeJob.status.in_(('queued', 'running'))).limit(1)):
            raise HTTPException(409, 'FlyBot is upgrading; please create the instance after it finishes')
        count = db.scalar(select(func.count()).select_from(RuntimeState))
        if count >= MAX_INSTANCES:
            raise HTTPException(409, 'Instance capacity reached; contact the administrator')
        instance = Instance(id=str(uuid.uuid4()), user_id=user.id, status='pending')
        db.add(instance); db.flush()
        state = RuntimeState(instance_id=instance.id, desired='running', revision=1, observed_revision=0, heartbeat_at=0)
        db.add(state)
        db.add(AuditEvent(actor_id=user.id, action='runtime.create.requested', target=instance.id))
        db.commit()
        return {'instance': summary(instance, state)}

    def request_state(instance, state, action, user, db):
        if not instance or not state:
            raise HTTPException(404, 'Instance not found')
        desired = 'running' if action == 'start' else 'stopped'
        if state.desired != desired or instance.status == 'failed':
            state.desired = desired; state.revision += 1; state.error_code = None
            instance.status = 'pending' if desired == 'running' else 'stopping'
            db.execute(delete(RuntimeLaunch).where(RuntimeLaunch.instance_id == instance.id))
            db.add(AuditEvent(actor_id=user.id, action=f'runtime.{action}.requested', target=instance.id))
        db.commit()
        return {'instance': summary(instance, state)}

    @router.post('/instance/{action}', status_code=202)
    def instance_action(action: str, user: User = Depends(require_csrf), db: Session = Depends(get_db)):
        enabled()
        if action not in {'start', 'stop'}:
            raise HTTPException(404, 'Unknown instance action')
        return request_state(*own(db, user, True), action, user, db)

    @router.post('/admin/instances/{instance_id}/{action}', status_code=202)
    def admin_action(instance_id: str, action: str, user: User = Depends(require_csrf), db: Session = Depends(get_db)):
        enabled()
        if user.role != 'admin':
            raise HTTPException(403, 'Administrator role required')
        if action not in {'start', 'stop'}:
            raise HTTPException(404, 'Unknown instance action')
        instance = db.scalar(select(Instance).where(Instance.id == instance_id).with_for_update())
        state = db.get(RuntimeState, instance_id)
        if instance and action == 'start' and db.get(User, instance.user_id).status != 'active':
            raise HTTPException(409, 'Account is suspended')
        return request_state(instance, state, action, user, db)

    # Static route is registered BEFORE /instance/{action} by inserting below at index zero.
    def new_launch(request: Request, user: User = Depends(require_csrf), db: Session = Depends(get_db)):
        enabled()
        instance, state = own(db, user, True)
        if not instance or not state or instance.status != 'ready' or state.desired != 'running' or state.observed_revision != state.revision or state.heartbeat_at < int(time.time()) - 90:
            raise HTTPException(409, 'Instance is not ready')
        db.execute(delete(RuntimeLaunch).where(RuntimeLaunch.user_id == user.id))
        row = RuntimeLaunch(id=str(uuid.uuid4()), instance_id=instance.id, user_id=user.id,
                            session_hash=digest(request.cookies.get('__Host-flybot_session' if os.environ.get('COOKIE_SECURE', 'true').lower() == 'true' else 'flybot_session', '')),
                            revision=state.revision, expires_at=int(time.time()) + 30)
        db.add(row); db.add(AuditEvent(actor_id=user.id, action='runtime.launch.requested', target=instance.id)); db.commit()
        return {'request_id': row.id, 'expires_at': row.expires_at}

    router.add_api_route('/instance/launch', new_launch, methods=['POST'], status_code=202)
    router.routes.insert(0, router.routes.pop())

    @router.post('/instance/launch/{request_id}/consume')
    def consume_launch(request_id: str, request: Request, response: Response, user: User = Depends(require_csrf), db: Session = Depends(get_db)):
        response.headers['Cache-Control'] = 'no-store'
        instance, state = own(db, user, True)
        row = db.scalar(select(RuntimeLaunch).where(RuntimeLaunch.id == request_id, RuntimeLaunch.user_id == user.id).with_for_update())
        session_hash = digest(request.cookies.get('__Host-flybot_session' if os.environ.get('COOKIE_SECURE', 'true').lower() == 'true' else 'flybot_session', ''))
        if not row or row.expires_at <= int(time.time()) or row.session_hash != session_hash:
            raise HTTPException(404, 'Launch request expired')
        if not state or state.revision != row.revision or state.desired != 'running' or instance.status != 'ready':
            raise HTTPException(409, 'Instance is not ready')
        if not row.launch_url:
            response.status_code = 202
            return {'status': 'pending'}
        url = row.launch_url; db.delete(row); db.commit()
        return {'url': url}

    app.include_router(router)
