from __future__ import annotations

import hmac
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import AuditEvent, Instance, Invitation, LoginAttempt, SessionRecord, User, RuntimeState, RuntimeLaunch
from .security import digest, hash_password, random_token, verify_password
from .store import engine, get_db, initialize

COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() == "true"
SESSION_TTL = int(os.environ.get("SESSION_TTL_HOURS", "12")) * 3600
INVITE_TTL = int(os.environ.get("INVITE_TTL_HOURS", "72")) * 3600
LOGIN_WINDOW = int(os.environ.get("LOGIN_WINDOW_SECONDS", "900"))
LOGIN_LIMIT = int(os.environ.get("LOGIN_FAILURE_LIMIT", "8"))
# Host-prefixed production cookies cannot be set by a sibling runtime subdomain.
SESSION_COOKIE = "__Host-flybot_session" if COOKIE_SECURE else "flybot_session"
CSRF_COOKIE = "__Host-flybot_csrf" if COOKIE_SECURE else "flybot_csrf"
DUMMY_PASSWORD_HASH = hash_password("not-a-real-account-password-value")

app = FastAPI(title="FlyBot Control Plane", version="0.2.0")
DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "dashboard"
if DASHBOARD_DIR.exists():
    app.mount("/dashboard", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")
# Same-origin API only. CORS is intentionally not opened to arbitrary origins.
app.add_middleware(CORSMiddleware, allow_origins=[], allow_credentials=True,
                   allow_methods=["GET", "POST", "PATCH", "DELETE"],
                   allow_headers=["Content-Type", "X-CSRF-Token"])


@app.on_event("startup")
def startup() -> None:
    initialize()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
    if COOKIE_SECURE:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


class LoginInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class InviteInput(BaseModel):
    email: EmailStr


class RegisterInput(BaseModel):
    token: str = Field(min_length=30, max_length=200)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=1024)


class UserStatusInput(BaseModel):
    status: str = Field(pattern="^(active|suspended)$")


def public_user(user: User) -> dict:
    return {"id": user.id, "email": user.email, "display_name": user.display_name,
            "role": user.role, "status": user.status, "created_at": user.created_at.isoformat()}


def session_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    record = db.get(SessionRecord, digest(token))
    if not record or record.expires_at <= int(time.time()):
        raise HTTPException(status_code=401, detail="Session expired or account unavailable")
    user = db.get(User, record.user_id)
    if not user or user.status != "active":
        raise HTTPException(status_code=401, detail="Session expired or account unavailable")
    return user


