"""Shared AIP registration/consent portal and authenticated multi-user MCP endpoint."""

from __future__ import annotations

import argparse
import hmac
import html
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import anyio
import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from .accounts import Accounts
from .config import GatewayConfig, read_config
from .oauth import Provider
from .runtime import Gateway
from .server import create_http_app, create_server


class TenantRuntime:
    def __init__(self, gateway, accounts):
        self.gateway, self.accounts = gateway, accounts

    async def call(self, name, arguments, device_id=None):
        token = get_access_token()
        if token is None or not token.subject or not self.accounts.active(token.subject):
            raise PermissionError("Authenticated user required")
        return await self.gateway.call_for_owner(token.subject, name, arguments, device_id)

    def close(self):
        self.gateway.close()


class Portal:
    def __init__(self, config, state, issuer, *, registration_open=False):
        self.state = Path(state).resolve()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.accounts = Accounts(self.state / "accounts.sqlite")
        self.gateway = Gateway(config)
        if not self.gateway.registry:
            raise ValueError("Multi-user service requires a migrated registry")
        self.registry = self.gateway.registry
        with self.registry.db() as db:
            if "owner" not in {row[1] for row in db.execute("PRAGMA table_info(devices)")}:
                raise ValueError("Run the explicit registry migrate command before startup")
        self.issuer = issuer.rstrip("/")
        self.secure = self.issuer.startswith("https://")
        self.cookie_prefix = "__Host-" if self.secure else ""
        self.registration_open = registration_open
        self.provider = Provider(self.accounts, self.issuer)

    def session(self, request):
        token = request.cookies.get(self.cookie_prefix + "rc_session", "")
        data = self.accounts.get("session", token)
        return data if data and self.accounts.active(data["owner"]) else None

    def set_cookie(self, response, name, value, seconds):
        response.set_cookie(
            self.cookie_prefix + name,
            value,
            max_age=seconds,
            httponly=True,
            secure=self.secure,
            samesite="lax",
            path="/",
        )

    def page(self, content, csrf=None):
        response = HTMLResponse(
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width"><title>Remote Commander</title>'
            "<style>body{font:17px system-ui;max-width:760px;margin:5vh auto;padding:24px;"
            "color:#162d38;background:#f4f7f8}input,button{font:inherit;padding:10px;margin:6px}"
            "label{display:block}code{overflow-wrap:anywhere}li{margin:16px 0}"
            "a{color:#075b81}button{cursor:pointer}</style></head><body>"
            "<h1>Remote Commander</h1>" + content + "</body></html>"
        )
        if csrf:
            self.set_cookie(response, "rc_form", csrf, 600)
        return response

    def form_token(self):
        return self.accounts.issue("csrf", {}, 600)

    @staticmethod
    def hidden(token):
        return '<input type="hidden" name="csrf" value="' + html.escape(token, quote=True) + '">'

    async def fields(self, request):
        origin = request.headers.get("origin")
        if origin and origin != self.issuer:
            raise PermissionError("Origin denied")
        if (
            request.headers.get("content-type", "").split(";")[0]
            != "application/x-www-form-urlencoded"
        ):
            raise ValueError("Form encoding required")
        raw = (await request.body()).decode()
        values = parse_qs(raw, keep_blank_values=True, max_num_fields=12)
        if any(len(v) != 1 for v in values.values()):
            raise ValueError("Duplicate form fields")
        data = {k: v[0] for k, v in values.items()}
        csrf = request.cookies.get(self.cookie_prefix + "rc_form", "")
        if not csrf or not hmac.compare_digest(csrf, data.get("csrf", "")):
            raise PermissionError("Invalid form token")
        if self.accounts.get("csrf", csrf) is None:
            raise PermissionError("Form expired")
        return data

    def next_path(self, value):
        return value if re.fullmatch(r"/consent\?flow=[A-Za-z0-9_-]{20,100}", value or "") else "/"

    async def auth(self, request):
        mode = request.url.path[1:]
        next_url = self.next_path(request.query_params.get("next"))
        if mode == "signup" and not self.registration_open:
            return self.page("<p>New registrations are disabled by the gateway operator.</p>")
        if request.method == "GET":
            token = self.form_token()
            return self.page(
                "<h2>"
                + mode.title()
                + '</h2><form method="post">'
                + self.hidden(token)
                + '<input type="hidden" name="next" value="'
                + html.escape(next_url, quote=True)
                + '">'
                '<label>Username <input name="username" required maxlength="64" '
                'autocomplete="username"></label>'
                '<label>Password <input name="password" type="password" required minlength="12" '
                'maxlength="1024" autocomplete="'
                + ("new-password" if mode == "signup" else "current-password")
                + '"></label><button>'
                + mode.title()
                + "</button></form>"
                '<p><a href="/login">Login</a> · <a href="/signup">Register</a></p>',
                token,
            )
        data = await self.fields(request)
        ip = request.client.host if request.client else "unknown"
        # Forwarded headers are intentionally not trusted.
        self.accounts.rate("auth:" + ip)
        if mode == "signup":
            self.accounts.rate("register:" + ip, 5, 3600)
            owner = await anyio.to_thread.run_sync(
                lambda: self.accounts.register(data.get("username", ""), data.get("password", ""))
            )
        else:
            owner = await anyio.to_thread.run_sync(
                lambda: self.accounts.login(data.get("username", ""), data.get("password", ""))
            )
        self.accounts.get(
            "session", request.cookies.get(self.cookie_prefix + "rc_session", ""), consume=True
        )
        session = self.accounts.issue("session", {"owner": owner}, 43200)
        response = RedirectResponse(self.next_path(data.get("next")), status_code=303)
        self.set_cookie(response, "rc_session", session, 43200)
        return response

    async def home(self, request):
        session = self.session(request)
        if not session:
            return self.page(
                "<p>Control your own devices through the shared AIP gateway.</p>"
                '<p><a href="/login">Login</a> · <a href="/signup">Create account</a></p>'
            )
        token = self.form_token()
        devices = self.registry.devices(session["owner"])
        items = ""
        for d in devices.values():
            items += (
                "<li><code>"
                + html.escape(d.id)
                + "</code> — "
                + ("connected" if d.online else "offline")
                + '<form method="post" action="/devices/revoke">'
                + self.hidden(token)
                + '<input type="hidden" name="device" value="'
                + html.escape(d.id, quote=True)
                + '"><button>Remove device</button></form></li>'
            )
        return self.page(
            "<h2>Your devices</h2><ul>" + items + "</ul>"
            '<form method="post" action="/devices">'
            + self.hidden(token)
            + '<label>New device name <input name="device" required '
            'pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,47}" maxlength="48"></label>'
            "<button>Download one-time pairing file</button></form>"
            "<p>On the device: clone the repository, then run "
            "<code>python setup_device.py --invite /path/to/pairing-invite.json</code>. "
            "Add <code>--allow-shell</code> only to permit terminal commands.</p>"
            "<h2>Connect ChatGPT</h2><p>MCP URL: <code>"
            + html.escape(self.issuer)
            + "/mcp</code>. Use OAuth and sign in with this account. Authorize only a "
            "client you recognize. Your existing private connector is separate.</p>"
            '<form method="post" action="/disconnect">'
            + self.hidden(token)
            + "<button>Disconnect all OAuth clients</button></form>"
            '<form method="post" action="/logout">'
            + self.hidden(token)
            + "<button>Log out</button></form>",
            token,
        )

    async def device_action(self, request):
        session = self.session(request)
        if not session:
            raise PermissionError("Login required")
        data = await self.fields(request)
        owner = session["owner"]
        if request.url.path == "/devices":
            self.accounts.rate("pair:" + owner, 20, 3600)
            with tempfile.TemporaryDirectory(dir=self.state) as temporary:
                target = Path(temporary) / "invite.json"
                await anyio.to_thread.run_sync(
                    lambda: self.registry.invite(data.get("device", ""), target, owner=owner)
                )
                content = target.read_bytes()
            return HTMLDownload(content)
        if request.url.path == "/devices/revoke":
            await anyio.to_thread.run_sync(
                lambda: self.registry.revoke(data.get("device", ""), owner=owner)
            )
        elif request.url.path == "/disconnect":
            self.accounts.revoke_authorizations(owner)
        else:
            self.accounts.get(
                "session", request.cookies.get(self.cookie_prefix + "rc_session", ""), consume=True
            )
        response = RedirectResponse("/", status_code=303)
        if request.url.path == "/logout":
            response.delete_cookie(self.cookie_prefix + "rc_session", path="/")
        return response

    async def consent(self, request):
        flow = request.query_params.get("flow", "")
        data = self.accounts.get("flow", flow)
        if not data:
            raise PermissionError("Authorization request expired")
        session = self.session(request)
        if not session:
            return RedirectResponse(
                "/login?" + urlencode({"next": "/consent?" + urlencode({"flow": flow})}),
                status_code=303,
            )
        if request.method == "POST":
            form = await self.fields(request)
            if form.get("approve") != "yes":
                raise PermissionError("Authorization not approved")
            return RedirectResponse(self.provider.approve(flow, session["owner"]), status_code=303)
        client = await self.provider.get_client(data["client_id"])
        if not client:
            raise PermissionError("Client unavailable")
        token = self.form_token()
        return self.page(
            "<h2>Authorize client</h2><p><strong>"
            + html.escape(client.client_name or client.client_id)
            + "</strong> requests file and terminal access to your paired devices.</p>"
            "<p>Redirect: <code>"
            + html.escape(data["params"]["redirect_uri"])
            + "</code></p><p>Only devices registered to this account will be accessible.</p>"
            '<form method="post">'
            + self.hidden(token)
            + '<button name="approve" value="yes">Authorize</button></form>'
            '<p><a href="/">Cancel</a></p>',
            token,
        )

    def app(self):
        runtime = TenantRuntime(self.gateway, self.accounts)
        host = urlsplit(self.issuer).netloc
        settings = AuthSettings(
            issuer_url=self.issuer,
            resource_server_url=self.provider.resource,
            validate_token_resource=True,
            required_scopes=["devices"],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=["devices"], default_scopes=["devices"]
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
        mcp = create_server(runtime, [host], auth_server_provider=self.provider, auth=settings)
        app = create_http_app(mcp, runtime)
        # SDK accepts public PKCE clients; advertise that capability as well as secret clients.
        from mcp.server.auth.routes import build_metadata

        metadata = build_metadata(
            settings.issuer_url,
            None,
            settings.client_registration_options,
            settings.revocation_options,
        ).model_dump(mode="json", exclude_none=True)
        for field in (
            "token_endpoint_auth_methods_supported",
            "revocation_endpoint_auth_methods_supported",
        ):
            metadata[field] = ["none", "client_secret_post", "client_secret_basic"]
        app.router.routes.insert(
            0,
            Route(
                "/.well-known/oauth-authorization-server",
                lambda request: JSONResponse(metadata),
                methods=["GET"],
            ),
        )

        async def safe(handler, request):
            try:
                return await handler(request)
            except PermissionError:
                return (
                    self.page('<p>Request denied or expired. <a href="/">Return home</a>.</p>')
                    if request.method == "GET"
                    else JSONResponse({"error": "Request denied"}, 403)
                )
            except (ValueError, KeyError):
                return JSONResponse({"error": "Invalid request"}, 400)

        def route(path, handler, methods):
            async def endpoint(request):
                return await safe(handler, request)

            return Route(path, endpoint, methods=methods)

        app.router.routes.extend(
            [
                route("/", self.home, ["GET"]),
                route("/login", self.auth, ["GET", "POST"]),
                route("/signup", self.auth, ["GET", "POST"]),
                route("/consent", self.consent, ["GET", "POST"]),
                *[
                    route(p, self.device_action, ["POST"])
                    for p in ("/devices", "/devices/revoke", "/logout", "/disconnect")
                ],
                Route("/healthz", lambda request: JSONResponse({"status": "ok"})),
            ]
        )
        return Boundary(app, self)


def HTMLDownload(content):
    from starlette.responses import Response

    return Response(
        content,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="pairing-invite.json"'},
    )


class Boundary:
    """Host/Origin validation, bounded bodies and registration throttling for every route."""

    def __init__(self, app, portal):
        self.app, self.portal = app, portal

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {}
        for k, v in scope["headers"]:
            key = k.decode().lower()
            if key in headers:
                return await JSONResponse({"error": "Duplicate header"}, 400)(scope, receive, send)
            headers[key] = v.decode()
        expected = urlsplit(self.portal.issuer).netloc
        if headers.get("host", "").lower() != expected.lower():
            return await JSONResponse({"error": "Host denied"}, 421)(scope, receive, send)
        if headers.get("origin") and headers["origin"] != self.portal.issuer:
            return await JSONResponse({"error": "Origin denied"}, 403)(scope, receive, send)
        # Never accept user-supplied identity headers, even from an internal reverse proxy.
        if any(
            k.startswith("oai-authenticated-") or k in ("x-user-id", "x-owner-id") for k in headers
        ):
            return await JSONResponse({"error": "Identity header denied"}, 403)(
                scope, receive, send
            )
        if scope["path"] == "/register" and scope["method"] == "POST":
            try:
                ip = scope.get("client", ("unknown",))[0]
                self.portal.accounts.rate("oauth-register:" + ip, 20, 3600)
            except PermissionError:
                return await JSONResponse({"error": "Rate limited"}, 429)(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > (4 * 1024 * 1024 if scope["path"] == "/mcp" else 65536):
                return await JSONResponse({"error": "Body too large"}, 413)(scope, receive, send)
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        form_targets = "'self'"
        if scope["path"] == "/consent":
            query = parse_qs(scope.get("query_string", b"").decode())
            flow = self.portal.accounts.get("flow", query.get("flow", [""])[0])
            if flow:
                target = urlsplit(flow["params"]["redirect_uri"])
                if target.scheme in {"http", "https"} and target.netloc:
                    form_targets += " " + target.scheme + "://" + target.netloc
        csp = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action "
            + form_targets
            + "; frame-ancestors 'none'"
        ).encode()
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (
                            b"content-security-policy",
                            csp,
                        ),
                    ]
                )
            await send(message)

        await self.app(scope, replay, secure_send)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-config", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--port", type=int, default=8890)
    parser.add_argument("--registration-open", action="store_true")
    parser.add_argument(
        "--allow-local-http", action="store_true", help="Testing only: issuer must use 127.0.0.1"
    )
    args = parser.parse_args()
    url = urlsplit(args.issuer)
    if url.path not in ("", "/") or url.query or url.fragment or url.username:
        parser.error("Issuer must be an origin")
    if not (
        url.scheme == "https"
        or (args.allow_local_http and url.scheme == "http" and url.hostname == "127.0.0.1")
    ):
        parser.error("HTTPS issuer required; local HTTP is testing only")
    config = read_config(args.gateway_config, GatewayConfig)
    # Migration is explicit: do not mutate a live registry while merely starting an app.
    portal = Portal(config, args.state, args.issuer, registration_open=args.registration_open)
    uvicorn.run(
        portal.app(),
        host="127.0.0.1",
        port=args.port,
        proxy_headers=False,
        access_log=False,
        limit_concurrency=64,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
