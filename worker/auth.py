from __future__ import annotations

import base64
import hmac

from fastapi import Request, WebSocket

from worker.config import Settings

FORWARDED_API_KEY_HEADER = "x-comfy-control-api-key"


def _valid_header(header: str | None, settings: Settings, allow_basic: bool) -> bool:
    if not header:
        return False
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer":
        return hmac.compare_digest(value.encode(), settings.api_key.encode())
    if allow_basic and scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(value).decode()
        except (ValueError, UnicodeDecodeError):
            return False
        username, _, password = decoded.partition(":")
        return hmac.compare_digest(
            username.encode(), settings.ui_username.encode()
        ) and hmac.compare_digest(password.encode(), settings.ui_password.encode())
    return False


def request_authorised(
    request: Request, settings: Settings, allow_basic: bool = True
) -> bool:
    forwarded = request.headers.get(FORWARDED_API_KEY_HEADER, "")
    return _valid_header(
        request.headers.get("authorization"), settings, allow_basic
    ) or hmac.compare_digest(forwarded.encode(), settings.api_key.encode())


def websocket_authorised(websocket: WebSocket, settings: Settings) -> bool:
    token = websocket.query_params.get("token")
    if token and hmac.compare_digest(token.encode(), settings.api_key.encode()):
        return True
    forwarded = websocket.headers.get(FORWARDED_API_KEY_HEADER, "")
    if hmac.compare_digest(forwarded.encode(), settings.api_key.encode()):
        return True
    return _valid_header(websocket.headers.get("authorization"), settings, True)