def require_admin(user: User = Depends(session_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator role required")
    return user


def require_csrf(request: Request, user: User = Depends(session_user), db: Session = Depends(get_db)) -> User:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    supplied = request.headers.get("X-CSRF-Token", "")
    session = db.get(SessionRecord, digest(request.cookies.get(SESSION_COOKIE, "")))
    if not cookie or not supplied or not hmac.compare_digest(cookie, supplied):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    if not session or not hmac.compare_digest(digest(cookie), session.csrf_hash):
        raise HTTPException(status_code=403, detail="CSRF token expired")
    return user


def create_session(response: Response, user: User, db: Session) -> str:
    session_token = random_token()
    csrf_token = random_token()
    db.add(SessionRecord(token_hash=digest(session_token), csrf_hash=digest(csrf_token),
                         user_id=user.id, expires_at=int(time.time()) + SESSION_TTL))
    db.commit()
    response.set_cookie(SESSION_COOKIE, session_token, httponly=True, secure=COOKIE_SECURE,
                        samesite="strict", max_age=SESSION_TTL, path="/")
    response.set_cookie(CSRF_COOKIE, csrf_token, httponly=False, secure=COOKIE_SECURE,
                        samesite="strict", max_age=SESSION_TTL, path="/")
    return csrf_token


def audit(db: Session, actor_id: str | None, action: str, target: str | None = None) -> None:
    db.add(AuditEvent(actor_id=actor_id, action=action, target=target))


@app.get("/", include_in_schema=False)
def panel_home():
    return RedirectResponse("/dashboard/login.html", status_code=303)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "control-plane", "mode": "runtime-managed" if os.environ.get("RUNTIME_MANAGEMENT_ENABLED") == "true" else "metadata-only", "database": engine.dialect.name}


def client_key(request: Request, email: str) -> str:
    # Do not trust forwarded headers here; configure the reverse proxy to sanitize them first.
    address = request.client.host if request.client else "unknown"
    return digest(f"{address.lower()}\0{email.casefold()}")


def check_login_rate(db: Session, key: str) -> None:
    now = int(time.time())
    attempt = db.get(LoginAttempt, key)
    if attempt and attempt.window_start + LOGIN_WINDOW > now and attempt.attempts >= LOGIN_LIMIT:
        raise HTTPException(status_code=429, detail="Too many login attempts; try again later")


def record_login_failure(db: Session, key: str) -> None:
    now = int(time.time())
    attempt = db.get(LoginAttempt, key)
    if attempt and attempt.window_start + LOGIN_WINDOW > now:
        attempt.attempts += 1
    else:
        if attempt:
            db.delete(attempt)
        db.add(LoginAttempt(key_hash=key, window_start=now, attempts=1))
    db.commit()


@app.post("/api/auth/login")
def login(data: LoginInput, request: Request, response: Response, db: Session = Depends(get_db)):
    key = client_key(request, str(data.email))
    check_login_rate(db, key)
    user = db.scalar(select(User).where(func.lower(User.email) == str(data.email).lower()))
    valid_password = verify_password(data.password, user.password_hash) if user else verify_password(data.password, DUMMY_PASSWORD_HASH)
    if not user or user.status != "active" or not valid_password:
        record_login_failure(db, key)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    attempt = db.get(LoginAttempt, key)
    if attempt:
        db.delete(attempt)
    csrf_token = create_session(response, user, db)
    return {"user": public_user(user), "csrf_token": csrf_token}


@app.post("/api/auth/register")
def register(data: RegisterInput, response: Response, db: Session = Depends(get_db)):
    now = int(time.time())
    invitation = db.get(Invitation, digest(data.token), with_for_update=True)
    if not invitation or invitation.used_at or invitation.expires_at <= now:
        raise HTTPException(status_code=400, detail="Invitation is invalid or expired")
    user = User(id=str(uuid.uuid4()), email=invitation.email.lower(), display_name=data.display_name.strip(),
                password_hash=hash_password(data.password), role="user", status="active")
    invitation.used_at = datetime.now(timezone.utc)
    db.add(user)
    try:
        # Persist the parent before its audit row: these mappers intentionally
        # have no ORM relationship to order the pending inserts automatically.
        db.flush()
        audit(db, user.id, "account.registered", user.id)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if db.scalar(select(User.id).where(func.lower(User.email) == user.email)):
            raise HTTPException(status_code=409, detail="An account already exists for this email") from exc
        raise HTTPException(status_code=500, detail="Registration could not be completed; please try again") from exc
    csrf_token = create_session(response, user, db)
    return {"user": public_user(user), "csrf_token": csrf_token}


@app.get("/api/auth/session")
def current_session(request: Request, response: Response, user: User = Depends(session_user), db: Session = Depends(get_db)):
    csrf_token = request.cookies.get(CSRF_COOKIE, "")
    session = db.get(SessionRecord, digest(request.cookies.get(SESSION_COOKIE, "")))
    if not csrf_token or not session or not hmac.compare_digest(digest(csrf_token), session.csrf_hash):
        csrf_token = random_token()
        session.csrf_hash = digest(csrf_token)
        db.commit()
        response.set_cookie(CSRF_COOKIE, csrf_token, httponly=False, secure=COOKIE_SECURE,
                            samesite="strict", max_age=SESSION_TTL, path="/")
    return {"user": public_user(user), "csrf_token": csrf_token}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, user: User = Depends(require_csrf), db: Session = Depends(get_db)):
    session = db.get(SessionRecord, digest(request.cookies.get(SESSION_COOKIE, "")))
    if session:
        db.delete(session)
        db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/", secure=COOKIE_SECURE, httponly=True, samesite="strict")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=COOKIE_SECURE, samesite="strict")
    return {"ok": True}


