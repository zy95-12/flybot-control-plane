#!/usr/bin/env python3
"""Render separate internal HTTPS and trusted-connector HTTP routes (Python 3.11+)."""
import argparse
import copy
import json
from pathlib import Path
import tomllib


def render(source: Path) -> dict:
    data = tomllib.loads(source.read_text())
    routers = data['http']['routers']
    for name, router in list(routers.items()):
        router['entryPoints'] = ['websecure']
        router['tls'] = {}
        clone = copy.deepcopy(router)
        clone.pop('tls')
        clone['entryPoints'] = ['webtunnel']
        if name == 'control-auth':
            clone['middlewares'] = ['auth-tunnel-rate-limit']
        routers[name + '-tunnel'] = clone
    middleware = copy.deepcopy(data['http']['middlewares']['auth-rate-limit'])
    middleware['rateLimit']['sourceCriterion'] = {'requestHeaderName': 'CF-Connecting-IP'}
    data['http']['middlewares']['auth-tunnel-rate-limit'] = middleware
    data.setdefault('tls', {})['certificates'] = [{
        'certFile': '/run/secrets/social-cert.pem',
        'keyFile': '/run/secrets/social-key.pem',
    }]
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # Caller stages this file in a fresh directory before mounting into ingress.
    args.output.write_text(json.dumps(render(args.source), indent=2) + '\n')
