import asyncio
import json
import os
import re
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from control.app import create_app
from control.config import ControlFile, ControlSettings
from control.dashboard.routes import (
    provider_fields,
    resource_specification,
    usage_with_resource_cost,
)
from control.dashboard.routes import (
    provider_logs as dashboard_provider_logs,
)
from control.http import exception_message
from control.inference.common import normalise_grok_image_options
from control.preferences import ControlPreferences
from control.registry import control_file as registry_control_file
from control.service import Controller, history_parameters
from control.store import ControlStore
from providers.base import StartRecovery
from providers.cliproxy import CliproxyClient
from providers.telemetry import normalise_usage, normalise_xai_quota

ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize(
    ("prompt", "aspect_ratio", "resolution"),
    [
        ("Photorealistic portrait, 2K", "9:16", "2k"),
        ("1k landscape studio photograph", "16:9", "1k"),
        ("Square composition at 2K", "1:1", "2k"),
        ("Studio photograph, 3:4, 1K", "3:4", "1k"),
    ],
)
def test_grok_image_options_are_inferred_from_prompt_in_any_order(
    prompt: str, aspect_ratio: str, resolution: str
) -> None:
    result = normalise_grok_image_options(
        {"model": "grok-imagine-image-quality", "prompt": prompt}
    )

    assert result["aspect_ratio"] == aspect_ratio
    assert result["resolution"] == resolution


def test_grok_image_options_preserve_structured_values_and_default_to_1k() -> None:
    explicit = normalise_grok_image_options(
        {
            "aspect_ratio": "2:3",
            "model": "cliproxyapi/grok-imagine-image-quality",
            "prompt": "portrait 2K",
            "resolution": "1k",
        }
    )
    defaulted = normalise_grok_image_options(
        {"model": "grok-imagine-image-quality", "prompt": "A studio photograph"}
    )
    unrelated = {"model": "another-image-model", "prompt": "portrait 2K"}

    assert explicit["aspect_ratio"] == "2:3"
    assert explicit["resolution"] == "1k"
    assert defaulted["resolution"] == "1k"
    assert "aspect_ratio" not in defaulted
    assert normalise_grok_image_options(unrelated) is unrelated


def test_provider_error_message_includes_safe_structured_detail() -> None:
    request = httpx.Request("POST", "https://provider.example.test/resources")
    response = httpx.Response(
        400,
        json={"message": "GPU class is unavailable", "token": "secret"},
        request=request,
    )

    assert (
        exception_message(
            httpx.HTTPStatusError("bad request", request=request, response=response)
        )
        == "provider API returned HTTP 400: GPU class is unavailable"
    )

    response = httpx.Response(
        400,
        json={"errors": {"readiness_probe.http.scheme": ["Field is required"]}},
        request=request,
    )
    assert (
        exception_message(
            httpx.HTTPStatusError("bad request", request=request, response=response)
        )
        == "provider API returned HTTP 400: readiness_probe.http.scheme: Field is required"
    )


@pytest.mark.parametrize(
    ("provider_id", "provider_type", "details", "expected"),
    [
        (
            "runpod-pod",
            "pod",
            {"gpuCount": 1, "gpuTypeId": "NVIDIA A40", "vcpuCount": 16},
            "NVIDIA A40 · 16 vCPU",
        ),
        (
            "runpod",
            "serverless",
            {"gpuTypeId": "NVIDIA A40", "workersMax": 3},
            "NVIDIA A40 · Up To 3 Workers",
        ),
        (
            "vast-pod",
            "pod",
            {"gpu_name": "RTX 4090", "gpu_ram": 49140, "num_gpus": 1},
            "RTX 4090 · 49.1 GB",
        ),
    ],
)
def test_resource_specification_is_concise(
    provider_id: str,
    provider_type: str,
    details: dict[str, object],
    expected: str,
) -> None:
    assert (
        resource_specification(
            provider_id,
            provider_type,
            {"details": details},
            ControlPreferences(),
        )
        == expected
    )


def write_config(path: Path) -> None:
    path.write_text(
        """
models:
  - id: public/image
    operation: image_generation
    targets:
      - model: worker/image
        provider: worker
  - id: public/image-edit
    operation: image_edit
    targets:
      - model: worker/image-edit
        provider: worker
  - id: public/upscale
    operation: image_upscale
    targets:
      - model: image-upscale/realesrgan-x4plus
        provider: worker
  - id: public/video
    operation: video_generation
    targets:
      - model: worker/video
        provider: worker
providers:
  - id: worker
    api_key: worker-key
    base_url: http://worker
    idle_seconds: 0
""".lstrip()
    )


def settings(tmp_path: Path) -> ControlSettings:
    config = tmp_path / "control.yaml"
    write_config(config)
    return ControlSettings(
        api_key="control-key",
        config_file=config,
        database_path=tmp_path / "control.db",
        ui_password="ui-password",
        ui_username="comfy",
    )


def worker_app() -> FastAPI:
    app = FastAPI()
    outputs: dict[str, tuple[bytes, str, str]] = {}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/internal/logs")
    async def logs() -> dict[str, object]:
        return {
            "source": "Worker",
            "entries": [
                {
                    "created_at": 1,
                    "level": "error",
                    "message": "ComfyUI worker traceback",
                    "source": "Worker",
                }
            ],
        }

    @app.post("/internal/executions")
    async def execute(request: Request) -> dict[str, object]:
        form = await request.form()
        spec = json.loads(str(form["spec"]))
        execution_id = str(spec["execution_id"])
        operation = spec["operation"]
        if operation == "image_generation":
            assert spec["model"] == "worker/image"
            output = (b"image", "image/png", "image.png")
        elif operation == "image_edit":
            assert spec["model"] == "worker/image-edit"
            images = form.getlist("image")
            if len(images) == 2:
                assert [image.filename for image in images] == [
                    "mara.png",
                    "elise.png",
                ]
                assert [await image.read() for image in images] == [b"mara", b"elise"]
            else:
                assert len(images) == 1
                assert await images[0].read() == b"image"
            output = (b"edit", "image/png", "edit.png")
        elif operation == "image_upscale":
            assert spec["model"] == "image-upscale/realesrgan-x4plus"
            assert 1 < spec["parameters"]["scale"] <= 4
            image = form.getlist("image")[0]
            assert await image.read() in {b"image", b"source-image"}
            output = (b"upscaled", "image/png", "upscaled.png")
        else:
            assert spec["model"] == "worker/video"
            images = form.getlist("image")
            if images:
                assert await images[0].read() == b"image"
            output = (b"video", "video/mp4", "video.mp4")
        outputs[execution_id] = output
        return {
            "execution_id": execution_id,
            "status": "completed",
            "outputs": [
                {
                    "content_type": output[1],
                    "filename": output[2],
                    "index": 0,
                    "url": f"http://worker/internal/executions/{execution_id}/outputs/0",
                }
            ],
        }

    @app.get("/internal/executions/{execution_id}/outputs/0")
    async def output(execution_id: str) -> Response:
        content, content_type, _ = outputs[execution_id]
        return Response(content, media_type=content_type)

    return app


async def attach_worker(app: FastAPI) -> None:
    runtime = app.state.controller.providers["worker"]
    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=worker_app()), base_url="http://worker"
    )


