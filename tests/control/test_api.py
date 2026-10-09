import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from worker.app import create_app as create_worker
from worker.comfy import OutputRef
from worker.config import Settings

ROOT = Path(__file__).parents[2]


def settings(deployment_type: str = "serverless") -> Settings:
    return Settings(
        api_key="test-key",
        catalogue_dirs=(ROOT / "catalogue",),
        comfy_url="http://comfy.internal",
        deployment_type=deployment_type,
        models_dir=ROOT / "models",
        comfyui_request_timeout=1,
        ui_password="ui-key",
        ui_username="comfy",
        generation_timeout=1,
    )


def create_app(configured: Settings):
    app = create_worker(configured)
    object_info = {}
    for workflow_path in ROOT.glob("catalogue/*/workflow.json"):
        for node in json.loads(workflow_path.read_text()).values():
            object_info[node["class_type"]] = {}
    for model in app.state.runtime.catalogue.list():
        model.required_files = []
    app.state.runtime.object_info = AsyncMock(return_value=object_info)
    return app


def execution_spec(execution_id: str = "execution_1") -> str:
    return json.dumps(
        {
            "execution_id": execution_id,
            "model": "flux-2-klein-9b/text-to-image",
            "operation": "image_generation",
            "parameters": {"height": 512, "prompt": "a clean test", "width": 512},
        }
    )


@pytest.mark.asyncio
async def test_worker_readiness_requires_comfyui_node_information():
    app = create_app(settings())
    app.state.runtime.object_info = AsyncMock(return_value=None)

    assert await app.state.runtime.ready() is False

    app.state.runtime.object_info = AsyncMock(return_value={"KSampler": {}})

    assert await app.state.runtime.ready() is True


