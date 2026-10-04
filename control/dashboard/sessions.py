from __future__ import annotations

import hmac
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from control.dashboard.auth import (
    SESSION_COOKIE,
    SESSION_SECONDS,
    login_html,
    secure_cookie,
    session_token,
    ui_authorised,
)
from control.http import RequestBodyTooLarge, limited_body

LOGIN_MAXIMUM_BYTES = 16 * 1024

router = APIRouter(tags=["dashboard"])


@router.get("/login")
async def login(request: Request) -> Response:
    settings = request.app.state.settings
    if ui_authorised(request, settings):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(login_html(settings), headers={"Cache-Control": "no-store"})


@router.post("/login")
async def create_session(request: Request) -> Response:
    settings = request.app.state.settings
    try:
        await limited_body(request, LOGIN_MAXIMUM_BYTES)
        form = await request.form()
    except RequestBodyTooLarge:
        return HTMLResponse(
            login_html(settings, invalid=True),
            status_code=413,
            headers={"Cache-Control": "no-store"},
        )
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    expected_password = settings.ui_password or settings.api_key
    valid = hmac.compare_digest(
        username.encode(), settings.ui_username.encode()
    ) and hmac.compare_digest(password.encode(), expected_password.encode())
    if not valid:
        return HTMLResponse(
            login_html(settings, invalid=True),
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )
    expires = int(time.time()) + SESSION_SECONDS
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        session_token(settings, expires),
        httponly=True,
        max_age=SESSION_SECONDS,
        path="/",
        samesite="lax",
        secure=secure_cookie(request),
    )
    return response


@router.post("/logout")
async def delete_session() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
