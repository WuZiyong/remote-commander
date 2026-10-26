import tools from "./tools.json" with { type: "json" };

const encoder = new TextEncoder();
const decoder = new TextDecoder();
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
});
const textResult = (value, isError = false) => ({
  content: [{ type: "text", text: JSON.stringify(value) }], isError,
});
const resultTool = {
  name: "get_operation_result",
  description: "Retrieve a pending remote operation by operation_id. Do not repeat a timed-out write or command.",
  inputSchema: { type: "object", properties: { operation_id: { type: "string" }, detail: { type: "string", enum: ["compact", "full"], default: "compact", description: "Device identity detail." } }, required: ["operation_id"], additionalProperties: false },
  annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
};
const allTools = [...tools, resultTool];

// Structured results carry the data once. Retain any distinct text or media blocks.
export function presentToolResult(result, name, detail = "compact") {
  const original = result.structuredContent;
  if (result.isError || !original || typeof original !== "object" || Array.isArray(original)) return result;
  const serialized = JSON.stringify(original);
  const content = (result.content || []).filter((block) => {
    if (block.type !== "text") return true;
    try { return JSON.stringify(JSON.parse(block.text)) !== serialized; }
    catch { return true; }
  });
  let structuredContent = original;
  if (name === "list_devices" && detail !== "full" && Array.isArray(original.devices)) {
    structuredContent = { ...original, devices: original.devices.map((device) => {
      if (!device || typeof device.id !== "string" || typeof device.online !== "boolean") return device;
      const item = { id: device.id, online: device.online };
      if (device.name && device.name !== device.id) item.name = device.name;
      if (device.system) item.system = device.system;
      if (device.error) item.error = device.error;
      return item;
    }) };
  }
  return { ...result, content, structuredContent };
}

