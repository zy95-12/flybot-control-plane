"""Run the existing panel/API on loopback with a disposable private SQLite database.

No manager, Docker, production environment file or saved credentials are loaded.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'control_plane'))


def check(base):
    import httpx

    with httpx.Client(base_url=base, timeout=10) as admin:
        assert admin.get('/api/health').status_code == 200
        assert admin.get('/dashboard/console.html').status_code == 200
        login = admin.post('/api/auth/login', json={
            'email': 'admin@example.com', 'password': os.environ['FLYBOT_DEMO_ADMIN_PASSWORD']})
        assert login.status_code == 200
        headers = {'X-CSRF-Token': login.json()['csrf_token']}
        identities = []
        for name in ('Alice', 'Bob'):
            invite = admin.post('/api/admin/invitations', headers=headers,
                                json={'email': f'{name.lower()}@example.com'})
            assert invite.status_code == 200
            with httpx.Client(base_url=base, timeout=10) as member:
                registered = member.post('/api/auth/register', json={
                    'token': invite.json()['token'], 'display_name': name,
                    'password': secrets.token_urlsafe(24)})
                assert registered.status_code == 200
                session = member.get('/api/auth/session')
                assert session.status_code == 200
                identities.append(session.json()['user']['id'])
                assert member.get('/api/admin/users').status_code == 403
                assert member.get('/api/instance').json()['enabled'] is False
        assert len(set(identities)) == 2
        assert admin.post('/api/admin/invitations', json={
            'email': 'no-csrf@example.com'}).status_code == 403
    with httpx.Client(base_url=base, timeout=10) as anonymous:
        assert anonymous.get('/api/admin/users').status_code == 401
    return {'panel_http': 'pass', 'invite_register_two_users': 'pass',
            'distinct_sessions': 'pass', 'admin_and_csrf_gates': 'pass',
            'runtime_manager': 'disabled', 'shared_project': 'not implemented (M1/M2)'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Run real HTTP checks and exit')
    parser.add_argument('--port', type=int, default=18088, help='Loopback port; 0 allocates a free port')
    args = parser.parse_args()
    password = secrets.token_urlsafe(32) if args.check else (
        os.environ.get('FLYBOT_DEMO_ADMIN_PASSWORD') or getpass.getpass('Temporary admin password (12+ characters): '))
    if len(password) < 12:
        parser.error('Admin password needs at least 12 characters')
    with tempfile.TemporaryDirectory(prefix='flybot-control-demo-') as directory:
        # Override inherited production settings before importing any app module.
        os.environ.update(APP_SECRET=secrets.token_urlsafe(48),
                          DATABASE_URL='sqlite:///' + str(Path(directory) / 'control.sqlite3'),
                          AUTO_CREATE_SCHEMA='true', COOKIE_SECURE='false',
                          RUNTIME_MANAGEMENT_ENABLED='false', PLATFORM_DOMAIN='demo.invalid',
                          FLYBOT_DEMO_ADMIN_PASSWORD=password)
        from app.main import app
        from app.models import User
        from app.security import hash_password
        from app.store import SessionLocal, initialize, engine
        import uvicorn

        initialize()
        with SessionLocal() as db:
            db.add(User(id=str(uuid.uuid4()), email='admin@example.com', display_name='Demo Admin',
                        password_hash=hash_password(password), role='admin', status='active'))
            db.commit()
        sock = socket.socket()
        sock.bind(('127.0.0.1', args.port))
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError('Demo API failed to start')
                time.sleep(0.05)
            base = f'http://127.0.0.1:{sock.getsockname()[1]}'
            if args.check:
                print(json.dumps(check(base), indent=2))
            else:
                print(f'Panel: {base}/dashboard/console.html\nAccount: admin@example.com\nTemporary data is deleted on exit.', flush=True)
                while thread.is_alive():
                    thread.join(0.5)
        except KeyboardInterrupt:
            pass  # Ctrl-C stops only this disposable server.
        finally:
            server.should_exit = True
            thread.join(10)
            sock.close()
            engine.dispose()


if __name__ == '__main__':
    main()
