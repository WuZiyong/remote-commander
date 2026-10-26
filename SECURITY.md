# Security

## Supported deployment

The multi-user portal uses the official Python MCP SDK for OAuth discovery, authorization-code + S256 PKCE, registered redirect validation, token endpoint client authentication, and protected MCP transport. Remote Commander supplies persistent account state, explicit consent, token issuance, and owner-scoped device routing. It does not advertise CIMD or RFC 9207 issuer-response support; choose DCR.

Passwords use salted scrypt. Session cookies, authorization codes, access tokens, refresh tokens, consent handles, and CSRF tokens are stored by SHA-256 digest, with expiry. Client secrets are retained in the private account database because the SDK needs to retrieve client metadata. Gateway agent tokens must also remain recoverable to contact each device. Protect both SQLite databases and backups as secrets.

HTTPS cookies use the `__Host-` prefix, Secure, HttpOnly, SameSite=Lax, and Path=/ without Domain. State-changing portal forms require a matching expiring CSRF token and same-origin requests. Host, duplicate header, foreign Origin, request body, client-registration, login, and pairing limits are enforced. Proxy access logs are the operator's responsibility: never log query strings or form bodies containing authorization material.

The portal denies caller-supplied identity headers. A user cannot supply an owner ID, agent URL, reverse port, or gateway SSH command. The route owner comes from a validated OAuth subject or authenticated web session. There is no API for registering an ordinary user as the legacy owner.

All device identifiers are globally namespaced for new users. Every device-bound call selects from that subject's current device set before opening any agent connection. Process/search identifiers are meaningful only inside that selected agent. Revoked devices never fall back to static routes.

## SSH device authorization

Invitation keys are short-lived, single-use, and restricted to a fixed enrollment command with no forwarding. Device keys are restricted to a fixed heartbeat command, their own allocated loopback reverse listener, and an unusable local-forward destination. The connector pins the verified gateway host key, never disables host verification, and never forwards an SSH agent.

Removing a device blocks subsequent gateway calls and removes its SSH key. Existing connections are fenced at the next heartbeat. Already accepted remote commands and external batch jobs are not undone by revocation. Ports and IDs remain tombstoned to prevent old tunnels or sessions being attached to replacement devices.

Per-user active-device capacity is 64; the shared port pool is 30000..30999. Tombstones consume ports. Expanding/reusing the pool requires an operator-reviewed procedure; do not delete rows to bypass fencing.

## Trust boundaries

- The AIP gateway operator and AIP OS account are trusted. This is not protection from a malicious gateway administrator.
- Ordinary users receive only restricted device SSH keys, never a shared AIP login or a gateway agent token.
- Keep the original owner-private Site private. Its relay is a single-owner compatibility path, not a multi-user ingress.
- Protect the shared gateway OS account. Do not give shell access on AIP to untrusted tenants.
- File roots are path checks, not an OS sandbox. Shell access grants the agent OS account's full authority. Run an agent in a dedicated account/container when that is the desired boundary.
- Devices are user-owned and should not be registered for multiple tenants. Do not share installation directories, agent tokens, device keys, or pairing files.
- Public deployments need TLS and a trusted reverse proxy. Do not expose agent, registry database, staging portal, or reverse-tunnel listener ports publicly.
- Login/signup throttles use the ASGI peer address and deliberately ignore spoofable forwarded headers. If a proxy aggregates users behind one peer, apply per-client limits at that proxy and size the portal's limits for the deployment.
- Password reset, email verification, MFA, and third-party identity federation are not implemented. Usernames are local gateway identities, not verified email addresses. No claim of independent security certification is made.

## Report a vulnerability

Use the repository's private GitHub security advisory channel when available, or contact its maintainer privately. Do not put live tokens, invitation files, database backups, private endpoints, or device data in a public issue.

## Verification

Run `python -m pytest -q` plus Ruff checks. The isolation tests use separate identities, actual HTTP agents, OAuth token flows, all device tools, cross-device process/search IDs, revocation, and a pre-upgrade registry fixture. The GitHub Actions matrix covers Linux, macOS, and Windows. A passing suite is evidence for the tested cases; it does not replace operational access controls or an independent review.