@pytest.mark.asyncio
async def test_image_upscale_routes_to_worker_and_records_lineage(tmp_path: Path):
    app = create_app(settings(tmp_path))
    await attach_worker(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        response = await client.post(
            "/v1/images/upscales",
            headers={"Authorization": "Bearer control-key"},
            data={
                "model": "public/upscale",
                "response_format": "url",
                "scale": "3",
            },
            files={"image": ("source.png", b"source-image", "image/png")},
        )

    assert response.status_code == 200
    assert float(response.headers["x-comfy-duration-seconds"]) >= 0
    assert response.headers["x-comfy-provider"] == "worker"
    history_id = response.headers["x-comfy-history-id"]
    history = next(
        item
        for item in app.state.controller.store.histories()
        if item["id"] == history_id
    )
    assert history["operation"] == "image_upscale"
    assert history["parameters"]["scale"] == 3.0
    media = app.state.controller.store.media_library(
        filters=[{"path": "history_id", "value": history_id}]
    )
    detail = app.state.controller.store.media_detail(media["data"][0]["asset_id"])
    assert detail is not None
    assert [source["filename"] for source in detail["lineage"]["sources"]] == [
        "source.png"
    ]
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_media_ui_edits_and_upscales_images(tmp_path: Path):
    app = create_app(settings(tmp_path))
    await attach_worker(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        generated = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={"model": "public/image", "prompt": "Source Image"},
        )
        history_id = generated.headers["x-comfy-history-id"]
        source = app.state.controller.store.media_library(
            filters=[{"path": "history_id", "value": history_id}]
        )["data"][0]
        await sign_in(client)
        page = await client.get("/media")
        token = re.search(r'data-csrf-token="([^"]+)"', page.text).group(1)
        detail = await client.get(f"/media/{source['asset_id']}")
        edited = await client.post(
            f"/media/{source['asset_id']}/edit",
            data={
                "csrf_token": token,
                "model": "public/image-edit",
                "prompt": "Add More Detail",
            },
        )
        upscaled = await client.post(
            f"/media/{source['asset_id']}/upscale",
            data={
                "csrf_token": token,
                "model": "public/upscale",
                "scale": "2",
            },
        )

    assert detail.json()["actions"] == {
        "image_edit": [{"id": "public/image-edit", "providers": ["worker"]}],
        "image_upscale": [{"id": "public/upscale", "providers": ["worker"]}],
    }
    assert edited.status_code == 200
    assert upscaled.status_code == 200
    assert edited.json()["asset_id"] != source["asset_id"]
    assert upscaled.json()["asset_id"] != source["asset_id"]
    source_detail = app.state.controller.store.media_detail(source["asset_id"])
    assert source_detail is not None
    assert source_detail["primary_use"]["history_id"] == history_id
    assert source_detail["primary_use"]["operation"] == "image_generation"
    assert source_detail["primary_use"]["role"] == "output"
    assert (
        app.state.controller.store.media_lineage(edited.json()["asset_id"])["sources"][
            0
        ]["id"]
        == source["asset_id"]
    )
    assert (
        app.state.controller.store.media_lineage(upscaled.json()["asset_id"])[
            "sources"
        ][0]["id"]
        == source["asset_id"]
    )
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_prefers_internal_execution_contract(tmp_path: Path):
    provider = FastAPI()

    @provider.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

    @provider.post("/internal/executions")
    async def execute(request: Request) -> dict[str, object]:
        form = await request.form()
        spec = json.loads(str(form["spec"]))
        assert spec["model"] == "worker/image"
        assert spec["operation"] == "image_generation"
        assert spec["parameters"]["prompt"] == "internal contract"
        return {
            "execution_id": spec["execution_id"],
            "status": "completed",
            "outputs": [
                {
                    "content_type": "image/png",
                    "filename": "internal.png",
                    "index": 0,
                    "url": "http://worker/internal/executions/output",
                }
            ],
        }

    @provider.get("/internal/executions/output")
    async def output() -> Response:
        return Response(b"internal-image", media_type="image/png")

    app = create_app(settings(tmp_path))
    runtime = app.state.controller.providers["worker"]
    runtime.base_url = "http://worker/"
    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=provider), base_url="http://worker"
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://control"
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={"model": "public/image", "prompt": "internal contract"},
        )

    assert response.status_code == 200
    assert response.json()["data"] == [{"b64_json": "aW50ZXJuYWwtaW1hZ2U="}]
    assert (
        app.state.controller.store.histories()[0]["media"][0]["filename"]
        == "internal.png"
    )
    assert (
        app.state.controller.store.histories()[0]["attempts"][0]["status"]
        == "completed"
    )
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_image_history_records_unexpected_provider_failure(tmp_path: Path):
    app = create_app(settings(tmp_path))
    app.state.controller.execute_internal = AsyncMock(
        side_effect=ValueError("provider deployment is incompatible")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={"model": "public/image", "prompt": "test"},
        )

    history = app.state.controller.store.histories()[0]
    assert response.status_code == 502
    assert history["error"] == "worker: provider deployment is incompatible"
    assert history["status"] == "failed"
    assert history["attempts"][0]["status"] == "failed"
    await app.state.controller.close()


