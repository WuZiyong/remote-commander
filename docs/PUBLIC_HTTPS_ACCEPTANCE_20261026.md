# Public HTTPS and ChatGPT acceptance — 2026-10-26

This report extends [the earlier isolation report](TEST_REPORT_20261026.md). The service now has a real HTTPS endpoint and has completed a live ChatGPT OAuth connection and a real device file read.

## User entry points

- Portal: https://remote.wuzy55.de/
- Create an independent account: https://remote.wuzy55.de/signup
- ChatGPT custom MCP server: https://remote.wuzy55.de/mcp

Public self-registration was enabled after the operator explicitly approved internet-wide registration. It is included in the persistent startup configuration. Each new account starts with no devices and must pair its own computer. The operator's original devices and private connector are separate.

Users choose their own credentials in the portal. Do not reuse the temporary acceptance accounts; both accounts are disabled and their OAuth grants and devices have been revoked.

## Deployed ingress

Cloudflare manages DNS for wuzy55.de. A dedicated Cloudflare Tunnel on AIP routes only remote.wuzy55.de to the multi-user portal at 127.0.0.1:8890. Other tunnel hostnames return 404. The origin remains bound to loopback.

The public portal has a separate accounts database and a gateway configuration containing only the production device-registry path. It does not publish the private static gateway configuration or inherit the legacy owner's identity.

The public portal and tunnel each have their own single-instance supervisor, boot entry, and five-minute supervisor recovery entry. The original agent and private bridge startup blocks were preserved. Tunnel credentials and account state remain private and are excluded from Git.

Cloudflare Tunnel uses the existing AIP machine; no replacement server was introduced. The zone uses Cloudflare's Free plan. This report makes no promise about third-party future pricing or availability.

## Completed acceptance

| Check | Result |
| --- | --- |
| Public TLS and discovery/security checks | 8 passed: valid HTTPS, issuer/discovery, protected MCP, closed registration during initial acceptance, identity forgery, cross-origin rejection, secure cookies |
| Public OAuth with two independent accounts | 11 passed: login, DCR, explicit consent, S256 PKCE, code replay prevention, tool discovery, empty initial device lists, original-device denial, refresh rotation and revocation |
| Tools discovered | 19 tools, including list_devices |
| Two live SSH-paired restricted agents | 10 passed: each account discovers only its own online device; own identity/read/write work; foreign device and out-of-root reads fail; shell remains disabled; all four original device routes remain denied |
| Real ChatGPT connection | OAuth/DCR, consent, tool scan and installation completed against the public endpoint |
| Real ChatGPT device call | First list_devices returned zero devices. After pairing, it returned only account A's online device and read a randomly generated proof file with exact content verified independently |
| Revocation and account cleanup | 7 passed: foreign-device removal rejected, both own devices removed through the public portal, all account grants revoked, previous tokens return 401, both temporary accounts disabled |
| Test-machine cleanup | Both temporary connectors and agents stopped, dedicated ports free, temporary profiles, SSH keys, invitations and test files removed |
| Final public registration check | Signup form available over valid public HTTPS; invalid form input and cross-origin submission still rejected; persistent registration flag verified |
| Original devices | AIP, Mac, comput1 and Windows remained online through the private connector |

The test devices used dedicated local ports and file roots with terminal commands disabled. They were not installed as persistent services and did not replace any existing device.

Local private deployment reports retain non-secret check results. Passwords, bearer tokens, client credentials, pairing files, device keys, database contents and Cloudflare credentials are not included in this report or committed.

## Browser issue fixed during acceptance

The portal previously sent Referrer-Policy: no-referrer on all responses. A real browser form submission during ChatGPT OAuth then carried Origin: null and was correctly rejected by the existing origin checks.

The portal now sends Referrer-Policy: same-origin. This preserves the browser's same-origin form behavior while suppressing referrers on cross-origin OAuth redirects. The server still rejects missing, null and foreign origins where required; no origin-validation bypass was added.

Validation after the fix: 56 Python tests passed on AIP and 56 passed on Mac, Ruff check and formatting passed, and the actual ChatGPT browser login and consent flow succeeded.

## Operations and recovery

The AIP deployment state is under .runtime/public-portal-20261026 and the dedicated tunnel state is under .runtime/cloudflare. These directories contain credentials and must remain private.

To close new registrations, remove --registration-open from both entries in the REMOTE_COMMANDER_PUBLIC_PORTAL startup block and restart only that portal supervisor with the same issuer, state, configuration and port. Closing registration does not by itself revoke existing accounts.

To stop public ingress, stop only the dedicated tunnel supervisor and disable its REMOTE_COMMANDER_CF_TUNNEL startup block. Do not stop or modify the original REMOTE_COMMANDER_SERVICE or REMOTE_COMMANDER_BRIDGE blocks. Existing private device access uses a separate connection.

For normal account/device cleanup, use Remove device and Disconnect all OAuth clients in the portal. Previously started OS jobs are not killed by device revocation.

See [ChatGPT setup](CHATGPT.md), [gateway deployment](../deploy/MULTIUSER.md), and [the security model](../SECURITY.md).