function to64(bytes) {
  let result = "";
  for (let i = 0; i < bytes.length; i += 8192) result += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(result);
}
function from64(value) { return Uint8Array.from(atob(value), (c) => c.charCodeAt(0)); }
async function key(env) {
  return crypto.subtle.importKey("raw", from64(env.RC_RELAY_KEY), "AES-GCM", false, ["encrypt", "decrypt"]);
}
export async function seal(env, value, aad) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const body = await crypto.subtle.encrypt({ name: "AES-GCM", iv, additionalData: encoder.encode(aad) }, await key(env), encoder.encode(JSON.stringify(value)));
  const bytes = new Uint8Array(12 + body.byteLength);
  bytes.set(iv); bytes.set(new Uint8Array(body), 12);
  return to64(bytes);
}
export async function open(env, value, aad) {
  const bytes = from64(value);
  return JSON.parse(decoder.decode(await crypto.subtle.decrypt({ name: "AES-GCM", iv: bytes.subarray(0, 12), additionalData: encoder.encode(aad) }, await key(env), bytes.subarray(12))));
}
async function bridgeAuthorized(request, env) {
  const received = encoder.encode(request.headers.get("Authorization") || "");
  const expected = encoder.encode("Bearer " + env.RC_RELAY_TOKEN);
  const a = new Uint8Array(await crypto.subtle.digest("SHA-256", received));
  const b = new Uint8Array(await crypto.subtle.digest("SHA-256", expected));
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a[i] ^ b[i];
  return difference === 0;
}
async function body(request) {
  const reader = request.body?.getReader();
  if (!reader) return {};
  const chunks = []; let size = 0;
  while (true) {
    const { done, value } = await reader.read(); if (done) break;
    size += value.length;
    if (size > 1800000) { await reader.cancel(); throw new Error("Request body too large"); }
    chunks.push(value);
  }
  const combined = new Uint8Array(size); let at = 0;
  for (const chunk of chunks) { combined.set(chunk, at); at += chunk.length; }
  return JSON.parse(decoder.decode(combined));
}
async function cleanup(env, now) {
  await env.DB.prepare("DELETE FROM relay_jobs WHERE expires < ?").bind(now).run();
  // A claimed operation is never executed again after losing its worker.
  await env.DB.prepare("UPDATE relay_jobs SET status='uncertain' WHERE status='running' AND lease_until < ?").bind(now).run();
}
async function operation(env, id, owner, detail = "compact") {
  const row = await env.DB.prepare("SELECT * FROM relay_jobs WHERE id=? AND owner=? AND expires>?").bind(id, owner, Date.now()).first();
  if (!row) return textResult({ error: "Unknown or expired operation" }, true);
  if (row.status === "done") {
    const result = await open(env, row.response, id + ":response");
    const request = await open(env, row.payload, id + ":request");
    return presentToolResult(result, request.name, detail);
  }
  return textResult({ operation_id: id, state: row.status,
    instruction: row.status === "uncertain" ? "Worker connection was lost. Inspect remote state before retrying any write or command." : "Call get_operation_result with this operation_id; do not repeat this operation." }, row.status === "uncertain");
}
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (!env.DB || !env.RC_RELAY_KEY || !env.RC_RELAY_TOKEN) return json({ error: "Service configuration incomplete" }, 503);
      if (url.pathname.startsWith("/relay/")) {
        if (!await bridgeAuthorized(request, env)) return json({ error: "Unauthorized" }, 401);
        if (request.method !== "POST") return json({ error: "POST required" }, 405);
        const data = await body(request), now = Date.now();
        if (url.pathname === "/relay/poll") {
          await cleanup(env, now);
          await env.DB.prepare("INSERT INTO relay_health (id,last_seen) VALUES ('gateway',?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen").bind(now).run();
          const jobs = [];
          for (let i = 0; i < Math.min(4, Math.max(0, Number(data.capacity) || 0)); i++) {
            const claim = crypto.randomUUID();
            const row = await env.DB.prepare("UPDATE relay_jobs SET status='running',claim=?,lease_until=? WHERE id=(SELECT id FROM relay_jobs WHERE status='queued' AND expires>? ORDER BY created LIMIT 1) RETURNING id,payload").bind(claim, now + 90000, now).first();
            if (!row) break;
            jobs.push({ ...row, claim });
          }
          return json({ jobs });
        }
        if (url.pathname === "/relay/complete") {
          if (typeof data.payload !== "string" || data.payload.length > 1800000) return json({ error: "Invalid payload" }, 400);
          // Same-result delivery retries are idempotent; the remote operation runs once.
          await env.DB.prepare("UPDATE relay_jobs SET response=?,status='done' WHERE id=? AND claim=? AND status IN ('running','done') AND expires>?").bind(data.payload, data.id, data.claim, now).run();
          return json({ accepted: true });
        }
        return json({ error: "Not found" }, 404);
      }
      if (url.pathname === "/mcp") {
        if (request.method !== "POST") return json({ error: "POST required" }, 405);
        const rpc = await body(request);
        const reply = (result) => json({ jsonrpc: "2.0", id: rpc.id, result });
        const error = (code, message) => json({ jsonrpc: "2.0", id: rpc.id ?? null, error: { code, message } });
        if (rpc.jsonrpc !== "2.0") return error(-32600, "Invalid Request");
        if (rpc.method === "server/discover") return reply({ supportedVersions: ["2026-07-28", "2025-11-25", "2025-03-26"], capabilities: { tools: {} }, serverInfo: { name: "Remote Commander", version: "0.2.0" } });
        if (rpc.method === "initialize") return reply({ protocolVersion: ["2025-11-25", "2025-03-26"].includes(rpc.params?.protocolVersion) ? rpc.params.protocolVersion : "2025-11-25", capabilities: { tools: {} }, serverInfo: { name: "Remote Commander", version: "0.2.0" }, instructions: "Private remote file and process tools. Call list_devices, then select deviceId. Retrieve timed-out operations with get_operation_result; never repeat writes blindly." });
        if (rpc.method?.startsWith("notifications/")) return new Response(null, { status: 202 });
        if (rpc.method === "ping") return reply({});
        if (rpc.method === "tools/list") return reply({ tools: allTools });
        if (rpc.method !== "tools/call") return error(-32601, "Method not found");
        // Dispatch supplies this verified identity. Service bypass tokens do not establish user identity.
        const owner = request.headers.get("oai-authenticated-user-id");
        if (!owner) return error(-32001, "Authenticated Site owner identity required");
        const name = rpc.params?.name, args = { ...(rpc.params?.arguments || {}) };
        let detail = "compact";
        if (name === "list_devices" || name === "get_operation_result") {
          detail = args.detail ?? "compact";
          if (detail !== "compact" && detail !== "full") return error(-32602, "detail must be compact or full");
          delete args.detail; // Presentation belongs to this entry point, not the remote agents.
        }
        if (name === "get_operation_result") return reply(await operation(env, args.operation_id, owner, detail));
        if (!tools.some((tool) => tool.name === name)) return error(-32602, "Unknown tool");
        const now = Date.now();
        await cleanup(env, now);
        const health = await env.DB.prepare("SELECT last_seen FROM relay_health WHERE id='gateway'").first();
        if (!health || health.last_seen < now - 45000) return reply(textResult({ error: "Remote bridge is offline; no operation was queued" }, true));
        const id = crypto.randomUUID();
        const payload = await seal(env, { name, arguments: args }, id + ":request");
        const inserted = await env.DB.prepare("INSERT INTO relay_jobs (id,owner,status,payload,created,expires) SELECT ?,?,'queued',?,?,? WHERE (SELECT COUNT(*) FROM relay_jobs WHERE status IN ('queued','running')) < 16").bind(id, owner, payload, now, now + 300000).run();
        if (!inserted.meta.changes) return reply(textResult({ error: "Remote queue full; operation was not queued" }, true));
        for (let i = 0; i < 80; i++) {
          const row = await env.DB.prepare("SELECT status FROM relay_jobs WHERE id=?").bind(id).first();
          if (row?.status === "done" || row?.status === "uncertain") return reply(await operation(env, id, owner, detail));
          await delay(250);
        }
        return reply(await operation(env, id, owner, detail));
      }
      if (url.pathname === "/") return new Response(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Remote Commander</title><style>body{font:18px system-ui;max-width:720px;margin:10vh auto;padding:24px;background:#101c27;color:#e6f0f4}h1{font-size:40px}p{line-height:1.6}code{color:#89dec3}</style><h1>Remote Commander</h1><p>你的私有远程工作入口。</p><p>在 ChatGPT 中启用此网站的 Remote Commander 插件，即可选择设备、操作文件、搜索和管理命令。</p><p>登录与 OAuth 由 ChatGPT Sites 管理。远程机器只建立出站连接；队列中的请求和结果使用 AES-GCM 加密并在 5 分钟后过期。</p><p>先调用 <code>list_devices</code>，再使用 <code>deviceId</code> 选择机器。</p>`, { headers: { "Content-Type": "text/html;charset=utf-8", "Cache-Control": "no-store" } });
      return json({ error: "Not found" }, 404);
    } catch {
      return json({ error: "Invalid request or unavailable service" }, 400);
    }
  },
};