async def sign_in(client: httpx.AsyncClient) -> httpx.Response:
    response = await client.post(
        "/login",
        data={"password": "ui-password", "username": "comfy"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    return response


@pytest.mark.asyncio
async def test_controller_lists_and_routes_models(tmp_path):
    app = create_app(settings(tmp_path))
    assert app.state.controller.preferences.model_dump()["routes"] == {
        "images": [{"model": "worker/image", "provider": "worker"}],
        "videos": [{"model": "worker/video", "provider": "worker"}],
    }
    await attach_worker(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        health = await client.get("/health")
        denied = await client.get("/v1/models")
        basic = await client.get("/", auth=("comfy", "ui-password"))
        login_page = await client.get("/login")
        invalid_login = await client.post(
            "/login", data={"password": "incorrect", "username": "comfy"}
        )
        await sign_in(client)
        home = await client.get("/", follow_redirects=False)
        studio = await client.get("/generate")
        dashboard = await client.get("/providers")
        models = await client.get(
            "/v1/models", headers={"Authorization": "Bearer control-key"}
        )
        image = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={"model": "worker/worker/image", "prompt": "test"},
        )
        status = await client.get("/ops/status")
        history = status.json()["history"][0]
        media = await client.get(
            f"/ops/history/{history['id']}/media/{history['media'][0]['id']}"
        )
        logout = await client.post("/logout", follow_redirects=False)
        denied_media = await client.get(
            f"/ops/history/{history['id']}/media/{history['media'][0]['id']}"
        )

    assert health.json() == {
        "models": 4,
        "providers": 1,
        "status": "ready",
    }
    assert denied.status_code == 401
    assert basic.status_code == 303
    assert basic.headers["location"] == "/login"
    assert login_page.status_code == 200
    assert 'autocomplete="current-password"' in login_page.text
    assert invalid_login.status_code == 401
    assert home.status_code == 303
    assert home.headers["location"] == "/generate"
    assert studio.status_code == 200
    assert 'class="model-grid"' in studio.text
    assert "<code>flux-2-klein-9b</code>" in studio.text
    assert dashboard.status_code == 200
    assert 'class="provider-grid"' in dashboard.text
    assert 'href="/history"' in dashboard.text
    assert 'href="/settings"' in dashboard.text
    assert 'href="/media"' in dashboard.text
    assert 'action="/logout"' in dashboard.text
    assert "setInterval" not in dashboard.text
    assert [model["id"] for model in models.json()["data"]] == [
        "public/image",
        "public/image-edit",
        "public/upscale",
        "public/video",
        "worker/image-upscale/realesrgan-x4plus",
        "worker/worker/image",
        "worker/worker/image-edit",
        "worker/worker/video",
    ]
    assert image.status_code == 200
    assert image.headers["x-comfy-provider"] == "worker"
    assert image.headers["x-comfy-history-id"] == history["id"]
    assert history["operation"] == "image_generation"
    assert history["model"] == "worker/worker/image"
    assert history["provider_model"] == "worker/image"
    assert history["parameters"]["model"] == "worker/worker/image"
    assert history["parameters"]["prompt"] == "test"
    assert isinstance(history["parameters"]["seed"], int)
    assert history["attempts"][0]["model"] == "worker/image"
    assert history["status"] == "completed"
    assert status.json()["history_pagination"] == {
        "count": 1,
        "page": 1,
        "pages": 1,
    }
    assert media.content == b"image"
    assert media.headers["content-type"] == "image/png"
    assert logout.status_code == 303
    assert logout.headers["location"] == "/login"
    assert denied_media.status_code == 401
    assert app.state.controller.store.history_usage("worker") == {
        "failed_requests": 0,
        "successful_requests": 1,
        "total_requests": 1,
    }
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_settings_are_admin_only_versioned_and_encrypted(tmp_path):
    app = create_app(settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        denied = await client.get(
            "/ops/settings", headers={"Authorization": "Bearer control-key"}
        )
        await sign_in(client)
        initial = await client.get("/ops/settings")
        revision = initial.json()["revision"]
        missing_confirmation = await client.patch(
            "/ops/settings", json={"revision": revision, "values": {}}
        )
        updated = await client.patch(
            "/ops/settings",
            headers={"x-comfy-control-settings": "update"},
            json={
                "revision": revision,
                "values": {
                    "hf_token": "hf-secret-value",
                    "worker_image": "registry.example/comfy-control:worker",
                },
            },
        )
        invalid = await client.patch(
            "/ops/settings",
            headers={"x-comfy-control-settings": "update"},
            json={
                "revision": updated.json()["revision"],
                "values": {
                    "modal_token_id": "must-not-leak",
                    "modal_token_secret": None,
                },
            },
        )
        stale = await client.patch(
            "/ops/settings",
            headers={"x-comfy-control-settings": "update"},
            json={"revision": revision, "values": {"worker_image": "stale"}},
        )

    fields = {field["name"]: field for field in updated.json()["fields"]}
    stored_secret = app.state.controller.store.connection.execute(
        "SELECT encrypted_value FROM control_secrets WHERE name = 'hf_token'"
    ).fetchone()[0]
    assert denied.status_code == 401
    assert missing_confirmation.status_code == 400
    assert updated.status_code == 200
    assert fields["hf_token"]["configured"] is True
    assert fields["hf_token"]["value"] is None
    assert fields["worker_image"]["value"] == "registry.example/comfy-control:worker"
    assert "hf-secret-value" not in stored_secret
    assert invalid.status_code == 400
    assert "must-not-leak" not in invalid.text
    assert stale.status_code == 409
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_model_packages_have_a_typed_current_api(tmp_path):
    app = create_app(settings(tmp_path))
    headers = {"Authorization": "Bearer control-key"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        denied = await client.get("/ops/model-packages")
        initial = await client.get("/ops/model-packages", headers=headers)
        updated = await client.put(
            "/ops/model-packages",
            headers=headers,
            json={
                "installed": ["krea-2-turbo"],
                "revision": initial.json()["revision"],
            },
        )
        invalid = await client.put(
            "/ops/model-packages",
            headers=headers,
            json={
                "installed": ["unknown"],
                "revision": updated.json()["revision"],
            },
        )

    assert denied.status_code == 401
    assert [package["id"] for package in initial.json()["data"]] == [
        "flux-2-klein-9b",
        "krea-2-turbo",
        "minimax-h3",
    ]
    assert updated.status_code == 200
    flux = next(
        package
        for package in initial.json()["data"]
        if package["id"] == "flux-2-klein-9b"
    )
    assert flux["minimum_vram_gb"] == 24
    assert "diffusion_models/flux-2-klein-9b-fp8.safetensors" in flux["assets"]
    assert app.state.controller.preferences.model_profiles == ["krea-2-turbo"]
    assert invalid.status_code == 400
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_provider_routes_have_a_typed_current_api(tmp_path):
    app = create_app(settings(tmp_path))
    headers = {"Authorization": "Bearer control-key"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        initial = await client.get("/ops/provider-routes", headers=headers)
        updated = await client.put(
            "/ops/provider-routes",
            headers=headers,
            json={**initial.json()},
        )

    assert initial.status_code == 200
    assert initial.json()["images"] == [{"model": "worker/image", "provider": "worker"}]
    assert updated.status_code == 200
    assert updated.json()["videos"] == [{"model": "worker/video", "provider": "worker"}]
    await app.state.controller.close()


def test_provider_routes_allow_multiple_models_per_provider():
    preferences = ControlPreferences.model_validate(
        {
            "routes": {
                "images": [
                    {"model": "flux-2-klein-9b", "provider": "modal"},
                    {"model": "krea-2-turbo", "provider": "modal"},
                ],
                "videos": [],
            }
        }
    )

    assert [route.model for route in preferences.routes["images"]] == [
        "flux-2-klein-9b",
        "krea-2-turbo",
    ]


@pytest.mark.asyncio
async def test_generated_control_file_keeps_multiple_models_per_provider(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MODAL_TOKEN_ID", "modal-id")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "modal-secret")
    monkeypatch.setenv("WORKER_API_KEY", "worker-key")
    controller = Controller(
        ControlSettings(
            api_key="control-key",
            config_file=None,
            database_path=tmp_path / "control.db",
            ui_password="ui-password",
            ui_username="comfy",
        )
    )
    preferences = controller.preferences.model_copy(
        update={
            "model_profiles": ["flux-2-klein-9b", "krea-2-turbo"],
            "routes": {
                "images": [
                    {"model": "flux-2-klein-9b", "provider": "modal"},
                    {"model": "krea-2-turbo", "provider": "modal"},
                ],
                "videos": [],
            },
        }
    )

    config = controller.load_control_file(preferences)
    image_model = next(
        model for model in config.models if model.id == "image-generation"
    )

    assert [(target.provider, target.model) for target in image_model.targets] == [
        ("modal", "flux-2-klein-9b/text-to-image"),
        ("modal", "krea-2-turbo/text-to-image"),
    ]
    await controller.close()


@pytest.mark.asyncio
async def test_environment_overrides_database_and_locks_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "environment-secret")
    monkeypatch.setenv("WORKER_IMAGE", "registry.example/environment:worker")
    configured = settings(tmp_path)
    app = create_app(configured)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        await sign_in(client)
        initial = await client.get("/ops/settings")
        fields = {field["name"]: field for field in initial.json()["fields"]}
        rejected = await client.patch(
            "/ops/settings",
            headers={"x-comfy-control-settings": "update"},
            json={
                "revision": initial.json()["revision"],
                "values": {"hf_token": "database-secret"},
            },
        )
        updated = await client.patch(
            "/ops/settings",
            headers={"x-comfy-control-settings": "update"},
            json={
                "revision": initial.json()["revision"],
                "values": {"maximum_request_mib": 64},
            },
        )
        page = await client.get("/settings")

    assert fields["hf_token"]["configured"] is True
    assert fields["hf_token"]["locked"] is True
    assert fields["worker_image"]["locked"] is True
    assert fields["worker_image"]["value"] == "registry.example/environment:worker"
    assert rejected.status_code == 400
    assert "environment-controlled" in rejected.text
    assert updated.status_code == 200
    assert "Worker Image" in page.text
    assert "Environment controlled" in page.text
    assert "Controlled By Environment" not in page.text
    assert "disabled" in page.text
    assert (
        app.state.controller.store.connection.execute(
            "SELECT encrypted_value FROM control_secrets WHERE name = 'hf_token'"
        ).fetchone()
        is None
    )
    await app.state.controller.close()

    monkeypatch.setenv("HF_TOKEN", "replacement-secret")
    monkeypatch.setenv("WORKER_IMAGE", "registry.example/replacement:worker")
    replacement = create_app(configured)
    assert replacement.state.controller.preferences.hf_token == "replacement-secret"
    assert replacement.state.controller.preferences.worker_image == (
        "registry.example/replacement:worker"
    )
    await replacement.state.controller.close()

    monkeypatch.delenv("HF_TOKEN")
    monkeypatch.delenv("WORKER_IMAGE")
    restored = create_app(configured)
    assert restored.state.controller.preferences.hf_token == ""
    assert restored.state.controller.preferences.worker_image == (
        "ghcr.io/maxexcloo/comfy-control:worker"
    )
    assert restored.state.controller.preferences.maximum_request_mib == 64
    await restored.state.controller.close()


def test_packaged_revision_uses_live_worker_image(monkeypatch):
    monkeypatch.delenv("WORKER_IMAGE", raising=False)
    monkeypatch.setenv(
        "COMFY_CONTROL_REVISION", "cd208c90d415720d783711fa6cbc7b14d1d71c05"
    )

    configured = ControlPreferences.from_environment()
    overrides = ControlPreferences.environment_overrides()

    assert configured.worker_image == "ghcr.io/maxexcloo/comfy-control:worker"
    assert configured.display_time_zone == "Australia/Sydney"
    assert "worker_image" not in overrides


@pytest.mark.asyncio
async def test_server_rendered_settings_require_csrf(monkeypatch, tmp_path):
    monkeypatch.setenv("MODAL_TOKEN_ID", "modal-id")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "modal-secret")
    app = create_app(settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        await sign_in(client)
        page = await client.get("/settings")
        rejected = await client.post(
            "/settings", data={"csrf_token": "invalid", "revision": "1"}
        )
        provider_rejected = await client.post(
            "/providers/modal/settings",
            data={"csrf_token": "invalid", "revision": "1"},
        )

    assert page.status_code == 200
    assert 'action="/settings"' in page.text
    assert 'name="csrf_token"' in page.text
    assert "data-route-editor" in page.text
    assert 'data-route-key="images"' in page.text
    assert 'data-route-key="videos"' in page.text
    assert page.text.count("data-route-key=") == 2
    assert "data-route-provider" in page.text
    assert "data-route-model" in page.text
    assert "data-route-template" in page.text
    assert 'data-model-package="flux-2-klein-9b"' in page.text
    assert "Generation Queue Limit" in page.text
    assert "Maximum Request Size (MiB)" in page.text
    assert "Australia/Sydney" in page.text
    assert "Dates &amp; Times" in page.text
    assert "Provider Routes" in page.text
    assert "Worker Model Packages" in page.text
    assert "Public Base URL" not in page.text
    assert "Request Limits" not in page.text
    assert "Modal Token ID" not in page.text
    description = app.state.controller.describe_configuration()
    route_field = next(
        field for field in description["fields"] if field["name"] == "routes"
    )
    assert route_field["providers"]
    modal_fields = provider_fields(description, "modal")
    assert [field["name"] for field in modal_fields] == [
        "modal_gpu",
        "modal_minimum_containers",
        "modal_model_volume",
        "modal_scaledown_window",
        "modal_token_id",
        "modal_token_secret",
    ]
    assert [field["label"] for field in modal_fields] == [
        "GPU",
        "Minimum Containers",
        "Model Volume",
        "Scaledown Window (Seconds)",
        "Token ID",
        "Token Secret",
    ]
    assert [
        field["label"] for field in provider_fields(description, "cliproxyapi")
    ] == ["API Key", "Management Key", "URL"]
    assert rejected.status_code == 403
    assert provider_rejected.status_code == 403
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_resolves_shared_explicit_video_route(tmp_path):
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models:
  - id: public/image-to-video
    operation: video_generation
    targets:
      - { model: upstream/video, provider: worker }
  - id: public/text-to-video
    operation: video_generation
    targets:
      - { model: upstream/video, provider: worker }
providers:
  - id: worker
    api_key: worker-key
    base_url: http://worker
""".lstrip()
    )
    app = create_app(configured)

    model, targets = app.state.controller.resolve_model(
        "worker/upstream/video", "video_generation"
    )

    assert model.id == "public/image-to-video"
    assert [(target.model, target.provider) for target in targets] == [
        ("upstream/video", "worker")
    ]
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_dashboard_tests_provider_and_shows_control_logs(tmp_path):
    app = create_app(settings(tmp_path))
    await attach_worker(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        await sign_in(client)
        tested = await client.post(
            "/ops/providers/worker/test",
            json={
                "model": "public/image",
                "prompt": "test from the dashboard",
                "size": "512x512",
            },
        )
        logs = await client.get("/ops/providers/worker/logs")
        media = await client.get(
            "/ops/history/"
            f"{tested.json()['history_id']}/media/{tested.json()['media'][0]['id']}"
        )

    assert tested.status_code == 200
    assert tested.json()["provider"] == "worker"
    assert tested.json()["status"] == "completed"
    assert logs.status_code == 200
    assert logs.json()["source"] == "Controller and Worker"
    assert logs.json()["entries"][0]["message"] == "dashboard test completed"
    assert any(
        entry["message"] == "ComfyUI worker traceback" and entry["source"] == "Worker"
        for entry in logs.json()["entries"]
    )
    assert media.content == b"image"
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_rewrites_worker_image_url_to_archived_media(tmp_path):
    provider_app = FastAPI()

    @provider_app.get("/health/ready")
    async def provider_ready() -> dict[str, str]:
        return {"status": "ready"}

    @provider_app.post("/internal/executions")
    async def provider_generation(request: Request) -> dict[str, object]:
        spec = json.loads(str((await request.form())["spec"]))
        return {
            "execution_id": spec["execution_id"],
            "status": "completed",
            "outputs": [
                {
                    "content_type": "image/png",
                    "filename": "generated.png",
                    "index": 0,
                    "url": "http://worker/generated.png",
                }
            ],
        }

    @provider_app.get("/generated.png")
    async def provider_media() -> Response:
        return Response(b"archived-image", media_type="image/png")

    app = create_app(settings(tmp_path))
    runtime = app.state.controller.providers["worker"]
    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=provider_app), base_url="http://worker"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        generated = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={
                "model": "public/image",
                "prompt": "test",
                "response_format": "url",
            },
        )
        archived_url = generated.json()["data"][0]["url"]
        archived = await client.get(
            archived_url, headers={"Authorization": "Bearer control-key"}
        )

    assert archived_url.startswith("http://control/ops/history/")
    assert archived.content == b"archived-image"
    assert archived.headers["content-type"] == "image/png"
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_rejects_provider_outside_selected_model(tmp_path):
    app = create_app(settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={
                "model": "public/image",
                "prompt": "test",
                "provider": "missing",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert "unknown provider: missing" in response.json()["error"]["message"]
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_preserves_repeated_image_fields_in_order(tmp_path):
    app = create_app(settings(tmp_path))
    await attach_worker(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        response = await client.post(
            "/v1/images/edits",
            headers={"Authorization": "Bearer control-key"},
            data={
                "model": "worker/worker/image-edit",
                "prompt": "Mara and Elise",
            },
            files=[
                ("image", ("mara.png", b"mara", "image/png")),
                ("image", ("elise.png", b"elise", "image/png")),
            ],
        )

    assert response.status_code == 200
    assert response.headers["x-comfy-provider"] == "worker"
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_owns_video_job_and_output_url(tmp_path):
    app = create_app(settings(tmp_path))
    await attach_worker(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        submitted = await client.post(
            "/v1/videos",
            headers={"Authorization": "Bearer control-key"},
            json={"model": "worker/worker/video", "prompt": "test"},
        )
        job_id = submitted.json()["id"]
        for _ in range(20):
            status = await client.get(
                f"/v1/videos/{job_id}",
                headers={"Authorization": "Bearer control-key"},
            )
            if status.json()["status"] == "completed":
                break
            await asyncio.sleep(0)
        content = await client.get(
            f"/v1/videos/{job_id}/content",
            headers={"Authorization": "Bearer control-key"},
            follow_redirects=False,
        )

    assert submitted.status_code == 200
    assert status.json()["model"] == "public/video"
    assert content.status_code == 200
    assert content.content == b"video"
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_polls_cliproxy_video_instead_of_worker_wait_endpoint(
    tmp_path, monkeypatch
):
    control_settings = settings(tmp_path)
    config = control_settings.config_file
    config.write_text(
        """
models:
  - id: public/video
    operation: video_generation
    targets:
      - model: grok-imagine-video-1.5
        provider: cliproxyapi
providers:
  - id: cliproxyapi
    api_key: proxy-key
    base_url: http://cliproxy
    idle_seconds: 0
    type: proxy
""".lstrip()
    )
    received: list[dict[str, object]] = []

    async def generate_video(self: CliproxyClient, body: dict[str, object]) -> str:
        received.append(body)
        return "https://videos.example/grok.mp4"

    monkeypatch.setattr(CliproxyClient, "generate_video", generate_video)
    app = create_app(control_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        submitted = await client.post(
            "/v1/videos",
            headers={"Authorization": "Bearer control-key"},
            json={
                "aspect_ratio": "16:9",
                "model": "public/video",
                "prompt": "test",
                "provider": "cliproxyapi",
                "resolution": "480p",
                "seconds": 6,
            },
        )
        assert submitted.status_code == 200, submitted.text
        job_id = submitted.json()["id"]
        for _ in range(20):
            status = await client.get(
                f"/v1/videos/{job_id}",
                headers={"Authorization": "Bearer control-key"},
            )
            if status.json()["status"] == "completed":
                break
            await asyncio.sleep(0)

    assert received == [
        {
            "aspect_ratio": "16:9",
            "model": "public/video",
            "prompt": "test",
            "resolution": "480p",
            "seconds": 6,
            "seed": received[0]["seed"],
        }
    ]
    assert status.json()["model"] == "public/video"
    assert status.json()["output_url"] == "https://videos.example/grok.mp4"
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_persists_multipart_video_until_completion(tmp_path):
    app = create_app(settings(tmp_path))
    await attach_worker(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        submitted = await client.post(
            "/v1/videos",
            headers={"Authorization": "Bearer control-key"},
            data={"model": "public/video", "prompt": "test"},
            files={"image": ("frame.png", b"image", "image/png")},
        )
        job_id = submitted.json()["id"]
        for _ in range(20):
            status = await client.get(
                f"/v1/videos/{job_id}",
                headers={"Authorization": "Bearer control-key"},
            )
            if status.json()["status"] == "completed":
                break
            await asyncio.sleep(0)

    assert status.json()["status"] == "completed", status.json()["error"]
    assert not (tmp_path / "uploads" / job_id).exists()
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_history_and_media_survive_controller_restart(tmp_path):
    configured = settings(tmp_path)
    first = create_app(configured)
    await attach_worker(first)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=first), base_url="http://control"
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={"model": "public/image", "prompt": "persistent"},
        )
    history_id = response.headers["x-comfy-history-id"]
    await first.state.controller.close()

    second = create_app(configured)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=second), base_url="http://control"
    ) as client:
        await sign_in(client)
        history = await client.get("/ops/history")
        item = history.json()["data"][0]
        media = await client.get(
            f"/ops/history/{history_id}/media/{item['media'][0]['id']}"
        )

    assert item["id"] == history_id
    assert item["parameters"]["prompt"] == "persistent"
    assert media.content == b"image"
    await second.state.controller.close()


@pytest.mark.asyncio
async def test_dashboard_paginates_persisted_history_and_events(tmp_path):
    app = create_app(settings(tmp_path))
    for index in range(21):
        app.state.controller.store.save_history(
            f"history-{index}",
            "image_generation",
            "public/image",
            '{"model":"public/image"}',
        )
        app.state.controller.store.event("info", f"event {index}", provider="worker")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        response = await client.get(
            "/ops/status?event_page=2&history_page=2",
            headers={"Authorization": "Bearer control-key"},
        )

    assert response.status_code == 200
    assert len(response.json()["events"]) == 1
    assert len(response.json()["history"]) == 1
    assert response.json()["event_pagination"] == {
        "count": 21,
        "page": 2,
        "pages": 2,
    }
    assert response.json()["history_pagination"] == {
        "count": 21,
        "page": 2,
        "pages": 2,
    }
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_dashboard_pages_filter_link_and_stream_current_data(tmp_path):
    app = create_app(settings(tmp_path))
    store = app.state.controller.store
    for index in range(30):
        store.save_history(
            f"other-{index}",
            "video_generation",
            "public/video",
            json.dumps({"prompt": f"Unrelated Prompt {index}"}),
        )
        store.event("info", f"Unrelated Event {index}", provider="other")
    history_id = "image-wombat"
    store.save_history(
        history_id,
        "image_generation",
        "public/image",
        '{"prompt":"Wombat Needle Portrait","seed":42}',
    )
    store.update_history(
        history_id,
        "completed",
        provider="worker",
        provider_model="flux-2-klein-9b/text-to-image",
    )
    output = tmp_path / "wombat.png"
    output.write_bytes(b"image")
    store.save_media(
        history_id,
        "image/png",
        "wombat.png",
        output,
        output.stat().st_size,
    )
    store.event(
        "warning",
        "Wombat Needle Event",
        provider="worker",
        request_id=history_id,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        await sign_in(client)
        providers = await client.get("/providers")
        provider_telemetry = await client.get(
            "/providers", params={"telemetry": "true"}
        )
        events = await client.get(
            "/events",
            params={"level": "warning", "provider": "worker", "q": "wombat needle"},
        )
        history = await client.get(
            "/history",
            params={
                "model": "flux-2-klein-9b/text-to-image",
                "operation": "image_generation",
                "provider": "worker",
                "q": "wombat needle",
                "status": "completed",
            },
        )
        media = await client.get(
            "/media", params={"filter": f"history_id|equals|{history_id}"}
        )
        detail = await client.get(
            f"/media/{store.media_library()['data'][0]['asset_id']}"
        )
        stylesheet = await client.get("/assets/dashboard.css")
        javascript = await client.get("/assets/dashboard.js")

        cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
        request = Request(
            {
                "app": app,
                "headers": [(b"cookie", cookie.encode())],
                "method": "GET",
                "path": "/providers/worker/logs",
                "query_string": b"",
                "scheme": "http",
                "server": ("control", 80),
                "type": "http",
            }
        )
        log_stream = await dashboard_provider_logs("worker", request)
        first_log_update = await anext(log_stream.body_iterator)
        await log_stream.body_iterator.aclose()

    assert providers.status_code == 200
    assert provider_telemetry.status_code == 200
    assert "data-provider-refresh" not in provider_telemetry.text
    assert (
        providers.text.index('href="/generate"')
        < providers.text.index('href="/media"')
        < providers.text.index('href="/history"')
        < providers.text.index('href="/providers"')
        < providers.text.index('href="/settings"')
    )
    assert 'aria-current="page" href="/providers">Providers' in providers.text
    assert "Compute, spend, and deployment controls." in providers.text
    assert 'class="skip-link"' in providers.text
    assert "@tabler/core" not in providers.text
    assert "basecoat-css@1.0.2" in providers.text
    assert 'href="/assets/dashboard.css?current"' in providers.text
    assert '<html class="dark"' in providers.text
    assert 'data-time-zone="Australia/Sydney"' in providers.text
    assert 'hx-get="/providers?telemetry=true"' in providers.text
    assert 'class="provider-grid"' in providers.text
    assert 'class="card h-full provider-card"' in providers.text
    assert 'src="/assets/dashboard.js?current"' in providers.text
    assert 'data-log-url="/providers/worker/logs"' in providers.text
    assert "Not deployed" in providers.text
    assert 'id="log-dialog"' in providers.text
    assert 'id="deploy-dialog"' in providers.text
    assert ">Logs</button" not in providers.text
    assert providers.text.index("Usage") < providers.text.index("Resource")
    assert 'href="http://worker"' in providers.text
    assert "Open Panel" not in providers.text
    assert events.text.count("Wombat Needle Event") == 1
    assert "Apply Filters" not in events.text
    assert "data-live-filter" in events.text
    assert 'aria-sort="descending"' in events.text
    assert "Unrelated Event" not in events.text
    assert f"/history?q={history_id}" in events.text
    assert '<span class="font-mono">image-wombat</span>' in events.text
    assert history.text.count(history_id) >= 1
    assert "Apply Filters" not in history.text
    assert "data-live-filter" in history.text
    assert "Provider Model:" not in history.text
    assert re.search(
        r'<option\s+value="flux-2-klein-9b/text-to-image"\s+selected\s*>',
        history.text,
    )
    assert "Unrelated Prompt" not in history.text
    assert 'data-media-id="1"' in history.text
    assert 'id="media-dialog"' in media.text
    assert 'aria-labelledby="media-title"' in media.text
    assert "data-next-media" in media.text
    assert "data-previous-media" in media.text
    assert 'data-media-id="' in media.text
    assert "Apply Filters" not in media.text
    assert "data-live-filter" in media.text
    assert detail.status_code == 200
    assert detail.json()["uses"][0]["prompt"] == "Wombat Needle Portrait"
    assert "path" not in detail.json()
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert stylesheet.headers["cache-control"] == "no-store"
    assert "@media (max-width: 767.98px)" in stylesheet.text
    assert ".media-grid" in stylesheet.text
    assert ".provider-grid" in stylesheet.text
    assert ".status-label" in stylesheet.text
    assert javascript.headers["content-type"].startswith("text/javascript")
    assert javascript.headers["cache-control"] == "no-store"
    assert "if (!mediaDialog.open) mediaDialog.showModal()" in javascript.text
    assert '"Generation time"' in javascript.text
    assert '["KiB", "MiB", "GiB"]' in javascript.text
    assert 'event.key === "ArrowLeft"' in javascript.text
    assert 'event.key === "ArrowRight"' in javascript.text
    assert 'event.key === "Escape"' in javascript.text
    assert "closeDialogOnBackdrop(mediaDialog)" in javascript.text
    assert "closeDialogOnBackdrop(logDialog)" in javascript.text
    assert 'element("a", "", "View Source")' in javascript.text
    assert "`Used for ${operation}`" in javascript.text
    assert "form.requestSubmit()" in javascript.text
    assert "closeDialogOnBackdrop(deployDialog)" in javascript.text
    assert "logBody.scrollTop = logBody.scrollHeight" in javascript.text
    assert 'following ? "Following" : "Paused"' in javascript.text
    assert "[...data.entries]" in javascript.text
    assert "event.preventDefault()" in javascript.text
    assert log_stream.media_type == "text/event-stream"
    assert str(first_log_update).startswith("data:")
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_uses_ordered_fallback(tmp_path):
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models:
  - id: public/image
    operation: image_generation
    targets:
      - model: worker/image
        provider: primary
      - model: worker/image
        provider: fallback
providers:
  - id: fallback
    api_key: worker-key
    base_url: http://fallback
    idle_seconds: 0
  - id: primary
    api_key: worker-key
    base_url: http://primary
    idle_seconds: 0
    startup_timeout: 0
""".lstrip()
    )
    app = create_app(configured)

    calls: dict[int, int] = {}

    def provider(status_code: int) -> FastAPI:
        provider_app = FastAPI()

        @provider_app.get("/health/ready")
        async def ready() -> dict[str, str]:
            return {"status": "ready"}

        @provider_app.post("/internal/executions")
        async def image(request: Request) -> JSONResponse:
            calls[status_code] = calls.get(status_code, 0) + 1
            spec = json.loads(str((await request.form())["spec"]))
            return JSONResponse(
                {
                    "execution_id": spec["execution_id"],
                    "status": "completed",
                    "outputs": [
                        {
                            "content_type": "image/png",
                            "filename": "image.png",
                            "index": 0,
                            "url": f"http://fallback/output/{status_code}",
                        }
                    ],
                },
                status_code=status_code,
            )

        @provider_app.get("/output/{code}")
        async def output(code: int) -> Response:
            return Response(str(code).encode(), media_type="image/png")

        return provider_app

    for provider_id, status_code in (("fallback", 200), ("primary", 503)):
        runtime = app.state.controller.providers[provider_id]
        await runtime.client.aclose()
        runtime.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=provider(status_code)),
            base_url=f"http://{provider_id}",
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={"model": "public/image", "prompt": "test"},
        )

    assert response.status_code == 200
    assert response.headers["x-comfy-provider"] == "fallback"
    assert calls == {200: 1, 503: 1}
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_reserves_provider_before_readiness_check(tmp_path):
    app = create_app(settings(tmp_path))
    runtime = app.state.controller.providers["worker"]
    provider_app = FastAPI()

    @provider_app.get("/health/ready")
    async def ready() -> dict[str, str]:
        assert runtime.active_requests == 1
        return {"status": "ready"}

    @provider_app.post("/internal/executions")
    async def image(request: Request) -> dict[str, object]:
        spec = json.loads(str((await request.form())["spec"]))
        return {
            "execution_id": spec["execution_id"],
            "status": "completed",
            "outputs": [],
        }

    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=provider_app), base_url="http://worker"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={"model": "public/image", "prompt": "test"},
        )

    assert response.status_code == 200
    assert runtime.active_requests == 0
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_limits_chunked_requests_while_streaming(tmp_path):
    app = create_app(settings(tmp_path))
    app.state.controller.preferences.maximum_request_mib = 1

    async def chunks():
        yield b"1" * (512 * 1024)
        yield b"2" * (512 * 1024 + 1)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            content=chunks(),
        )

    assert response.status_code == 413
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_exposes_authenticated_provider_actions(tmp_path):
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models: []
providers:
  - id: worker
    api_key: worker-key
    base_url: http://worker
    actions:
      deploy:
        confirmation: Deploy the worker?
        url: http://management/deploy
