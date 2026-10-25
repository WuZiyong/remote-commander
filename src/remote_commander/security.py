"""ASGI authentication and bounded request bodies for trusted private networks."""

import secrets

from starlette.responses import JSONResponse


class Guard:
    def __init__(self, app, token: str, max_body_bytes: int = 4 * 1024 * 1024):
        if len(token) < 32 or not token.isascii() or any(char.isspace() for char in token):
            raise ValueError("Token must be at least 32 ASCII characters without whitespace")
        self.app, self.token, self.max_body_bytes = app, token, max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        authorization = [value for key, value in headers if key.lower() == b"authorization"]
        if len(authorization) != 1 or not secrets.compare_digest(
            authorization[0], f"Bearer {self.token}".encode()
        ):
            return await JSONResponse({"error": "Unauthorized"}, status_code=401)(
                scope, receive, send
            )
        # Native/CLI MCP clients do not send Origin. Browser access is intentionally denied.
        if any(key.lower() == b"origin" for key, _ in headers):
            return await JSONResponse({"error": "Browser origin denied"}, status_code=403)(
                scope, receive, send
            )
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            data = message.get("body", b"")
            size += len(data)
            if size > self.max_body_bytes:
                return await JSONResponse({"error": "Body too large"}, status_code=413)(
                    scope, receive, send
                )
            chunks.append(data)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
