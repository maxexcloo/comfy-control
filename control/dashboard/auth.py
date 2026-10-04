from __future__ import annotations

import hashlib
import hmac
import time
from html import escape

from fastapi import Request

from control.config import ControlSettings

SESSION_COOKIE = "comfy_control_session"
SESSION_SECONDS = 12 * 60 * 60


def bearer_authorised(request: Request, settings: ControlSettings) -> bool:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(
        value.encode(), settings.api_key.encode()
    )


def session_secret(settings: ControlSettings) -> bytes:
    return (settings.ui_password or settings.api_key).encode()


def session_token(settings: ControlSettings, expires: int) -> str:
    payload = str(expires)
    signature = hmac.new(
        session_secret(settings), payload.encode(), hashlib.sha512
    ).hexdigest()
    return f"{payload}.{signature}"


def csrf_token(settings: ControlSettings, expires: int) -> str:
    payload = f"csrf:{expires}"
    signature = hmac.new(
        session_secret(settings), payload.encode(), hashlib.sha512
    ).hexdigest()
    return f"{expires}.{signature}"


def valid_csrf(request: Request, settings: ControlSettings, token: str) -> bool:
    expires, separator, signature = token.partition(".")
    if not separator:
        return False
    try:
        expiry = int(expires)
    except ValueError:
        return False
    expected = csrf_token(settings, expiry).partition(".")[2]
    return (
        ui_authorised(request, settings)
        and expiry >= int(time.time())
        and hmac.compare_digest(signature.encode(), expected.encode())
    )


def ui_authorised(request: Request, settings: ControlSettings) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    payload, separator, signature = token.partition(".")
    if not separator:
        return False
    try:
        expires = int(payload)
    except ValueError:
        return False
    expected = session_token(settings, expires).partition(".")[2]
    return expires >= int(time.time()) and hmac.compare_digest(
        signature.encode(), expected.encode()
    )


def secure_cookie(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").partition(",")[0].strip()
    return request.url.scheme == "https" or forwarded == "https"


def login_html(settings: ControlSettings, invalid: bool = False) -> str:
    message = (
        '<div class="alert" data-variant="destructive" role="alert">'
        "<section>Incorrect username or password.</section></div>"
        if invalid
        else ""
    )
    return f"""<!doctype html>
<html class="dark" lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Comfy Control</title><link href="https://cdn.jsdelivr.net/npm/basecoat-css@1.0.2/dist/basecoat.cdn.min.css" rel="stylesheet"></head>
<body class="grid min-h-screen place-items-center p-4"><main class="card w-full max-w-sm">
<header><h1>Comfy Control</h1></header><section>{message}<form class="grid gap-4" method="post" action="/login">
<label class="field">Username<input class="input" name="username" value="{escape(settings.ui_username)}" autocomplete="username" required></label>
<label class="field">Password<input class="input" name="password" type="password" autocomplete="current-password" required autofocus></label>
<button class="btn w-full" type="submit">Sign In</button></form></section></main></body></html>"""
