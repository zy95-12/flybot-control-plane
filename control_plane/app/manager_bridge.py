"""Trusted host-worker stdin/stdout protocol, invoked only with docker exec.

There is deliberately no corresponding HTTP endpoint. stdout may carry launch
credentials; the host worker must capture it and never forward it to a log.
"""
from __future__ import annotations
import json
import sys
import time
import uuid
from urllib.parse import urlsplit, parse_qs
from sqlalchemy import select, delete
from .models import Instance, RuntimeState, RuntimeLaunch, User, SessionRecord
from .store import SessionLocal
from .runtimes import DOMAIN


def handle(data):
    now = int(time.time())
    with SessionLocal.begin() as db:
        if data['operation'].startswith('upgrade_'):
            from .upgrades import handle as upgrade_handle
            return upgrade_handle(data, db, now)
        db.execute(delete(RuntimeLaunch).where(RuntimeLaunch.expires_at <= now))
        if data['operation'] == 'snapshot':
            rows = db.execute(select(Instance, RuntimeState, User.status).join(RuntimeState, RuntimeState.instance_id == Instance.id).join(User, User.id == Instance.user_id).order_by(Instance.id)).all()
            return {'instances': [{'id': i.id, 'owner_id': i.user_id, 'desired': s.desired if u == 'active' else 'suspended', 'revision': s.revision} for i, s, u in rows],
                    'launches': [{'id': r.id, 'instance_id': r.instance_id, 'revision': r.revision} for r in db.scalars(select(RuntimeLaunch).where(RuntimeLaunch.launch_url.is_(None), RuntimeLaunch.expires_at > now))]}
        if data['operation'] == 'observe':
            instance = db.scalar(select(Instance).where(Instance.id == data['id']).with_for_update())
            state = db.get(RuntimeState, data['id'])
            if not instance or not state or state.revision != data['revision']:
                return {'accepted': False}
            owner = db.get(User, instance.user_id)
            desired = state.desired if owner.status == 'active' else 'suspended'
            if desired != data['desired']:
                return {'accepted': False}
            status = data['status']
            if status not in {'starting', 'ready', 'stopped', 'suspended', 'failed'}:
                raise ValueError('Invalid observed state')
            if status == 'ready' and desired != 'running':
                raise ValueError('Ready status conflicts with desired state')
            state.heartbeat_at = now; state.observed_revision = state.revision
            state.error_code = data.get('error_code')
            instance.status = status
            if status == 'ready':
                instance.flybot_version = data['version']
            return {'accepted': True}
        if data['operation'] == 'launch':
            row = db.scalar(select(RuntimeLaunch).where(RuntimeLaunch.id == data['request_id']).with_for_update())
            if not row or row.expires_at <= now or row.launch_url:
                return {'accepted': False}
            instance = db.get(Instance, row.instance_id); state = db.get(RuntimeState, row.instance_id)
            owner = db.get(User, row.user_id); session = db.get(SessionRecord, row.session_hash)
            if not owner or owner.status != 'active' or not session or session.expires_at <= now or state.revision != row.revision or state.desired != 'running' or instance.status != 'ready':
                return {'accepted': False}
            url = data['url']; parsed = urlsplit(url)
            expected = 'u-' + uuid.UUID(instance.id).hex + '.' + DOMAIN
            query = parse_qs(parsed.query)
            if parsed.scheme != 'https' or parsed.netloc != expected or parsed.path != '/' or parsed.fragment or set(query) != {'token'} or len(query['token']) != 1 or not 16 <= len(query['token'][0]) <= 1024:
                raise ValueError('Invalid launch URL')
            row.launch_url = url
            return {'accepted': True}
        raise ValueError('Invalid worker operation')


if __name__ == '__main__':
    payload = sys.stdin.buffer.read(65537)
    if len(payload) > 65536:
        raise ValueError('Worker payload too large')
    print(json.dumps(handle(json.loads(payload))))
