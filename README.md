# FlyBot Control Plane

The FlyBot platform control plane: the admin/user **panel** (web console), its FastAPI backend,
and the privileged host **manager** that provisions per-user FlyBot runtimes, renews certificates
and performs one-click upgrades.

## Layout
- `control_plane/` — FastAPI app (`app/`: auth, invites, runtimes, upgrades, manager bridge), Alembic migrations, `requirements.txt`.
- `dashboard/` — static panel UI (login/register/account/console).
- `manager/` — host-side worker + systemd units: `worker.py` (runtime provisioning), `operations.py` (backup/restore/ops), `upgrade_runner.py` / `upgrade_build.py` (upgrade pipeline), `bot_social_upgrade.py`, `renew_certificates.py`, `bridge_client.py`, `config.example.json`.
- `deploy/` — Docker Compose (`compose.public.yaml`, `compose.tunnel.yaml`), Traefik ingress route generators, runtime certificate helper, and an example site definition under `deploy/sites/`.

## Configuration
All secrets and host-specific values come from environment variables / files that are **not** in this repo.
Copy `.env.example` to `.env` and fill it in; copy `manager/config.example.json` to `manager/config.json`.
Key variables: `APP_SECRET`, `DATABASE_URL`, `POSTGRES_PASSWORD`, `PLATFORM_DOMAIN`, `COOKIE_SECURE`,
TLS cert/key paths, image tags, `FLYBOT_HOST_PUBLIC_IP`, `FLYBOT_DNS_UPSTREAMS`, `GITHUB_TOKEN` (optional, for upgrade source fetches).

The manager scripts assume the project root `/srv/flybot-platform-botawaken`; adjust paths in the systemd units and config for your host.

Not included: instance data, databases, backups, certificates, secrets, upgrade job records, build artifacts.

For a clean, isolated local panel/API with two-user HTTP checks, see [dev/README.md](dev/README.md).