""".lstrip()
    )
    app = create_app(configured)
    management = FastAPI()

    @management.post("/deploy")
    async def deploy() -> dict[str, str]:
        return {"api_key": "must-not-reach-the-browser", "status": "deployed"}

    await app.state.controller.lifecycle_client.aclose()
    app.state.controller.lifecycle_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=management), base_url="http://management"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        bearer = {"Authorization": "Bearer control-key"}
        status = await client.get("/ops/status", headers=bearer)
        deployment_options = await client.get(
            "/ops/providers/worker/deployment-options", headers=bearer
        )
        rejected = await client.post(
            "/ops/providers/worker/actions/deploy", headers=bearer
        )
        deployed = await client.post(
            "/ops/providers/worker/actions/deploy",
            headers={
                **bearer,
                "x-comfy-control-action": "worker/deploy",
            },
        )

    assert status.json()["providers"][0]["actions"] == [
        {"confirmation": "Deploy the worker?", "name": "deploy"}
    ]
    assert deployment_options.json() == {"provider": "worker", "options": []}
    assert rejected.status_code == 400
    assert deployed.json()["body"] == {"api_key": "***", "status": "deployed"}
    messages = [
        entry["message"]
        for entry in (await app.state.controller.provider_logs("worker"))["entries"]
    ]
    assert messages == [
        "provider action deploy succeeded",
        "provider action deploy started",
    ]
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_tracks_provider_api_resource(tmp_path):
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models: []
providers:
  - id: worker
    api_key: worker-key
    base_url: https://{resource_id}-8000.example.test
    actions:
      deploy:
        resource_id_path: resource.id
        url: http://management/deploy
      destroy:
        method: DELETE
        url: http://management/resources/{resource_id}
""".lstrip()
    )
    app = create_app(configured)
    management = FastAPI()
    destroyed: list[str] = []

    @management.post("/deploy")
    async def deploy() -> dict[str, object]:
        return {"resource": {"id": "pod-123"}}

    @management.delete("/resources/{resource_id}")
    async def destroy(resource_id: str) -> dict[str, bool]:
        destroyed.append(resource_id)
        return {"deleted": True}

    await app.state.controller.lifecycle_client.aclose()
    app.state.controller.lifecycle_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=management), base_url="http://management"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        await sign_in(client)
        deployed = await client.post(
            "/ops/providers/worker/actions/deploy",
            headers={"x-comfy-control-action": "worker/deploy"},
        )
        status = await client.get("/ops/status")
        destroyed_response = await client.post(
            "/ops/providers/worker/actions/destroy",
            headers={"x-comfy-control-action": "worker/destroy"},
        )

    assert deployed.status_code == 200
    assert status.json()["providers"][0]["resource_id"] == "pod-123"
    assert destroyed_response.status_code == 200
    assert destroyed == ["pod-123"]
    assert app.state.controller.store.provider_resource("worker") is None
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_clears_resource_when_provider_already_deleted(tmp_path):
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models: []
providers:
  - id: worker
    api_key: worker-key
    base_url: https://{resource_id}-8000.example.test
    actions:
      deploy:
        resource_id_path: resource.id
        url: http://management/deploy
      terminate:
        method: DELETE
        url: http://management/resources/{resource_id}
