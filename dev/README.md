# Isolated control-plane development

Reuse the current panel, FastAPI routes, authentication and invitation model:

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r dev/requirements.txt
.venv/bin/python dev/local_demo.py --check --port 0
.venv/bin/python dev/local_demo.py --port 18088
```

The interactive command asks for a temporary admin password. Open the displayed
loopback panel URL and log in as `admin@example.com`. All data is disposable and
deleted on exit; run in the foreground and stop with Ctrl-C. It uses an isolated
SQLite database and generated application secret, overriding inherited production
database, cookie, domain and manager settings before the app is imported. It never
loads `.env`, manager config, production data, or Docker. HTTP cookies are allowed
only for this loopback fixture. Do not use this launcher for public hosting.

`--check` starts a real HTTP server on a free port, logs in, invites/registers Alice
and Bob, verifies independent identities and admin/CSRF denials, then shuts down.
Passwords, invitation tokens and cookies are never printed. The runtime manager
stays disabled; this check does not provision shared project instances or link
platform users to FlyBot members. Those remain M1/M2 work in FlyBot issue #2's
roadmap.

## Upgrade source

Host operators can set `FLYBOT_SOURCE_REPOSITORY` (default `zy95-12/FlyBot`) and
`FLYBOT_SOURCE_REF` (default `main`) in the upgrader service environment. These are
trusted operator configuration, never HTTP request parameters. The ref resolves to
an immutable commit; all source download paths use that same repository. Build
provenance includes repository, ref and commit. Optional token discovery also
accepts `FLYBOT_PLATFORM_ROOT`; explicit `FLYBOT_GITHUB_TOKEN_FILE` takes precedence.
No environment-file loader is added: configure systemd explicitly before using it.

```sh
.venv/bin/python -m unittest discover -s dev -p 'test_*.py' -v
```

## Existing deployment limitations

The privileged manager is not a portable clean-install demo launcher: it enforces
the original project label, root, domain, resource bounds and firewall ownership.
`upgrade_build.py` also expects prior release/runtime overlay templates that are
not in this repository (`build-20261020/runtime` and `release-20261020`). The local
demo deliberately does not start those components or weaken their ownership guards.
Deploying shared project runtimes still needs an explicit provisioning contract,
portable build templates and M2 authorization. Changing upgrade source alone does
not make the existing host upgrader deployable on a new host.

The FlyBot-side `npm run demo:check` exercises the real DSH agent separately; see
the sibling FlyBot checkout's `dev/demo/README.md`. Current panel/backend services
are the reuse boundary; the FlyBot task sidebar and Project board may be rebuilt.