@pytest.mark.asyncio
async def test_internal_info_requires_auth_and_lists_models():
    app = create_app(settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.get("/internal/info")
        response = await client.get(
            "/internal/info", headers={"Authorization": "Bearer test-key"}
        )
        forwarded = await client.get(
            "/internal/info",
            headers={
                "Authorization": "Bearer gateway-key",
                "x-comfy-control-api-key": "test-key",
            },
        )

    assert denied.status_code == 401
    assert forwarded.status_code == 200
    assert response.json()["ready"] is True
    assert "flux-2-klein-9b/text-to-image" in response.json()["models"]


@pytest.mark.asyncio
async def test_internal_logs_require_auth(monkeypatch, tmp_path):
    log_path = tmp_path / "worker.jsonl"
    log_path.write_text(
        '{"created_at":1,"level":"error","message":"ComfyUI failed","source":"Worker"}\n'
    )
    monkeypatch.setenv("WORKER_LOG_PATH", str(log_path))
    app = create_app(settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.get("/internal/logs")
        response = await client.get(
            "/internal/logs", headers={"Authorization": "Bearer test-key"}
        )

    assert denied.status_code == 401
    assert response.json() == {
        "source": "Worker",
        "entries": [
            {
                "created_at": 1,
                "level": "error",
                "message": "ComfyUI failed",
                "source": "Worker",
            }
        ],
    }


@pytest.mark.asyncio
async def test_internal_execution_returns_and_streams_manifest_output():
    app = create_app(settings())
    app.state.runtime.run = AsyncMock(return_value=[OutputRef("result.png")])

    async def stream_output(_: OutputRef):
        yield b"png-bytes"

    app.state.runtime.comfy.stream_output = stream_output
    headers = {"Authorization": "Bearer test-key"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/executions",
            headers=headers,
            files={"spec": (None, execution_spec())},
        )
        output = await client.get(response.json()["outputs"][0]["url"], headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["outputs"][0]["url"].startswith("/internal/executions/")
    assert output.content == b"png-bytes"
    assert app.state.runtime.run.await_args.args[1]["prompt"] == "a clean test"


@pytest.mark.asyncio
async def test_internal_execution_uploads_named_files():
    app = create_app(settings())
    app.state.runtime.run = AsyncMock(return_value=[OutputRef("result.mp4")])
    app.state.runtime.comfy.upload = AsyncMock(return_value="uploaded/frame.png")
    spec = json.dumps(
        {
            "execution_id": "video_1",
            "model": "minimax-h3/image-to-video",
            "operation": "video_generation",
            "parameters": {"prompt": "move"},
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/executions",
            headers={"Authorization": "Bearer test-key"},
            files={"image": ("frame.png", b"image", "image/png"), "spec": (None, spec)},
        )

    assert response.status_code == 200
    assert app.state.runtime.run.await_args.args[1]["image"] == "uploaded/frame.png"


@pytest.mark.asyncio
async def test_internal_execution_upscales_an_uploaded_image():
    app = create_app(settings())
    app.state.runtime.run = AsyncMock(return_value=[OutputRef("upscaled.png")])
    app.state.runtime.comfy.upload = AsyncMock(return_value="uploaded/source.png")
    spec = json.dumps(
        {
            "execution_id": "upscale_1",
            "model": "image-upscale/realesrgan-x4plus",
            "operation": "image_upscale",
            "parameters": {"scale": 4.0},
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/executions",
            headers={"Authorization": "Bearer test-key"},
            files={
                "image": ("source.png", b"image", "image/png"),
                "spec": (None, spec),
            },
        )

    assert response.status_code == 200
    assert app.state.runtime.run.await_args.args[1] == {
        "image": "uploaded/source.png",
        "scale": 4.0,
    }


@pytest.mark.asyncio
async def test_internal_execution_is_idempotent():
    app = create_app(settings())
    app.state.runtime.run = AsyncMock(return_value=[OutputRef("result.png")])
    headers = {"Authorization": "Bearer test-key"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/internal/executions",
            headers=headers,
            files={"spec": (None, execution_spec())},
        )
        second = await client.post(
            "/internal/executions",
            headers=headers,
            files={"spec": (None, execution_spec())},
        )

    assert first.json() == second.json()
    app.state.runtime.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_generation_queue_rejects_excess_work():
    app = create_app(replace(settings(), generation_queue_limit=1))
    await app.state.runtime.reserve_generation()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/executions",
            headers={"Authorization": "Bearer test-key"},
            files={"spec": (None, execution_spec())},
        )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "execution_queue_full"


@pytest.mark.asyncio
async def test_request_size_limit_is_enforced():
    app = create_app(replace(settings(), maximum_request_bytes=8))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/executions",
            headers={"Authorization": "Bearer test-key"},
            content=b"0123456789",
        )

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_health_ping_and_metrics_report_comfy_readiness():
    app = create_app(settings())
    app.state.runtime.ready = AsyncMock(return_value=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")
        ping = await client.get("/ping")
        metrics = await client.get("/metrics")

    assert live.status_code == ready.status_code == ping.status_code == 200
    assert "comfy_control_requests" in metrics.text


@pytest.mark.asyncio
async def test_serverless_blocks_frontend_but_proxies_native_api():
    app = create_app(settings())
    app.state.runtime.comfy.http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"native")),
        base_url="http://comfy.internal",
    )
    headers = {"Authorization": "Bearer test-key"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        frontend = await client.get("/extensions/core.js", headers=headers)
        native = await client.get("/object_info", headers=headers)

    assert frontend.status_code == 404
    assert native.content == b"native"


@pytest.mark.asyncio
async def test_pod_proxies_frontend_with_basic_auth():
    app = create_app(settings("pod"))
    app.state.runtime.comfy.http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"frontend")
        ),
        base_url="http://comfy.internal",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.get("/extensions/core.js")
        response = await client.get("/extensions/core.js", auth=("comfy", "ui-key"))

    assert denied.status_code == 401
    assert response.content == b"frontend"


def test_worker_openapi_publishes_only_current_internal_contract():
    schema = create_app(settings()).openapi()

    assert schema["info"]["version"] == "current"
    assert "/internal/executions" in schema["paths"]
    assert not any(path.startswith("/v1/") for path in schema["paths"])