""".lstrip()
    )
    app = create_app(configured)
    management = FastAPI()

    @management.post("/deploy")
    async def deploy() -> dict[str, object]:
        return {"resource": {"id": "pod-123"}}

    @management.delete("/resources/{resource_id}")
    async def terminate(resource_id: str) -> Response:
        assert resource_id == "pod-123"
        return Response(status_code=404)

    await app.state.controller.lifecycle_client.aclose()
    app.state.controller.lifecycle_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=management), base_url="http://management"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        await sign_in(client)
        await client.post(
            "/ops/providers/worker/actions/deploy",
            headers={"x-comfy-control-action": "worker/deploy"},
        )
        app.state.controller.providers["worker"].active_requests = 1
        terminated = await client.post(
            "/ops/providers/worker/actions/terminate",
            headers={"x-comfy-control-action": "worker/terminate"},
        )

    assert terminated.status_code == 200
    assert terminated.json()["status"] == 404
    assert app.state.controller.store.provider_resource("worker") is None
    await app.state.controller.close()


def test_control_config_expands_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_TOKEN", "secret")
    config = tmp_path / "control.yaml"
    config.write_text(
        """
models: []
providers:
  - id: worker
    api_key: env.CONTROL_TOKEN
    base_url: http://worker
    lifecycle:
      start:
        headers:
          authorization: Bearer ${CONTROL_TOKEN}
        url: http://control/start
    actions:
      status:
        url: http://control/status
