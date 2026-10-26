# Multi-user AIP gateway: implementation and deployment report

Date: 2026-10-26 (Europe/Berlin).

## Result and remaining deployment boundary

The multi-user implementation is committed and the production AIP gateway source and registry have been upgraded. The original private ChatGPT connection continues to operate all four original devices. The old private Site's visibility, endpoint and credentials were not changed.

Native multi-user registration, login, explicit consent, OAuth, pairing, discovery, access isolation and revocation passed automated tests and live acceptance tests.

**Public onboarding is not enabled.** AIP currently has no configured public HTTPS endpoint for this native portal. The isolated portal is loopback-only. A dedicated hostname/TLS ingress and an actual new ChatGPT connection through it still require configuration and acceptance. Do not interpret the successful local OAuth flow as a completed public ChatGPT rollout.

## Commits

- `98358c5`: isolated accounts, OAuth provider/portal, owner-scoped routing, migration, installation and security tests.
- `07049c3`: integrates the existing `78e0694` cloud-result presentation work. This is the production code deployment revision.
- This report is a later documentation-only commit.

Both the functionality and integration revision were pushed to GitHub. Existing unrelated work was retained.

## Automated verification

| Environment | Outcome |
| --- | --- |
| AIP Linux, Python 3.11.13 | 56 passed |
| macOS, Python 3.14.7 | 56 passed |
| Windows, Python 3.12.14 | 54 passed, 2 platform-specific tests skipped |
| Existing cloud relay, Node 24.21.0 | 11 passed |
| Ruff lint and formatting | Passed |
| Cloud build and artifact ESM validation | Passed |
| Git whitespace checks | Passed |

The Python runs emit one upstream Starlette/httpx TestClient deprecation warning. It is not a test failure. The GitHub Actions matrix was retained; this report records actual remote-machine runs, not an unobserved CI result.

Coverage includes all 18 device-bound tools, empty accounts, original-device access denial, separate owners with the same device name, file roots, foreign process/search session IDs, forged identity headers, CSRF, Host/Origin/body limits, password and opaque-token storage, PKCE, code replay, client binding, resource mismatch, refresh rotation, account disabling, OAuth revocation, registration closure, HTTPS cookie flags, and an additive pre-upgrade registry fixture.

Two independent HTTP agents were exercised through the authenticated MCP boundary. Alice could write her own file and start a process; Bob could neither read Alice's file nor access or terminate Alice's process/search sessions. Device removal blocked future routing and left the other user's agent operational.

## Isolated deployment and production-data rehearsal

An independent portal was deployed on AIP loopback port 18890 with a fresh registry, account database, host key and non-live SSH authorization file. Standalone HTTP signup, DCR, consent, token exchange and authenticated MCP calls passed. A newly registered account had no devices; all four original IDs were denied.

A private SQLite copy of the real production registry was migrated separately. Device IDs, ports and token fingerprints matched before/after. All four original devices were then probed successfully through the copied routing configuration. An unrelated user could discover none of them and could not call any of their routes. Production data and SSH authorization were not changed by this rehearsal.

## Production gateway migration

- Saved a private SQLite backup and a Git recovery ref before migration.
- Preserved a private copy of the old Sites hosting manifest before removing instance-specific metadata from version control.
- Added per-device and per-invitation owner columns and indexes transactionally.
- Verified original device IDs, ports and token fingerprints were unchanged.
- Verified agent/bridge env files, agent config, gateway config and SSH authorization were unchanged by migration.
- Restarted only the supervised legacy bridge so it loaded the owner-filtered registry.
- Did not restart any original agent or device connector.
- Left the original private Site and ChatGPT connector settings unchanged.
- Confirmed four original devices online through the current ChatGPT connector after migration and executed read-only identity commands on each.

Private recovery paths and credentials remain only in the gateway's protected runtime state.

## Actual two-user SSH enrollment acceptance

A temporary loopback-only native portal used the migrated registry for a controlled end-to-end test:

1. Created two unrelated local accounts and separate OAuth grants.
2. Each account downloaded its own one-use invitation for a device named `acceptance`.
3. Two temporary macOS profiles generated independent device keys locally and completed actual SSH pairing against the existing AIP endpoint.
4. Each connector started its own restricted agent, opened its allocated reverse tunnel, and appeared automatically only in its own account.
5. Each user wrote and read a file in its own temporary root.
6. Each user's attempt to access the other test device or any original device was rejected.
7. The original private ChatGPT connector still listed exactly its original four devices while both foreign test devices were connected.
8. SSH requests to create an unauthorized reverse listener were refused for both users.
9. SSH direct/local forwarding to the gateway's agent port was refused for both users.
10. Alice's attempt to remove Bob's device was denied; Bob remained connected.
11. Each account removed its own device; discovery became empty and further device access failed.
12. Revoking each OAuth grant caused subsequent token use to return HTTP 401.

Both temporary devices were revoked, their managed connector/agent processes terminated, local temporary keys/profiles/files removed, and the temporary acceptance portal stopped. One-use invitation copies and the acceptance token file were deleted. Revoked IDs and ports remain tombstoned to prevent stale-session reuse. The final private-connector check again showed the original four devices online.

## Modified areas

| Area | Files |
| --- | --- |
| Identity and credentials | `src/remote_commander/accounts.py` |
| SDK OAuth provider | `src/remote_commander/oauth.py` |
| Signup, login, consent, device management, protected MCP | `src/remote_commander/portal.py` |
| Ownership migration, pairing, revocation, channel identity | `src/remote_commander/registry.py` |
| Owner-scoped gateway routing | `src/remote_commander/runtime.py` |
| Shared MCP server authentication configuration | `src/remote_commander/server.py` |
| Cross-platform installation and portal supervision | `setup_device.py`, `scripts/install_linux_service.py` |
| Isolated staging helper | `scripts/stage_multiuser.py` |
| Isolation and real-agent tests | `tests/test_multiuser.py`, `tests/test_tenant_agents.py`, `tests/test_tenant_boundaries.py` |
| Installation, deployment and ChatGPT guides | `README.md`, `deploy/MULTIUSER.md`, `deploy/Caddyfile.multiuser`, `docs/CHATGPT.md` |
| Security and open-source preparation | `SECURITY.md`, `LICENSE`, `.gitignore`, generic cloud hosting manifest/build fallback |
| Legacy compatibility documentation | `docs/LEGACY_SINGLE_OWNER.md`, `deploy/AIP.md`, `deploy/ENROLLMENT.md`, `cloud/README.md` |

## Next acceptance gate

Configure a dedicated, valid HTTPS ingress to the same AIP gateway, preserve the canonical issuer/Host, and proxy the complete portal/OAuth surface. Then perform external discovery, real browser consent and a new ChatGPT tool scan. Enable public onboarding only after that succeeds. The existing private Site must remain private.

The application isolation tests and live SSH probes passed. This is not an independent security certification; the gateway OS administrator remains a trusted operator. See SECURITY.md for current capabilities and operational limits.
