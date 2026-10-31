#!/usr/bin/env python3
"""Issue independent runtime TLS certificates from an offline platform CA."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile


def openssl(*args: str) -> None:
    subprocess.run(["openssl", *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def create(directory: Path, hostname: str | None = None, ca: Path | None = None) -> None:
    if directory.exists():
        raise ValueError("certificate output already exists; use a new generation directory")
    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=".cert-", dir=directory.parent))
    try:
        key = staging / "key.pem"
        cert = staging / "cert.pem"
        openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(key))
        os.chmod(key, 0o600)
        if ca is None:
            openssl("req", "-new", "-x509", "-key", str(key), "-out", str(cert), "-days", "3650", "-sha256",
                    "-subj", "/CN=FlyBot Runtime CA", "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                    "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        else:
            if not hostname or len(hostname) > 253 or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", hostname):
                raise ValueError("hostname must be a concrete DNS hostname")
            if (ca / "key.pem").stat().st_mode & 0o077:
                raise ValueError("CA private key must have mode 0600 or stricter")
            csr = staging / "request.pem"
            extensions = staging / "extensions.cnf"
            extensions.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:" + hostname + "\n")
            openssl("req", "-new", "-key", str(key), "-out", str(csr), "-subj", f"/CN={hostname}")
            openssl("x509", "-req", "-in", str(csr), "-CA", str(ca / "cert.pem"), "-CAkey", str(ca / "key.pem"),
                    "-set_serial", str(secrets.randbits(158) + 1), "-out", str(cert), "-days", "90", "-sha256", "-extfile", str(extensions))
            csr.unlink(); extensions.unlink()
        os.chmod(cert, 0o644)
        # Reserve the destination exclusively so a concurrent issuer cannot
        # overwrite an existing generation or its private key.
        directory.mkdir(mode=0o700)
        for filename in ("key.pem", "cert.pem"):
            os.replace(staging / filename, directory / filename)
    finally:
        shutil.rmtree(staging)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init-ca")
    init.add_argument("--output", type=Path, required=True)
    issue = subparsers.add_parser("issue")
    issue.add_argument("--ca", type=Path, required=True)
    issue.add_argument("--hostname", required=True)
    issue.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "init-ca":
        create(args.output.resolve())
    else:
        create(args.output.resolve(), args.hostname, args.ca.resolve())