""".lstrip()
    )

    loaded = ControlFile.load(config)

    assert loaded.providers[0].api_key == "secret"
    assert loaded.providers[0].lifecycle.start is not None
    assert loaded.providers[0].actions["status"].url == "http://control/status"
    assert (
        loaded.providers[0].lifecycle.start.headers["authorization"] == "Bearer secret"
    )


def test_control_settings_reject_placeholders(monkeypatch):
    monkeypatch.setenv("CONTROL_API_KEY", "replace-with-sk-media-key")
    monkeypatch.setenv("CONTROL_UI_PASSWORD", "secure-password")

    with pytest.raises(ValueError, match="non-placeholder"):
        ControlSettings.from_env()


def test_control_preferences_reject_non_positive_request_limit(monkeypatch):
    monkeypatch.setenv("MAXIMUM_REQUEST_MIB", "0")

    with pytest.raises(ValueError, match="numeric configuration values"):
        ControlPreferences.from_environment()


def test_control_settings_use_packaged_registry_by_default(monkeypatch):
    monkeypatch.setenv("CONTROL_API_KEY", "secure-key")
    monkeypatch.setenv("CONTROL_SECRET_KEY", "secure-secret-key-at-least-32-characters")
    monkeypatch.setenv("CONTROL_UI_PASSWORD", "secure-password")
    monkeypatch.delenv("CONTROL_CONFIG", raising=False)

    assert ControlSettings.from_env().config_file is None


def test_compose_forwards_only_bootstrap_environment():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert set(compose["services"]) == {"comfy-control"}
    service = compose["services"]["comfy-control"]
    forwarded = set(service["environment"])
    env_example = {
        line.partition("=")[0]
        for line in (ROOT / ".env.example").read_text().splitlines()
        if line and not line.startswith("#")
    }

    assert service["env_file"] == [{"path": ".env", "required": False}]
    assert env_example <= forwarded
    assert env_example == forwarded - {"CONTROL_CONFIG", "CONTROL_DATABASE"}
    for name in env_example:
        assert str(
            compose["services"]["comfy-control"]["environment"][name]
        ).startswith(f"${{{name}")


def test_control_file_enables_configured_providers(monkeypatch):
    monkeypatch.setenv("CLIPROXY_API_KEY", "cliproxy-key")
    monkeypatch.setenv("CLIPROXY_MANAGEMENT_KEY", "management-key")
    monkeypatch.setenv("CLIPROXY_URL", "http://cliproxy")
    monkeypatch.setenv("RUNPOD_POD_ID", "pod-id")
    monkeypatch.setenv("RUNPOD_POD_URL", "https://pod.example")
    monkeypatch.setenv("RUNPOD_API_KEY", "provider-key")
    monkeypatch.setenv("WORKER_API_KEY", "worker-key")

    loaded = registry_control_file(os.environ)

    assert [model.id for model in loaded.models] == [
        "image-edit",
        "image-generation",
        "image-upscale",
        "flux-2-klein-9b",
        "flux-2-klein-9b-edit",
        "grok-image",
        "grok-image-edit",
        "grok-video",
    ]
    assert [provider.id for provider in loaded.providers] == [
        "cliproxyapi",
        "runpod",
        "runpod-pod",
    ]
    assert {target.provider for target in loaded.models[0].targets} == {
        "runpod",
        "runpod-pod",
    }
    assert (
        next(model for model in loaded.models if model.id == "grok-image")
        .targets[0]
        .provider
        == "cliproxyapi"
    )
    assert loaded.providers[2].type == "pod"


def test_control_file_enables_managed_providers_from_credentials(monkeypatch):
    monkeypatch.setenv("CLIPROXY_API_KEY", "cliproxy-key")
    monkeypatch.setenv("CLIPROXY_MANAGEMENT_KEY", "management-key")
    monkeypatch.setenv("CLIPROXY_URL", "http://cliproxy")
    monkeypatch.setenv("MODAL_TOKEN_ID", "modal-id")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "modal-secret")
    monkeypatch.setenv("RUNPOD_API_KEY", "runpod-key")
    monkeypatch.setenv("SALAD_API_KEY", "salad-key")
    monkeypatch.setenv("SALAD_ORGANISATION", "salad-organisation")
    monkeypatch.setenv("SALAD_PROJECT", "salad-project")
    monkeypatch.setenv("VAST_API_KEY", "vast-key")
    monkeypatch.setenv("WORKER_API_KEY", "worker-key")

    loaded = registry_control_file(os.environ)

    assert [provider.id for provider in loaded.providers] == [
        "cliproxyapi",
        "modal",
        "runpod",
        "runpod-pod",
        "salad",
        "vast",
        "vast-pod",
    ]
    assert all(
        provider.base_url is None
        for provider in loaded.providers
        if provider.management
    )
    providers = {provider.id: provider for provider in loaded.providers}
    assert providers["modal"].actions["deploy"].internal == "modal-deploy"
    assert providers["modal"].actions["terminate"].internal == "modal-terminate"
    assert sorted(providers["runpod"].actions) == [
        "deploy",
        "scale-down",
        "scale-up",
        "terminate",
    ]
    assert sorted(providers["salad"].actions) == ["deploy", "terminate"]
    assert sorted(providers["vast"].actions) == ["deploy", "terminate"]


@pytest.mark.asyncio
async def test_controller_discovers_managed_runpod_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "provider-key")
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models: []
providers:
  - id: runpod-pod
    api_key: worker-key
    management:
      kind: runpod-pod
      name: comfy-control
""".lstrip()
    )
    app = create_app(configured)
    management = FastAPI()

    @management.get("/v1/pods")
    async def pods(request: Request) -> list[dict[str, str]]:
        assert request.headers["authorization"] == "Bearer provider-key"
        return [{"id": "pod-123", "name": "comfy-control"}]

    await app.state.controller.lifecycle_client.aclose()
    app.state.controller.lifecycle_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=management), base_url="https://rest.runpod.io"
    )
    runtime = app.state.controller.providers["runpod-pod"]

    await app.state.controller.refresh_endpoint(runtime)

    assert runtime.base_url == "https://pod-123-8000.proxy.runpod.net"
    assert app.state.controller.resource_id("runpod-pod") == "pod-123"
    assert sorted(app.state.controller.available_actions("runpod-pod")) == [
        "start",
        "stop",
    ]
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_missing_managed_resource_is_not_deployed(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "provider-key")
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models: []
providers:
  - id: runpod-pod
    actions:
      deploy:
        internal: provider-deploy
        resource_id_path: id
    api_key: worker-key
    management:
      kind: runpod-pod
      name: comfy-control
""".lstrip()
    )
    app = create_app(configured)
    await app.state.controller.lifecycle_client.aclose()
    app.state.controller.lifecycle_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))
    )
    runtime = app.state.controller.providers["runpod-pod"]

    status = await app.state.controller.provider_status(runtime)

    assert status["state"] == "not-deployed"
    assert status["status"] == "ok"
    assert sorted(app.state.controller.available_actions("runpod-pod")) == ["deploy"]
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_request_auto_deploys_missing_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "provider-key")
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models: []
providers:
  - id: worker
    actions:
      deploy:
        internal: provider-deploy
        resource_id_path: id
    api_key: worker-key
    management:
      kind: runpod-pod
      name: comfy-control
""".lstrip()
    )
    app = create_app(configured)
    controller = app.state.controller
    runtime = controller.providers["worker"]
    controller.check_ready = AsyncMock(side_effect=[False, False, True])
    controller.action = AsyncMock(return_value=httpx.Response(201, json={"id": "pod"}))

    await controller.ensure_ready(runtime, "request-1")

    controller.action.assert_awaited_once()
    assert controller.action.await_args.args[2] == "deploy"
    assert controller.check_ready.await_count == 3
    await controller.close()


