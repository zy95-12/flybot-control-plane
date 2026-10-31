from __future__ import annotations

import argparse
import getpass
import uuid

from pydantic import EmailStr, TypeAdapter
from sqlalchemy import select

from .models import User
from .security import hash_password
from .store import SessionLocal, initialize


def main() -> None:
    parser = argparse.ArgumentParser(description="FlyBot Control Plane administration")
    sub = parser.add_subparsers(dest="command", required=True)
    bootstrap = sub.add_parser("bootstrap-admin", help="Create the initial administrator")
    bootstrap.add_argument("email")
    bootstrap.add_argument("--name", default="Platform Admin")
    args = parser.parse_args()

    initialize()
    if args.command == "bootstrap-admin":
        try:
            email = str(TypeAdapter(EmailStr).validate_python(args.email)).lower()
        except Exception as exc:
            raise SystemExit("Enter a valid email address") from exc
        password = getpass.getpass("Admin password (minimum 12 characters): ")
        confirm = getpass.getpass("Repeat password: ")
        if password != confirm:
            raise SystemExit("Passwords do not match")
        try:
            encoded = hash_password(password)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        with SessionLocal() as db:
            if db.scalar(select(User.id).where(User.role == "admin").limit(1)):
                raise SystemExit("An administrator already exists")
            db.add(User(id=str(uuid.uuid4()), email=email, display_name=args.name,
                        password_hash=encoded, role="admin", status="active"))
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise
        print(f"Administrator created: {args.email}")


if __name__ == "__main__":
    main()
