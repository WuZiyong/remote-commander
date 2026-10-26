"""MCP SDK OAuth provider: PKCE is validated by the SDK; subjects come from login."""

from __future__ import annotations

import time
from urllib.parse import urlencode

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .accounts import digest


class Provider:
    def __init__(self, accounts, issuer):
        self.accounts, self.issuer = accounts, issuer.rstrip("/")
        self.resource = self.issuer + "/mcp"

    async def get_client(self, client_id):
        data = self.accounts.get("client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info):
        self.accounts.put(
            "client", client_info.client_id, client_info.model_dump(mode="json"), 365 * 86400
        )

    async def authorize(self, client, params):
        if params.resource not in (None, self.resource):
            from mcp.server.auth.provider import AuthorizeError

            raise AuthorizeError("invalid_target", "Unknown MCP resource")
        flow = self.accounts.issue(
            "flow", {"client_id": client.client_id, "params": params.model_dump(mode="json")}, 600
        )
        return self.issuer + "/consent?" + urlencode({"flow": flow})

    def approve(self, flow, owner):
        data = self.accounts.get("flow", flow, consume=True)
        if not data or not self.accounts.active(owner):
            raise PermissionError("Authorization request expired")
        p = data["params"]
        code = self.accounts.issue(
            "code",
            {
                "subject": owner,
                "client_id": data["client_id"],
                "scopes": ["devices"],
                "expires_at": time.time() + 120,
                "code_challenge": p["code_challenge"],
                "redirect_uri": p["redirect_uri"],
                "redirect_uri_provided_explicitly": p["redirect_uri_provided_explicitly"],
                "resource": self.resource,
            },
            120,
        )
        # SDK has already validated this exact registered redirect URI.
        from mcp.server.auth.handlers.authorize import construct_redirect_uri

        return construct_redirect_uri(p["redirect_uri"], code=code, state=p["state"])

    async def load_authorization_code(self, client, authorization_code):
        data = self.accounts.get("code", authorization_code)
        if not data or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, **data)

    def issue_tokens(self, data):
        if not self.accounts.active(data["subject"]):
            raise TokenError("invalid_grant", "Account unavailable")
        value = {k: data[k] for k in ("subject", "client_id", "scopes", "resource")}
        value["family"] = data.get("family", digest(__import__("secrets").token_urlsafe(48)))
        access = self.accounts.issue(
            "access", {**value, "expires_at": int(time.time()) + 3600}, 3600
        )
        refresh = self.accounts.issue(
            "refresh", {**value, "expires_at": int(time.time()) + 2592000}, 2592000
        )
        return OAuthToken(
            access_token=access, refresh_token=refresh, expires_in=3600, scope="devices"
        )

    async def exchange_authorization_code(self, client, authorization_code):
        data = self.accounts.get("code", authorization_code.code, consume=True)
        if not data or data["client_id"] != client.client_id:
            raise TokenError("invalid_grant", "Authorization code unavailable")
        return self.issue_tokens(data)

    async def load_access_token(self, token):
        data = self.accounts.get("access", token)
        if not data or not self.accounts.active(data["subject"]):
            return None
        return AccessToken(token=token, **{k: v for k, v in data.items() if k != "family"})

    async def load_refresh_token(self, client, refresh_token):
        data = self.accounts.get("refresh", refresh_token)
        if not data or data["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, **{k: v for k, v in data.items() if k != "family"})

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        data = self.accounts.get("refresh", refresh_token.token, consume=True)
        if not data or data["client_id"] != client.client_id:
            raise TokenError("invalid_grant", "Refresh token unavailable")
        self.accounts.revoke_family(data["family"])
        return self.issue_tokens(data)

    async def revoke_token(self, token):
        kind = "access" if isinstance(token, AccessToken) else "refresh"
        data = self.accounts.get(kind, token.token)
        if data:
            self.accounts.revoke_family(data["family"])