@pytest.mark.asyncio
async def test_request_replaces_runpod_pod_stranded_on_busy_host(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "provider-key")
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models: []
providers:
  - id: runpod-pod
    api_key: worker-key
    management:
      kind: runpod-pod
      name: comfy-control
""".lstrip()
    )
    app = create_app(configured)
    controller = app.state.controller
    runtime = controller.providers["runpod-pod"]
    controller.store.save_provider_resource("runpod-pod", "pod-old")
    controller.check_ready = AsyncMock(side_effect=[False, False, True])
    request = httpx.Request("POST", "https://rest.runpod.io/v1/pods/pod-old/start")
    response = httpx.Response(500, request=request)
    controller.action = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "host capacity unavailable", request=request, response=response
        )
    )
    adapter = AsyncMock()
    adapter.recover_start.return_value = StartRecovery("pod-new", "pod-old")
    adapter.terminate.return_value = httpx.Response(204)
    monkeypatch.setattr("control.service.provider_adapter", lambda _: adapter)

    await controller.ensure_ready(runtime, "request-1")

    assert controller.store.provider_resource("runpod-pod") == "pod-new"
    adapter.terminate.assert_awaited_once_with(
        controller.lifecycle_client,
        runtime.config,
        controller.preferences,
        "pod-old",
    )
    await controller.close()


@pytest.mark.asyncio
async def test_failed_runpod_replacement_restores_original_pod(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "provider-key")
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models: []
providers:
  - id: runpod-pod
    api_key: worker-key
    management:
      kind: runpod-pod
      name: comfy-control
    startup_timeout: 0
""".lstrip()
    )
    app = create_app(configured)
    controller = app.state.controller
    runtime = controller.providers["runpod-pod"]
    controller.store.save_provider_resource("runpod-pod", "pod-old")
    controller.check_ready = AsyncMock(side_effect=[False, False])
    request = httpx.Request("POST", "https://rest.runpod.io/v1/pods/pod-old/start")
    response = httpx.Response(500, request=request)
    controller.action = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "host capacity unavailable", request=request, response=response
        )
    )
    adapter = AsyncMock()
    adapter.recover_start.return_value = StartRecovery("pod-new", "pod-old")
    adapter.terminate.return_value = httpx.Response(204)
    monkeypatch.setattr("control.service.provider_adapter", lambda _: adapter)

    with pytest.raises(TimeoutError, match="did not become ready"):
        await controller.ensure_ready(runtime, "request-1")

    assert controller.store.provider_resource("runpod-pod") == "pod-old"
    adapter.terminate.assert_awaited_once_with(
        controller.lifecycle_client,
        runtime.config,
        controller.preferences,
        "pod-new",
    )
    await controller.close()


@pytest.mark.asyncio
async def test_existing_managed_provider_waits_for_readiness(tmp_path):
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models: []
providers:
  - id: modal
    api_key: worker-key
    management:
      function: serve
      kind: modal
      name: comfy-control
    startup_timeout: 10
