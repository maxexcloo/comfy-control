from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from control.media import ControlMedia


@pytest.mark.parametrize("external", [False, True])
async def test_download_resolves_relative_redirects_and_scopes_credentials(
    tmp_path, external
):
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            location = (
                "https://cdn.example.test/output/image.png" if external else "image.png"
            )
            return httpx.Response(302, headers={"location": location})
        assert request.url.path == "/output/image.png"
        assert request.headers.get("authorization") == (
            None if external else "Bearer secret"
        )
        return httpx.Response(
            200, content=b"image", headers={"content-type": "image/png"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        store = Mock()
        runtime = SimpleNamespace(
            client=client, config=SimpleNamespace(api_key="secret")
        )
        controller = SimpleNamespace(
            store=store,
            providers={"worker": runtime},
            worker_url=lambda *_: "https://worker.example.test",
        )
        media = ControlMedia(controller, tmp_path)
        await media.client.aclose()
        media.client = client
        await media.download("history", "worker", "/output/start", "fallback.png")
        assert len(requests) == 2
        assert store.save_media.call_args.args[2] == "image.png"
        assert store.save_media.call_args.args[3].read_bytes() == b"image"


async def test_download_bounds_redirect_loops(tmp_path):
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        if count > 6:
            pytest.fail("redirect loop exceeded its limit")
        return httpx.Response(302, headers={"location": "/loop"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        store = Mock()
        runtime = SimpleNamespace(
            client=client, config=SimpleNamespace(api_key="secret")
        )
        controller = SimpleNamespace(
            store=store,
            providers={"worker": runtime},
            worker_url=lambda *_: "https://worker.example.test",
        )
        media = ControlMedia(controller, tmp_path)
        try:
            with pytest.raises(ValueError, match="redirected too many times"):
                await media.download("history", "worker", "/loop", "fallback.png")
        finally:
            await media.close()
        assert count == 6
        store.save_media.assert_not_called()
