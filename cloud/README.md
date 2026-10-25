# Private ChatGPT MCP gateway

This is the exact Worker source of the owner-private Remote Commander Site. The native Sites MCP integration provisions the ChatGPT plugin and manages OAuth, resource discovery and caller identity. It does not require a custom password or a second identity provider.

The Worker only invokes operations for authenticated Site users. Keep the Site **owner-private**; sharing the Site grants its viewers remote tool access. Service tokens alone do not establish a tool caller's identity.

The bridge on an authorized host makes outbound HTTPS requests, claims each queued operation once, routes it to a configured MCP agent, and delivers its result. The current deployment runs the bridge on the Mac: comput1 receives Cloudflare 403 responses when connecting directly to Sites. The Mac must stay online; comput1's agent starts at boot independently. Requests/results in D1 use AES-256-GCM with per-operation authenticated data and expire after five minutes. A lost worker lease is marked uncertain and never automatically repeats a write or command. `get_operation_result` retrieves slow operations without running them again.

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