""".lstrip()
    )
    app = create_app(configured)
    controller = app.state.controller
    runtime = controller.providers["modal"]
    controller.store.save_provider_resource("modal", "comfy-control")
    controller.check_ready = AsyncMock(side_effect=[False, False, True])
    controller.action = AsyncMock()

    await controller.ensure_ready(runtime, "request-1")

    controller.action.assert_not_awaited()
    assert controller.check_ready.await_count == 3
    await controller.close()


def test_usage_normalisers():
    assert usage_with_resource_cost(
        {
            "status": "ok",
            "metrics": [{"label": "Credit balance", "unit": "USD", "value": 25}],
        },
        {"details": {"dph_total": 0.42}},
    ) == {
        "status": "ok",
        "metrics": [
            {"label": "Credit balance", "unit": "USD", "value": 25},
            {"label": "Running cost", "unit": "USD/hour", "value": 0.42},
        ],
    }
    assert normalise_usage("runpod", [{"amount": 1.25}, {"amount": 2}])[0] == {
        "label": "Spend",
        "unit": "USD",
        "value": 3.25,
    }
    assert normalise_usage("runpod", {"data": {"myself": {"credit": 42}}}) == [
        {"label": "Credit", "unit": "USD", "value": 42}
    ]
    assert normalise_usage("vast", {"credit": 25}) == [
        {"label": "Credit", "unit": "USD", "value": 25}
    ]
    assert normalise_usage("vast", {"balance": 0, "credit": 25, "total_spend": 7}) == [
        {"label": "Credit", "unit": "USD", "value": 25},
        {"label": "Total spend", "unit": "USD", "value": 7},
    ]
    assert normalise_usage(
        "cliproxyapi",
        {"failed_requests": 1, "successful_requests": 2, "total_requests": 3},
    ) == [
        {"label": "Requests", "value": 3},
        {"label": "Successful", "value": 2},
        {"label": "Failed", "value": 1},
    ]
    assert normalise_usage(
        "salad",
        {
            "container_groups_quotas": {
                "container_replicas_quota": 10,
                "container_replicas_used": 2,
            }
        },
    ) == [
        {
            "detail": "8 Of 10 Available",
            "label": "Replica capacity remaining",
            "unit": "%",
            "value": 80.0,
        }
    ]
    assert normalise_xai_quota(
        {
            "config": {
                "creditUsagePercent": 25,
                "monthlyLimit": {"val": 5000},
                "onDemandCap": {"val": 2000},
                "onDemandUsed": {"val": 500},
                "productUsage": [
                    {"product": "GrokBuild", "usagePercent": 10},
                    {"product": "GrokChat", "usagePercent": 20},
                    {"product": "GrokImagine", "usagePercent": 40},
                    {"product": "MonthlyIncluded", "usagePercent": 50},
                ],
                "used": {"val": 1250},
            }
        },
        1,
    ) == [
        {
            "detail": "Account 1",
            "label": "Weekly remaining",
            "unit": "%",
            "value": 75,
        },
        {
            "detail": "Account 1",
            "label": "Grok Build remaining",
            "unit": "%",
            "value": 90,
        },
        {
            "detail": "Account 1",
            "label": "Grok Chat remaining",
            "unit": "%",
            "value": 80,
        },
        {
            "detail": "Account 1",
            "label": "Grok Imagine remaining",
            "unit": "%",
            "value": 60,
        },
    ]


def test_history_parameters_omit_embedded_media_and_secrets():
    assert history_parameters(
        {
            "api_key": "secret",
            "image": "data:image/png;base64,aW1hZ2U=",
            "prompt": "test",
        }
    ) == {
        "api_key": "***",
        "image": "<embedded media omitted: 30 characters>",
        "prompt": "test",
    }


def test_media_library_fuzzy_search_filters_and_lineage(tmp_path: Path):
    store = ControlStore(tmp_path / "control.db")
    store.save_history(
        "image_1",
        "image_generation",
        "public/image",
        '{"prompt":"A wombat operating a GPU server","seed":42,"steps":8}',
    )
    source = tmp_path / "source.png"
    source.write_bytes(b"source-image")
    output = tmp_path / "output.png"
    output.write_bytes(b"output-image")
    source_id = store.save_input_media(
        "image_1",
        "image/png",
        "source.png",
        source,
        source.stat().st_size,
        field_name="image",
    )
    store.save_media(
        "image_1",
        "image/png",
        "output.png",
        output,
        output.stat().st_size,
    )

    result = store.media_library(
        query="wombt GPU",
        filters=[{"operator": "equals", "path": "seed", "value": 42}],
    )
    history_result = store.media_library(
        filters=[{"operator": "equals", "path": "history_id", "value": "image_1"}]
    )
    lineage = store.media_lineage(source_id)

    assert result["count"] == 1
    assert result["data"][0]["parameters"]["steps"] == 8
    assert history_result["count"] == 1
    assert [item["filename"] for item in lineage["derivatives"]] == ["output.png"]
    store.close()


def test_media_metadata_and_schema_migrations_are_persisted(tmp_path: Path):
    store = ControlStore(tmp_path / "control.db")
    store.save_history("image_1", "image_generation", "public/image", "{}")
    image = tmp_path / "dimensions.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\0" * 8
        + (640).to_bytes(4, "big")
        + (480).to_bytes(4, "big")
    )
    store.save_media(
        "image_1", "image/png", "dimensions.png", image, image.stat().st_size
    )
    item = store.media_library()["data"][0]
    versions = store.connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()

    assert (item["width"], item["height"]) == (640, 480)
    assert [row["version"] for row in versions] == [1, 2, 3, 4, 5, 6, 7]
    store.close()


def test_provider_identifiers_are_migrated_to_current_names(tmp_path: Path):
    database = tmp_path / "control.db"
    store = ControlStore(database)
    with store.connection:
        store.connection.execute("DELETE FROM schema_migrations WHERE version >= 3")
        store.connection.execute(
            "INSERT INTO history (id, operation, model, provider, provider_model, "
            "status, created_at, updated_at, parameters_json, error) "
            "VALUES ('old', 'image_generation', 'image-generation', "
            "'modal-serverless', 'worker/image', 'completed', 1, 2, ?, NULL)",
            ('{"provider":"modal-serverless"}',),
        )
        store.connection.execute(
            "INSERT INTO provider_attempts "
            "(history_id, provider, model, started_at, finished_at, status, error) "
            "VALUES ('old', 'modal-serverless', 'worker/image', 1, 2, "
            "'completed', NULL)"
        )
        store.connection.execute(
            "INSERT INTO provider_resources (provider, resource_id, updated_at) "
            "VALUES ('runpod-serverless', 'endpoint', 1)"
        )
        store.connection.execute(
            "INSERT INTO generation_parameters "
            "(history_id, path, position, value_type, text_value) "
            "VALUES ('old', 'provider', 0, 'text', 'modal-serverless')"
        )
        store.connection.execute(
            "INSERT INTO control_configuration "
            "(id, revision, document_json, updated_at) VALUES (1, 1, ?, 1)",
            (
                json.dumps(
                    {
                        "comfy_ui_password": "old-password",
                        "comfy_ui_username": "old-user",
                        "control_maximum_request_bytes": 2 * 1024 * 1024,
                        "local_pod_url": "http://local",
                        "maximum_pending_generations": 7,
                        "maximum_request_bytes": 4 * 1024 * 1024,
                        "public_base_url": "https://old.example",
                        "request_timeout": 12,
                        "workflow_timeout": 34,
                        "routes": {
                            "image-generation": [
                                "local-pod",
                                "runpod-serverless",
                            ]
                        },
                    }
                ),
            ),
        )
    store.close()

    migrated = ControlStore(database)
    history = migrated.connection.execute(
        "SELECT provider, parameters_json FROM history WHERE id = 'old'"
    ).fetchone()
    attempt = migrated.connection.execute(
        "SELECT provider FROM provider_attempts WHERE history_id = 'old'"
    ).fetchone()
    resources = migrated.connection.execute(
        "SELECT provider FROM provider_resources"
    ).fetchall()
    parameter = migrated.connection.execute(
        "SELECT text_value FROM generation_parameters WHERE history_id = 'old'"
    ).fetchone()
    document = json.loads(
        migrated.connection.execute(
            "SELECT document_json FROM control_configuration WHERE id = 1"
        ).fetchone()["document_json"]
    )

    assert history["provider"] == "modal"
    assert json.loads(history["parameters_json"])["provider"] == "modal"
    assert attempt["provider"] == "modal"
    assert [row["provider"] for row in resources] == ["runpod"]
    assert parameter["text_value"] == "modal"
    assert document["routes"]["images"] == [
        {"model": "flux-2-klein-9b", "provider": "runpod"}
    ]
    assert document["routes"]["videos"] == []
    assert document["comfyui_request_timeout"] == 12
    assert document["generation_queue_limit"] == 7
    assert document["generation_timeout"] == 34
    assert document["maximum_request_mib"] == 2
    assert "comfy_ui_password" not in document
    assert "comfy_ui_username" not in document
    assert "local_pod_url" not in document
    assert "public_base_url" not in document
    migrated.close()


def test_historical_routes_and_unique_input_lineage_are_reconciled(tmp_path: Path):
    configured = settings(tmp_path)
    first = create_app(configured)
    store = first.state.controller.store
    store.save_history(
        "source",
        "image_generation",
        "public/image",
        '{"model":"public/image","prompt":"source"}',
    )
    store.update_history(
        "source",
        "completed",
        provider="worker",
        provider_model="worker/image",
    )
    source = tmp_path / "source.png"
    source.write_bytes(b"unique-source")
    source_id = store.save_media(
        "source", "image/png", "source.png", source, source.stat().st_size
    )
    store.save_history(
        "edit",
        "image_edit",
        "public/image-edit",
        json.dumps(
            {
                "input_media": [
                    {
                        "content_type": "image/png",
                        "filename": "reference.png",
                        "size": source.stat().st_size,
                    }
                ],
                "model": "worker/worker/image-edit",
                "provider": "worker",
                "prompt": "edit",
            }
        ),
    )
    store.update_history("edit", "completed", provider="worker")
    output = tmp_path / "output.png"
    output.write_bytes(b"edited-output-with-a-different-size")
    output_id = store.save_media(
        "edit", "image/png", "output.png", output, output.stat().st_size
    )
    store.close()

    second = create_app(configured)
    history = next(
        item
        for item in second.state.controller.store.histories()
        if item["id"] == "edit"
    )

    assert history["model"] == "worker/worker/image-edit"
    assert history["provider_model"] == "worker/image-edit"
    assert history["attempts"][0]["model"] == "worker/image-edit"
    assert second.state.controller.store.media_lineage(output_id)["sources"] == [
        {
            "content_type": "image/png",
            "filename": "reference.png",
            "history_id": "edit",
            "id": source_id,
        }
    ]
    second.state.controller.store.close()


def test_interrupted_image_requests_are_failed_on_restart(tmp_path: Path):
    configured = settings(tmp_path)
    first = create_app(configured)
    store = first.state.controller.store
    store.save_history(
        "interrupted",
        "image_generation",
        "public/image",
        '{"model":"public/image","prompt":"interrupted"}',
    )
    store.update_history("interrupted", "in_progress", provider="worker")
    store.start_attempt("interrupted", "worker", "worker/image")
    store.close()

    second = create_app(configured)
    history = next(
        item
        for item in second.state.controller.store.histories()
        if item["id"] == "interrupted"
    )

    assert history["status"] == "failed"
    assert history["error"] == "controller restarted before the request completed"
    assert history["attempts"][0]["status"] == "failed"
    assert history["attempts"][0]["finished_at"] is not None
    second.state.controller.store.close()


def test_historical_worker_model_maps_to_selected_fallback_provider(tmp_path: Path):
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models:
  - id: public/video
    operation: video_generation
    targets:
      - { model: local/minimax, provider: local }
      - { model: proxy/grok, provider: proxy }
providers:
  - id: local
    api_key: local-key
    base_url: http://local
  - id: proxy
    api_key: proxy-key
    base_url: http://proxy
    type: proxy
""".lstrip()
    )
    first = create_app(configured)
    first.state.controller.store.save_history(
        "legacy-video",
        "video_generation",
        "local/minimax",
        '{"model":"local/minimax","provider":"proxy"}',
    )
    first.state.controller.store.update_history(
        "legacy-video", "completed", provider="proxy"
    )
    first.state.controller.store.close()

    second = create_app(configured)
    history = second.state.controller.store.histories()[0]
    _, direct_targets = second.state.controller.resolve_model(
        "proxy/grok", "video_generation", "proxy"
    )

    assert history["model"] == "local/minimax"
    assert history["provider"] == "proxy"
    assert history["provider_model"] == "proxy/grok"
    assert history["attempts"][0]["model"] == "proxy/grok"
    assert direct_targets[0].model == "proxy/grok"
    second.state.controller.store.close()


def test_controller_openapi_is_current_and_has_no_legacy_api(tmp_path: Path):
    app = create_app(settings(tmp_path))
    schema = app.openapi()

    assert schema["info"]["version"] == "current"
    assert "/ops/media" in schema["paths"]
    assert schema["paths"]["/ops/model-packages"]["put"]["operationId"] == (
        "set_model_packages"
    )
    assert schema["paths"]["/ops/provider-routes"]["put"]["operationId"] == (
        "set_provider_routes"
    )
    assert not any(path.startswith("/api/") for path in schema["paths"])
    assert schema["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    status_schema = schema["paths"]["/ops/status"]["get"]["responses"]["200"]
    settings_body = schema["paths"]["/ops/settings"]["patch"]["requestBody"]
    test_body = schema["paths"]["/ops/providers/{provider_id}/test"]["post"][
        "requestBody"
    ]
    deployment_body = schema["paths"][
        "/ops/providers/{provider_id}/actions/{action_name}"
    ]["post"]["requestBody"]
    image_create = schema["paths"]["/v1/images/generations"]["post"]
    image_edit = schema["paths"]["/v1/images/edits"]["post"]
    image_upscale = schema["paths"]["/v1/images/upscales"]["post"]
    video_create = schema["paths"]["/v1/videos"]["post"]
    assert status_schema["content"]["application/json"]["schema"]["$ref"].endswith(
        "/OperationsStatus"
    )
    assert settings_body["content"]["application/json"]["schema"]["required"] == [
        "revision",
        "values",
    ]
    assert test_body["content"]["application/json"]["schema"]["required"] == [
        "model",
        "prompt",
    ]
    assert deployment_body["content"]["application/json"]["schema"]["required"] == [
        "option_id"
    ]
    assert image_create["operationId"] == "create_image"
    assert image_create["requestBody"]["content"]["application/json"]["schema"][
        "required"
    ] == ["model", "prompt"]
    assert image_create["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/ImageGenerationResponse")
    assert image_edit["operationId"] == "edit_image"
    assert "multipart/form-data" in image_edit["requestBody"]["content"]
    assert image_upscale["operationId"] == "upscale_image"
    assert "multipart/form-data" in image_upscale["requestBody"]["content"]
    assert set(image_upscale["responses"]["200"]["headers"]) == {
        "x-comfy-duration-seconds",
        "x-comfy-history-id",
        "x-comfy-provider",
    }
    assert video_create["operationId"] == "create_video"
    assert set(video_create["requestBody"]["content"]) == {
        "application/json",
        "multipart/form-data",
    }
    assert schema["paths"]["/v1/models"]["get"]["operationId"] == "list_models"
    assert schema["paths"]["/v1/videos/{job_id}"]["get"]["operationId"] == ("get_video")
    assert schema["paths"]["/ops/history"]["get"]["operationId"] == (
        "generation_history"
    )
    assert (
        schema["paths"]["/ops/history/{history_id}/media/{media_id}"]["get"][
            "operationId"
        ]
        == "generation_media"
    )
