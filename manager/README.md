# Isolated runtime manager

The privileged host worker is deployed at `/srv/flybot-platform-botawaken/manager/worker.py` under `flybot-platform-manager.service`. It accepts only the local operator configuration. The control-plane container has no Docker socket, host credential, CA private key, or management HTTP listener.

## User flow

Users sign in at `/dashboard/account.html`, create one instance, start/stop it and request an owner-only launch. Administrators can see and start/stop instances in the console. Suspending an account immediately blocks new HTTP requests at the account-state gate; the worker withdraws its route and stops the process on reconciliation. An existing WebSocket closes when the container stops. Re-enabling an account leaves the instance stopped until an explicit start.

The account-state gate is an additional check before the runtime's DSH token/cookie authentication. It permits only active owners, running desired state, healthy observed state at the current revision, and a worker heartbeat younger than 90 seconds. The gate itself never issues a DSH session. Manager failures close access when that heartbeat expires.

Launch handoffs are tied to the authenticated account and exact platform session, expire after 30 seconds, and are consumed once. The temporary handoff URL is stored in the private control database until consumed/expired; the worker deletes expired rows on polling. It is never returned in instance lists, audit records, worker logs or Docker arguments. DSH's underlying token remains reusable for its process lifetime, and its signed browser sessions persist across restarts. Protect database backups as credentials. User stop/suspension removes access through the route/account gate.

## Runtime boundaries

- Project and per-instance labels must match before every container mutation.
- Immutable image allowlist in the root-owned config; API callers cannot choose images, commands, networks or mounts.
- One `/29` bridge per instance. Initial pool `172.31.128.0/20` is checked against existing networks/routes, and initial admission is capped at 16 instances.
- Only new `fb-*` interfaces receive firewall hooks. Per-instance source rules allow the instance's private ingress on TCP443 and established replies, then deny private, link-local, metadata, host, multicast and reserved destinations. Remaining public IPv4 egress uses Docker's existing NAT. IPv6 is disabled per runtime. No global firewall policy is changed.
- Each runtime has 2 CPUs, 2 GiB RAM, no extra swap, 256 PIDs, read-only image filesystem, dropped capabilities, no-new-privileges and no published ports.
- Data and workspace share a fixed 8 GiB ext4 loop filesystem in the task-owned `flybot-platform-botawaken-runtime-storage` Docker volume. This volume lives on the large Docker data filesystem. Private loop mount records and per-runtime certificate generations remain under the project root. A minimum 64 GiB free-space reserve gates new allocation.
- Stop retains data; delete and version-changing APIs are intentionally absent in this stage. The worker refuses an unexpected image rather than silently replacing a running release.

## Operations

Only these newly created resources are managed:

```
systemctl status flybot-platform-manager.service
journalctl -u flybot-platform-manager.service --no-pager -n 30
```

Do not run global Docker cleanup or change Docker daemon networking. To disable new instance creation, set `RUNTIME_MANAGEMENT_ENABLED=false` for the control plane; the gate then closes runtime access. Stop the dedicated worker and owned runtime containers separately when taking the platform offline. Preserve the database and entire instance filesystems before removing anything.

Runtime restart policy is `no`: after a host/Docker restart the host worker mounts its recorded filesystems, restores scoped firewall rules and only then starts each desired runtime. Do not use `PrivateMounts` for the host worker; Docker must see these mounts. Its systemd unit caps the worker at 1 CPU / 256 MiB and captures only sanitized operational messages.

## Still required

Runtime upgrade, explicit data/image restore, automatic failure rollback and private-leaf renewal have now been exercised on a dedicated fixture. PostgreSQL and Social recovery passed against isolated backup copies. Automatic live shared-service rollout/restore, off-host replication/retention and reboot recovery validation remain separate work. See `../docs/maintenance-validation-2026-10-20.md`.

## Snapshots and pinned upgrades

`operations.py` adds operator commands with a separate process lock. Copy it beside `worker.py` on the host. Run operations in a bounded transient unit; replace `INSTANCE_UUID`, `IMAGE_DIGEST`, `RELEASE_NAME` and `BACKUP_DIRECTORY` with the actual recorded values:

```sh
systemd-run --wait --pipe --collect --unit=flybot-platform-operation \
  --property=CPUQuota=200% --property=MemoryMax=1G \
  /usr/bin/python3 /srv/flybot-platform-botawaken/manager/operations.py backup INSTANCE_UUID
```

Other actions use the same bounded wrapper:

```
backup-shared
upgrade INSTANCE_UUID --image sha256:IMAGE_DIGEST --version RELEASE_NAME
restore INSTANCE_UUID BACKUP_DIRECTORY
```

An instance snapshot withdraws its route, stops only that instance, unmounts its filesystem and copies the complete disk image (sparse/reflink when supported). The manifest includes the exact image ID, release, instance identity, size and SHA256. The worker resumes and remounts/restarts the original instance afterward. Backup data resides in the separately labelled project backup volume on the large Docker filesystem, with root-only access.

An upgrade snapshots first, replaces only the selected instance container and updates its root-owned image/version record. It requires a locally imported immutable ARM64 image digest; no pull or floating tag is used. The worker recreates that instance with the new image and existing storage. If readiness is absent for 180 seconds, operations restores the complete pre-upgrade filesystem and image record, then restarts reconciliation. The command now waits for healthy recovery before reporting a completed rollback. Upgrade one instance first, review its operational behavior, then apply the same digest to others. The 2026-10-20 drill exercised a label-only compatible image, full disk/image restore and a deliberately failing image; it did not deploy a newer upstream release.

`backup-shared` writes a PostgreSQL logical dump and a consistent Bot Social archive while briefly stopping only the platform's Social container. It records both service image IDs and file checksums. Shared service upgrades still use the pinned Compose environment and an operator-controlled maintenance window; automated shared-service restoration/rollout is not implemented. Keep the previous environment/image pins and backup together, and never run a schema downgrade without restoring its matching data.

Snapshots currently remain on the same physical host. Off-host encrypted backup replication, retention policy and scheduled execution are pending; no scheduler was added without a request.


## Automatic private certificate renewal

`flybot-platform-certificates.timer` runs the dedicated oneshot service daily at **04:10–04:30 Asia/Shanghai**. It renews 90-day leaves when less than 30 days remain, verifies the CA/hostname/key/validity, retains previous material and records a persistent activation journal. Failure retries after 30 minutes. The service has CPU/memory limits and checks Docker is already active instead of starting the shared daemon.

User runtimes and the canary load replacement leaves with a scoped stop/start, preserving file-bind inodes and persistent data. Only due runtimes restart. The worker withdraws a runtime's route while a renewal journal exists. An interrupted or failed activation restores the old pair; verified TLS and runtime health are required before journal removal. TLS probes run inside the owned runtime namespace to preserve the tenant firewall boundary.

Social TLS uses immutable complete pairs inside the mounted dynamic configuration directory and one atomic configuration swap; ingress is not restarted. The original Compose certificate bind sources are mirrored after activation to preserve recreation compatibility. Public HTTPS certificates continue to be managed by Cloudflare.

```
systemctl status flybot-platform-certificates.timer
systemctl status flybot-platform-certificates.service
journalctl -u flybot-platform-certificates.service --no-pager -n 30
```

Last check: `/srv/flybot-platform-botawaken/certificate-renewal-status.json`. Pending activation journals: `renewal-state/`; old recovery material: `certificate-renewals/` under the same project root. Do not delete a pending journal or its recovery material to bypass a failed activation. The CA itself is not automatically replaced; the service signals an operator error when its remaining validity falls below one year.