@app.get("/api/admin/users")
def list_users(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(User).order_by(User.created_at.desc()).limit(500)).all()
    return {"users": [public_user(row) for row in rows]}


@app.post("/api/admin/invitations")
def create_invitation(data: InviteInput, user: User = Depends(require_csrf), db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator role required")
    email = str(data.email).lower()
    if db.scalar(select(User.id).where(func.lower(User.email) == email)):
        raise HTTPException(status_code=409, detail="An account already exists for this email")
    token = random_token()
    expires_at = int(time.time()) + INVITE_TTL
    db.add(Invitation(token_hash=digest(token), email=email, created_by=user.id, expires_at=expires_at))
    audit(db, user.id, "invitation.created", email)
    db.commit()
    return {"email": email, "token": token, "expires_at": expires_at}


@app.patch("/api/admin/users/{user_id}/status")
def set_user_status(user_id: str, data: UserStatusInput, user: User = Depends(require_csrf), db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator role required")
    if user_id == user.id and data.status == "suspended":
        raise HTTPException(status_code=400, detail="You cannot suspend your own account")
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if data.status == "suspended" and target.role == "admin" and target.status == "active":
        active_admins = db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.status == "active"))
        if active_admins <= 1:
            raise HTTPException(status_code=400, detail="Cannot suspend the last active administrator")
    target.status = data.status
    if data.status == "suspended":
        db.query(SessionRecord).filter(SessionRecord.user_id == user_id).delete(synchronize_session=False)
        instance = db.scalar(select(Instance).where(Instance.user_id == user_id).with_for_update())
        if instance:
            state = db.get(RuntimeState, instance.id)
            if state:
                state.desired = "stopped"
                state.revision += 1
                instance.status = "stopping"
            db.execute(delete(RuntimeLaunch).where(RuntimeLaunch.instance_id == instance.id))
    audit(db, user.id, f"user.status.{data.status}", user_id)
    db.commit()
    return {"id": user_id, "status": data.status}


@app.get("/api/admin/instances")
def list_instances(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.execute(select(Instance, User.email, User.display_name).join(User, User.id == Instance.user_id).order_by(Instance.updated_at.desc()).limit(500)).all()
    instances = [{**{column.name: getattr(row.Instance, column.name) for column in Instance.__table__.columns},
                  "email": row.email, "display_name": row.display_name} for row in rows]
    for item in instances:
        for key in ("created_at", "updated_at"):
            if item[key]: item[key] = item[key].isoformat()
    return {"instances": instances, "mode": "runtime-managed" if os.environ.get("RUNTIME_MANAGEMENT_ENABLED") == "true" else "metadata-only"}


@app.get("/api/admin/audit")
def list_audit(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.execute(select(AuditEvent, User.email.label("actor_email")).outerjoin(User, User.id == AuditEvent.actor_id).order_by(AuditEvent.id.desc()).limit(200)).all()
    return {"events": [{"id": row.AuditEvent.id, "actor_id": row.AuditEvent.actor_id,
                         "actor_email": row.actor_email, "action": row.AuditEvent.action,
                         "target": row.AuditEvent.target,
                         "created_at": row.AuditEvent.created_at.isoformat()} for row in rows]}


from .runtimes import install as install_runtime_routes
install_runtime_routes(app, session_user, require_csrf)

from .upgrades import install as install_upgrade_routes
install_upgrade_routes(app, require_admin, require_csrf)
