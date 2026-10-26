# Connect your own ChatGPT account

The gateway operator must first deploy a publicly reachable HTTPS portal/MCP endpoint. A loopback staging URL, GitHub repository URL, or someone else's private Site is not a shared gateway endpoint.

The operator's deployment is available at [remote.wuzy55.de](https://remote.wuzy55.de/), with [self-registration](https://remote.wuzy55.de/signup) and MCP URL `https://remote.wuzy55.de/mcp`. [Live HTTPS and ChatGPT acceptance](PUBLIC_HTTPS_ACCEPTANCE_20261026.md) was completed on 2026-10-26. Create your own account and pair your own device before authorizing ChatGPT.

1. Register and sign in to your own Remote Commander portal account.
2. Pair your own computer and verify it appears in that portal.
3. In ChatGPT's custom MCP/plugin management, add the operator's HTTPS URL ending in `/mcp`.
4. Choose OAuth and **dynamic client registration (DCR)**. This server does not advertise CIMD. Use the exact redirect URI generated for the connection; do not substitute a callback from another connection.
5. Complete the gateway login and explicit client-consent screen. Use your own gateway credentials, not the gateway operator's credentials.
6. Finish the tool scan and installation. Start with `list_devices`, then select your deviceId.
7. Test a read-only call such as `who_am_i`, followed by a file operation only in a directory you authorized.

UI names, plan availability, and workspace permissions can vary. Current official instructions describe adding a custom MCP server from ChatGPT Plugins and installing the resulting plugin. A successful SDK OAuth test does not prove the external URL has completed a real ChatGPT connection; the operator must perform that acceptance step once HTTPS ingress is configured.

The service publishes protected-resource metadata, authorization-server metadata, S256 PKCE, DCR, registered redirect validation, resource-bound tokens, refresh tokens and revocation. It supports public PKCE clients and the SDK's client-secret authentication methods. It does not request email/profile scopes or assert verified email addresses.

A new account has an empty device list. The original operator's Mac, AIP, comput1, Windows and private connector are not inherited. Each device is routed from the authenticated OAuth subject, not a device list supplied by ChatGPT.

To disconnect, remove the ChatGPT connection and use **Disconnect all OAuth clients** in the portal. To remove a device, use **Remove device**; already-running external jobs are not killed.

## Troubleshooting

- **Cannot reach the server:** verify public HTTPS, certificate, Host and all discovery/OAuth routes. localhost cannot be reached by a remote ChatGPT connection.
- **Invalid client:** keep the DCR registration stable and use the authentication mode the registration returned.
- **Authorization error:** retry the same connection's login flow with its generated redirect URI. Do not reuse another client's authorization code.
- **Empty device list:** make sure the computer was paired under this gateway account. Existing private-owner devices intentionally remain in that owner's separate connection.
- **Offline device:** confirm the agent and connector are running, SSH can reach the gateway, and the pinned host key matches. Never disable StrictHostKeyChecking to work around a mismatch.
- **Forbidden file/command:** check the local roots, read-only setting, and whether shell was explicitly enabled when installing that device.

Official references (checked 2026-10-26):

- [Authentication for ChatGPT plugins](https://developers.openai.com/plugins/build/auth)
- [Connect an MCP server in ChatGPT](https://developers.openai.com/api/docs/mcp)
