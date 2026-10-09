import base64
import time
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.websockets import WebSocket

from control.dashboard.auth import (
    bearer_authorised,
    session_token,
    ui_authorised,
    valid_csrf,
)
from control.dashboard.sessions import create_session
from worker.auth import _valid_header, request_authorised, websocket_authorised


def settings():
    return SimpleNamespace(
        api_key="test-key", ui_password="sésame", ui_username="Renée"
    )


def test_worker_accepts_unicode_basic_credentials():
    encoded = base64.b64encode("Renée:sésame".encode()).decode()
    assert _valid_header(f"Basic {encoded}", settings(), allow_basic=True)


@pytest.mark.parametrize("header", [b"authorization", b"x-comfy-control-api-key"])
def test_unicode_bearer_and_forwarded_keys_are_rejected(header):
    value = "Bearer café" if header == b"authorization" else "café"
    request = Request({"type": "http", "headers": [(header, value.encode())]})
    assert not request_authorised(request, settings())
    assert not bearer_authorised(request, settings())


def test_unicode_websocket_token_is_rejected():
    websocket = WebSocket(
        {"query_string": b"token=caf%C3%A9", "type": "websocket", "headers": []},
        receive=None,
        send=None,
    )
    assert not websocket_authorised(websocket, settings())


def test_unicode_session_signature_is_rejected():
    token = f"{int(time.time()) + 60}.é"
    request = Request(
        {
            "type": "http",
            "headers": [(b"cookie", f"comfy_control_session={token}".encode())],
        }
    )
    assert not ui_authorised(request, settings())
    assert not valid_csrf(request, settings(), token)


def test_unicode_csrf_signature_is_rejected_for_valid_session():
    expires = int(time.time()) + 60
    cookie = f"comfy_control_session={session_token(settings(), expires)}"
    request = Request({"type": "http", "headers": [(b"cookie", cookie.encode())]})
    assert ui_authorised(request, settings())
    assert not valid_csrf(request, settings(), f"{expires}.é")


@pytest.mark.asyncio
async def test_login_accepts_unicode_credentials():
    body = b"username=Ren%C3%A9e&password=s%C3%A9same"

    async def receive():
        return {"body": body, "more_body": False, "type": "http.request"}

    request = Request(
        {
            "app": SimpleNamespace(state=SimpleNamespace(settings=settings())),
            "type": "http",
            "scheme": "https",
            "server": ("control", 443),
            "path": "/login",
            "query_string": b"",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        },
        receive=receive,
    )
    response = await create_session(request)
    assert response.status_code == 303
    assert "Secure" in response.headers["set-cookie"]


async def test_media_routes_reject_unicode_bearer_without_exception():
    from control.operations.media import search_media

    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", "Bearer café".encode())],
            "app": SimpleNamespace(
                state=SimpleNamespace(controller=None, settings=settings())
            ),
        }
    )
    assert (await search_media(request)).status_code == 401


async def test_provider_action_rejects_unicode_confirmation_without_exception():
    from control.operations.providers import provider_action

    request = Request(
        {
            "type": "http",
            "headers": [
                (b"authorization", b"Bearer test-key"),
                (b"x-comfy-control-action", "café".encode()),
            ],
            "app": SimpleNamespace(
                state=SimpleNamespace(controller=None, settings=settings())
            ),
        }
    )
    assert (await provider_action("worker", "start", request)).status_code == 400
