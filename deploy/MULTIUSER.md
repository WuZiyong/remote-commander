# AIP single-gateway multi-user deployment

Keep AIP as the gateway and device connection manager. The native multi-user portal is an additional service on the same AIP host. The original private Site, bridge credentials, MCP URL, device keys, and four existing devices are preserved.

## Requirements

- Linux gateway, Python 3.11+, OpenSSH server/client, and the existing project dependencies.
- A dedicated HTTPS hostname routed to AIP, with a valid certificate and a TLS reverse proxy on that same gateway. The public hostname is an operator configuration, not supplied by this repository.
- A reachable restricted SSH endpoint for device pairing. On an established AIP installation reuse its verified host, SSH port, public host key, and OS service account. Do not replace its SSH configuration or host keys.
- A private working directory: 700 for state directories, 600 for env files, database files, and backups. Never serve this directory through HTTP.

## Isolated validation first

Install the source into a separate checkout or virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -c constraints.txt -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts setup_device.py
.venv/bin/ruff format --check src tests scripts setup_device.py
.venv/bin/python scripts/stage_multiuser.py --state .runtime/stage-check --port 18890
```

The stage service listens only at `http://127.0.0.1:18890`, uses a fresh registry and account database, and writes only a non-live authorized_keys file. It does not touch the production gateway or pair devices through production SSH. Its supervisor state is under the stage directory. To stop it, validate the PID in `service/supervisor.pid` belongs to this stage supervisor, then send SIGTERM. Do not use broad process-name kills.

The integration suite starts separate real HTTP agents, registers separate owners, and checks files, processes, searches, discovery and revocation through OAuth. An HTTP test endpoint is not a public ChatGPT deployment.

## Existing registry upgrade

1. Record a read-only baseline of all four existing devices through the existing private connector; inspect Git status and preserve any operator modifications.
2. Save a Git recovery ref and private SQLite backup. Upgrade the source only after the isolated tests pass.
3. Explicitly migrate the registry:

```sh
.venv/bin/python -m remote_commander.registry \
  --db .runtime/registry/devices.sqlite migrate
```

This creates a private SQLite backup and adds indexed owner columns. Existing device IDs, tokens, ports, heartbeat and connection IDs remain unchanged and are assigned the original registry owner. Migration is idempotent and transactional.

4. Restart only the legacy bridge after allowing its in-flight deliveries to finish. Existing agents, device connectors, and SSH tunnels need not restart. It is essential that the old bridge imports the upgraded owner-filtered registry before any new users are enabled: the pre-upgrade bridge did not filter owners.
5. Use the original ChatGPT connector to re-check all four devices and execute read-only identity checks. Preserve the old private Site's sharing and authentication settings.

The native portal never assigns an ordinary registration the legacy owner's identity. Original devices stay with the original private connector; they do not appear in a newly registered account.

## Fresh registry, if deploying your own instance

Do this only for a new installation, not an existing owner registry:

```sh
.venv/bin/python -m remote_commander.registry --db .runtime/registry/devices.sqlite init \
  --host ssh.gateway.example --port 2222 --user gateway-user \
  --host-key-file /verified/path/gateway-host-key.pub \
  --authorized-keys /home/gateway-user/.ssh/authorized_keys
```

The host key file must match the actual SSH pairing endpoint, obtained through an independently trusted connection. Invitations contain that public key for pinning. Do not substitute a key from a different SSH daemon.

The existing SSH daemon must honor authorized_keys restrictions. Confirm `restrict`, `command`, `permitlisten`, `permitopen` and loopback-only listeners with a disposable device before accepting untrusted users. Normal administrator keys outside the managed block are retained.

## Native multi-user service

Create a private `.runtime/multiuser-gateway.json`:

```json
{
  "registry_path": "/absolute/path/remote-commander/.runtime/registry/devices.sqlite"
}
```

No static AIP agent route or legacy bridge secrets are needed in this config. Start in the foreground first, replacing the example hostname with your real public hostname:

```sh
.venv/bin/python -m remote_commander.portal \
  --gateway-config .runtime/multiuser-gateway.json \
  --state .runtime/accounts \
  --issuer https://remote.gateway.example \
  --port 8890 --registration-open
```

The service still binds only to 127.0.0.1. Registrations are disabled unless `--registration-open` is supplied. The issuer must remain stable across restarts, OAuth discovery, authorization and token exchange.

After successful public TLS and onboarding verification, install persistent service supervision:

```sh
.venv/bin/python scripts/install_linux_service.py --kind portal \
  --gateway-config .runtime/multiuser-gateway.json \
  --accounts-state .runtime/accounts \
  --issuer https://remote.gateway.example --port 8890 --registration-open
```

This manages an independent PORTAL cron block and `.runtime/portal-service/`. It does not replace SERVICE, BRIDGE, other cron tasks, or device connector services. Existing portal processes must be stopped deliberately before changing their installed command; a single-instance supervisor will not silently adopt a different configuration.

Use [Caddyfile.multiuser](Caddyfile.multiuser) as a dedicated TLS front end. Preserve the external Host header. Forward all OAuth discovery, authorization, token, registration, revocation and portal routes; proxying only /mcp is insufficient. Do not log query strings, cookies, form bodies or Authorization headers. Do not forward untrusted identity headers.

## Acceptance and rollback

Before enabling public registration, verify external HTTPS discovery, DCR, browser login/consent, token refresh/revocation, and the actual ChatGPT tool scan. Then pair two disposable devices under different user accounts and verify cross-owner access fails. Re-check the original four devices through the original private connector.

Keep the private Site private. Never use its publication settings as a shortcut to public multi-user access.

On trouble, stop only the new portal service and leave the original private connector running. The owner migration is additive. To restore a pre-upgrade registry, first stop the portal, revoke all post-upgrade device keys, regenerate the managed authorized_keys block, and stop affected device channels; never restore a stale database while leaving newer keys/tunnels authorized. Restoring code alone without filtering owners is unsafe when new-user rows remain in the database. Back up current state before any rollback.

Account tokens and registry secrets are in separate SQLite databases. Back them up privately with SQLite's backup API, retain the corresponding Git version and issuer, and never commit them. See [SECURITY.md](../SECURITY.md) for operational limits and trust boundaries.
