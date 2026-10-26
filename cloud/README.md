# Private compatibility relay only

Do not share or publish an existing owner-private Site to enable multiple users. Use the native AIP portal in [the multi-user guide](../deploy/MULTIUSER.md). The example hosting manifest contains no project identity; create your own private project metadata before deployment.

# Private ChatGPT MCP gateway

This is the exact Worker source of the owner-private Remote Commander Site. The native Sites MCP integration provisions the ChatGPT plugin and manages OAuth, resource discovery and caller identity. It does not require a custom password or a second identity provider.

The Worker only invokes operations for authenticated Site users. Keep the Site **owner-private**; sharing the Site grants its viewers remote tool access. Service tokens alone do not establish a tool caller's identity.

The bridge on an authorized host makes outbound HTTPS requests, claims each queued operation once, routes it to a configured MCP agent, and delivers its result. The current deployment runs on AIP's erebos server. Paired devices connect outward to AIP and are discovered from its owner-bound persistent registry without restarting the bridge. The Mac is optional; shutting it down does not affect comput1. See [enrollment](../deploy/ENROLLMENT.md) and [deployment notes](../deploy/AIP.md). Requests/results in D1 use AES-256-GCM with per-operation authenticated data and expire after five minutes. A lost worker lease is marked uncertain and never automatically repeats a write or command. `get_operation_result` retrieves slow operations without running them again.

## Compact tool results

The ChatGPT entry point returns each result once in MCP `structuredContent`, removing only text blocks that duplicate that exact JSON object. Distinct text, media, errors and all file/process/search data remain intact; clients must read `structuredContent` even when `content` is empty. Agent and gateway APIs are unchanged.

`list_devices` defaults to ID, online status, OS, a distinct display name and any error. Use `detail="full"` for the original full device identities, or `who_am_i` for one device. `get_operation_result` accepts the same detail option and retrieves the same saved result without rerunning the operation. No file body, command output, pagination cursor, exit status, truncation flag or search status is shortened. This reduces returned context, not the model's reasoning costs or subscription usage by a guaranteed percentage.

The base 19 tools are exported from the Python server. The hosted gateway adds `get_operation_result` for a total of 20 tools.

## Secrets

Store `RC_RELAY_TOKEN` and `RC_RELAY_KEY` (base64 encoded 32 bytes) in Sites runtime secrets. Configure the same values and the Site-specific `RC_SITES_BYPASS_TOKEN` in the bridge's private env file, along with each agent's token. Never commit these values.

## Validation

Requires Node 24:

```sh
node --test tests/relay.test.mjs
node scripts/build.mjs
node scripts/validate-artifact.mjs
```

Publish through the Sites source workflow, preserving `.openai/hosting.json` identity and the owner-only audience. D1 migrations are under `drizzle/`. Use the native `get_site(include_mcp_connection=true)` to obtain the actual MCP URL, OAuth resource and plugin ID; do not invent them.
