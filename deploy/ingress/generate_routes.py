#!/usr/bin/env python3
"""Render a Traefik file-provider route map from trusted runtime inventory JSON."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

INSTANCE_ID = re.compile(r"^[0-9a-f]{32}$")
DOMAIN_LABEL = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def valid_network(network) -> bool:
    return (isinstance(network, ipaddress.IPv4Network) and 16 <= network.prefixlen <= 29
            and any(network.subnet_of(private) for private in PRIVATE_NETWORKS))


def toml_string(value: str) -> str:
    # JSON basic strings use compatible escaping for the ASCII values emitted here.
    return json.dumps(value, ensure_ascii=True)


def render(domain: str, network: ipaddress.IPv4Network, entries: Any, cert_resolver: str | None = None, http_entrypoint: str | None = None) -> str:
    if http_entrypoint is not None and not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", http_entrypoint):
        raise ValueError("HTTP entrypoint must be a simple configuration name")
    if http_entrypoint and cert_resolver:
        raise ValueError("ACME resolver cannot be combined with a plaintext tunnel entrypoint")
    if cert_resolver is not None and not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", cert_resolver):
        raise ValueError("certificate resolver must be a simple configuration name")
    if not valid_network(network):
        raise ValueError("runtime network must be a private IPv4 subnet between /16 and /29")
    domain = domain.lower().rstrip(".")
    if len(domain) > 200 or not DOMAIN_LABEL.fullmatch(domain):
        raise ValueError("domain must be a DNS name without a wildcard")
    if not isinstance(entries, list):
        raise ValueError("runtime inventory must be a JSON array")

    lines = [
        "[http]",
        "  [http.middlewares.flybot-security.headers]",
        '    referrerPolicy = "no-referrer"',
        "    contentTypeNosniff = true",
        "    [http.middlewares.flybot-security.headers.customResponseHeaders]",
        '      X-Frame-Options = "DENY"',
        "",
    ]
    seen_ids: set[str] = set()
    seen_ips: set[ipaddress.IPv4Address] = set()
    for row in entries:
        if not isinstance(row, dict):
            raise ValueError("each runtime entry must be an object")
        instance_id = row.get("instance_id")
        status = row.get("status")
        raw_ip = row.get("ipv4")
        if not isinstance(instance_id, str) or not INSTANCE_ID.fullmatch(instance_id):
            raise ValueError("instance_id must be 32 lowercase hexadecimal characters")
        if instance_id in seen_ids:
            raise ValueError("duplicate instance_id")
        seen_ids.add(instance_id)
        if not isinstance(status, str):
            raise ValueError(f"invalid status for {instance_id}")
        if status not in {"pending", "starting", "ready", "stopped", "suspended", "upgrading", "failed", "deleting"}:
            raise ValueError(f"unknown status for {instance_id}")
        if status != "ready":
            continue
        if not isinstance(raw_ip, str):
            raise ValueError(f"invalid IPv4 address for {instance_id}")
        try:
            ip = ipaddress.IPv4Address(raw_ip)
        except (ipaddress.AddressValueError, TypeError) as exc:
            raise ValueError(f"invalid IPv4 address for {instance_id}") from exc
        if ip not in network or ip in {network.network_address, network.broadcast_address}:
            raise ValueError(f"runtime address {ip} is outside the assigned ingress network")
        if ip in seen_ips:
            raise ValueError(f"duplicate runtime address {ip}")
        seen_ips.add(ip)

        suffix = instance_id
        hostname = f"u-{instance_id}.{domain}"
        service = f"flybot-{suffix}"
        router = f"flybot-{suffix}"
        lines.extend([
            f"[http.routers.{router}]",
            f"  rule = {toml_string(f'Host(`{hostname}`)')}",
            '  entryPoints = [' + toml_string(http_entrypoint or 'websecure') + ']',
            f"  service = {toml_string(service)}",
            "  middlewares = [\"flybot-security\"]",
            *([] if http_entrypoint else ["  [http.routers." + router + ".tls]"]),
            *([f"    certResolver = {toml_string(cert_resolver)}"] if cert_resolver else []),
            "",
            f"[http.services.{service}.loadBalancer]",
            "  passHostHeader = true",
            f"  serversTransport = {toml_string(service)}",
            f"  [[http.services.{service}.loadBalancer.servers]]",
            f"    url = {toml_string(f'https://{ip}:3080')}",
            "",
            f"[http.serversTransports.{service}]",
            f"  serverName = {toml_string(hostname)}",
            '  rootCAs = ["/run/secrets/runtime-ca.pem"]',
            "  insecureSkipVerify = false",
            "",
        ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True, help="base domain, e.g. flybot.example.com")
    parser.add_argument("--network-cidr", required=True, help="dedicated runtime network CIDR")
    parser.add_argument("--inventory", type=Path, required=True, help="trusted runtime inventory JSON")
    parser.add_argument("--output", type=Path, required=True, help="Traefik dynamic config path")
    parser.add_argument("--cert-resolver", help="optional preconfigured ACME certificate resolver")
    parser.add_argument("--http-entrypoint", help="trusted private connector HTTP entrypoint; no router TLS/ACME")
    args = parser.parse_args()
    network = ipaddress.ip_network(args.network_cidr, strict=True)
    if not valid_network(network):
        parser.error("network-cidr must be a private IPv4 subnet between /16 and /29")
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    content = render(args.domain, network, inventory, args.cert_resolver, args.http_entrypoint)
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".routes-", dir=args.output.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, args.output)
        directory_fd = os.open(args.output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


if __name__ == "__main__":
    main()
