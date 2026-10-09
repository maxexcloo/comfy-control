import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from worker import vast_gateway


@pytest.mark.asyncio
async def test_vast_worker_checks_backend_readiness(monkeypatch):
    async def health(_):
        return web.json_response({"status": "ready"})

    app = web.Application()
    app.router.add_get("/health/ready", health)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(vast_gateway, "API_KEY", "test-key")
    monkeypatch.setattr(
        vast_gateway, "BACKEND_BASE", str(server.make_url("/")).rstrip("/")
    )

    try:
        result = await vast_gateway.execute(healthcheck=True)
    finally:
        await server.close()

    assert result == {"status": "ready"}


@pytest.mark.asyncio
async def test_vast_worker_executes_and_embeds_outputs(monkeypatch):
    received = {}

    async def execute(request):
        received["authorization"] = request.headers.get("Authorization")
        form = await request.post()
        received["image"] = form["image"].file.read()
        received["spec"] = json.loads(form["spec"])
        return web.json_response(
            {
                "execution_id": "image-1",
                "status": "completed",
                "outputs": [
                    {
                        "content_type": "image/png",
                        "filename": "output.png",
                        "url": "/internal/executions/image-1/outputs/0",
                    }
                ],
            }
        )

    async def output(_):
        return web.Response(body=b"image-output", content_type="image/png")

    app = web.Application()
    app.router.add_post("/internal/executions", execute)
    app.router.add_get("/internal/executions/image-1/outputs/0", output)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(vast_gateway, "API_KEY", "test-key")
    monkeypatch.setattr(
        vast_gateway, "BACKEND_BASE", str(server.make_url("/")).rstrip("/")
    )

    try:
        result = await vast_gateway.execute(
            files=[
                {
                    "content": "aW1hZ2UtaW5wdXQ=",
                    "content_type": "image/png",
                    "field": "image",
                    "filename": "input.png",
                }
            ],
            spec={
                "execution_id": "image-1",
                "model": "flux/text-to-image",
                "operation": "image_generation",
                "parameters": {"prompt": "A wombat"},
            },
        )
    finally:
        await server.close()

    assert received == {
        "authorization": "Bearer test-key",
        "image": b"image-input",
        "spec": {
            "execution_id": "image-1",
            "model": "flux/text-to-image",
            "operation": "image_generation",
            "parameters": {"prompt": "A wombat"},
        },
    }
    assert result == {
        "execution_id": "image-1",
        "outputs": [
            {
                "content": "aW1hZ2Utb3V0cHV0",
                "content_type": "image/png",
                "filename": "output.png",
            }
        ],
    }
