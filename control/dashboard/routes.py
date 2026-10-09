from __future__ import annotations

import asyncio
import json
import uuid
from importlib import resources
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from control.dashboard.auth import ui_authorised, valid_csrf
from control.dashboard.fields import (
    PROVIDER_SETTINGS_PREFIXES,
    provider_fields,
)
from control.dashboard.rendering import render_dashboard
from control.http import error
from control.preferences import ConfigurationConflict, ControlPreferences
from providers.deployment.common import DeploymentSelection
from providers.registry import provider_panel_url
from providers.telemetry import first_number

DASHBOARD_CSS = (
    resources.files("control.dashboard").joinpath("static", "dashboard.css").read_text()
)
DASHBOARD_JS = (
    resources.files("control.dashboard").joinpath("static", "dashboard.js").read_text()
)
router = APIRouter(tags=["dashboard"])


@router.get("/assets/dashboard.css", include_in_schema=False)
async def dashboard_css() -> Response:
    return Response(
        DASHBOARD_CSS,
        media_type="text/css",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/assets/dashboard.js", include_in_schema=False)
async def dashboard_js() -> Response:
    return Response(
        DASHBOARD_JS,
        media_type="text/javascript",
        headers={"Cache-Control": "no-store"},
    )


def usage_with_resource_cost(
    usage: dict[str, object], status: dict[str, object]
) -> dict[str, object]:
    details = status.get("details")
    hourly = first_number(details, "costPerHr", "dph_total")
    if hourly is None:
        return usage
    metrics = [
        *(usage.get("metrics", []) if isinstance(usage.get("metrics"), list) else []),
        {"label": "Running cost", "unit": "USD/hour", "value": hourly},
    ]
    return {**usage, "metrics": metrics, "status": "ok"}


def resource_specification(
    provider_id: str,
    provider_type: str,
    status: dict[str, object],
    preferences: ControlPreferences,
) -> str:
    details = status.get("details")
    values = details if isinstance(details, dict) else {}
    gpu = values.get("gpuTypeId") or values.get("gpuTypeIds") or values.get("gpu_name")
    if provider_id == "modal":
        gpu = preferences.modal_gpu
    elif provider_id == "salad" and not gpu:
        gpu = preferences.salad_gpu_classes
    if isinstance(gpu, list):
        gpu = " / ".join(str(value) for value in gpu)
    gpu_count = first_number(values, "gpuCount", "num_gpus") or 1
    parts = []
    if gpu:
        prefix = f"{int(gpu_count)}× " if gpu_count > 1 else ""
        parts.append(f"{prefix}{gpu}")
    memory = first_number(values, "gpuMemoryInGb", "gpu_ram")
    if memory is not None:
        memory_gb = memory / 1000 if memory > 1000 else memory
        parts.append(f"{memory_gb:.1f} GB")
    processors = first_number(values, "vcpuCount")
    if processors is not None:
        parts.append(f"{int(processors)} vCPU")
    workers = first_number(values, "workersMax", "max_workers")
    if workers is not None and provider_type == "serverless":
        parts.append(f"Up To {int(workers)} Workers")
    return " · ".join(parts)


@router.get("/providers", include_in_schema=False)
async def providers(request: Request) -> Response:
    controller = request.app.state.controller
    if not ui_authorised(request, request.app.state.settings):
        return RedirectResponse("/login", status_code=303)
    refresh = request.query_params.get("telemetry") == "true"
    if refresh:
        async with controller.configuration_lock:
            usage, statuses = await asyncio.gather(
                controller.usage(), controller.provider_statuses()
            )
    else:
        usage = {name: runtime.usage for name, runtime in controller.providers.items()}
        statuses = {
            name: {
                "panel_url": provider_panel_url(runtime.config, {}, runtime.base_url)
            }
            for name, runtime in controller.providers.items()
        }
    description = controller.describe_configuration()
    items = [
        {
            "actions": [
                {"confirmation": action.confirmation, "name": name}
                for name, action in controller.available_actions(
                    runtime.config.id
                ).items()
            ],
            "configured": True,
            "id": runtime.config.id,
            "panel_url": statuses[runtime.config.id]["panel_url"],
            "platform": runtime.config.platform or runtime.config.id,
            "resource_id": controller.resource_id(runtime.config.id),
            "resource_specification": resource_specification(
                runtime.config.id,
                runtime.config.type,
                statuses[runtime.config.id],
                controller.preferences,
            ),
            "settings": provider_fields(description, runtime.config.id),
            "state": runtime.state,
            "type": runtime.config.type,
            "usage": usage_with_resource_cost(
                usage[runtime.config.id], statuses[runtime.config.id]
            ),
        }
        for runtime in controller.providers.values()
    ] + [
        {
            "actions": [],
            "configured": False,
            "id": provider["id"],
            "panel_url": None,
            "platform": provider["platform"],
            "resource_id": None,
            "resource_specification": "",
            "settings": provider_fields(description, str(provider["id"])),
            "state": "unconfigured",
            "type": provider["type"],
            "usage": {"status": "unconfigured"},
        }
        for provider in controller.available_providers
        if provider["id"] not in controller.providers
    ]
    return render_dashboard(
        request,
        "dashboard_providers.html",
        "providers",
        providers=items,
        refresh=refresh,
        revision=description["revision"],
    )


@router.post("/providers/{provider_id}/settings", include_in_schema=False)
async def save_provider_settings(provider_id: str, request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not ui_authorised(request, settings):
        return Response(status_code=401)
    prefix = PROVIDER_SETTINGS_PREFIXES.get(provider_id)
    if prefix is None:
        return error("unknown provider", 404, "unknown_provider")
    form = await request.form()
    if not valid_csrf(request, settings, str(form.get("csrf_token", ""))):
        return error("invalid CSRF token", 403, "invalid_csrf")
    values: dict[str, object] = {}
    for field in controller.describe_configuration()["fields"]:
        name = str(field["name"])
        if not name.startswith(prefix) or field.get("locked"):
            continue
        raw = str(form.get(name, ""))
        if field.get("secret") and not raw:
            continue
        if field.get("type") == "number":
            values[name] = float(raw) if "." in raw else int(raw)
        elif field.get("type") == "list":
            values[name] = [item.strip() for item in raw.split(",") if item.strip()]
        else:
            values[name] = raw
    try:
        await controller.update_preferences(values, int(str(form["revision"])))
    except (ConfigurationConflict, RuntimeError, TypeError, ValueError) as exc:
        return error(str(exc), 400, "invalid_configuration")
    return RedirectResponse("/providers?message=Settings+Saved", status_code=303)


@router.post("/providers/{provider_id}/actions/{action_name}", include_in_schema=False)
async def provider_action(
    provider_id: str, action_name: str, request: Request
) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not ui_authorised(request, settings):
        return Response(status_code=401)
    form = await request.form()
    if not valid_csrf(request, settings, str(form.get("csrf_token", ""))):
        return error("invalid CSRF token", 403, "invalid_csrf")
    preferences = None
    selection = None
    if action_name == "deploy" and (gpu := str(form.get("gpu", "")).strip()):
        updates: dict[str, object] = {}
        if provider_id == "modal":
            updates["modal_gpu"] = gpu
        elif provider_id in {"runpod", "runpod-pod"}:
            updates["runpod_gpu_types"] = [gpu]
        elif provider_id == "salad":
            updates["salad_gpu_classes"] = [gpu]
        elif provider_id in {"vast", "vast-pod"}:
            memory = str(form.get("memory_gb", "")).strip()
            if memory:
                updates["vast_minimum_gpu_memory_gb"] = max(1, int(float(memory)))
        memory = str(form.get("memory_gb", "")).strip()
        selection = DeploymentSelection(
            memory_gb=float(memory) if memory else None,
            option_id=str(form.get("option_id", "")).strip() or gpu,
            variant=str(form.get("variant", "")).strip() or None,
        )
        if updates:
            preferences = controller.preferences.model_copy(update=updates)
    try:
        await controller.run_provider_action(
            provider_id,
            action_name,
            uuid.uuid4().hex[:16],
            preferences=preferences,
            selection=selection,
        )
    except Exception as exc:  # noqa: BLE001 - provider SDK boundary
        return error(str(exc), 400, "provider_action_failed")
    return RedirectResponse(
        f"/providers?message={quote(f'{provider_id} {action_name} completed'.title())}",
        status_code=303,
    )


@router.get("/providers/{provider_id}/deployment-options", include_in_schema=False)
async def provider_deployment_options(provider_id: str, request: Request) -> Response:
    controller = request.app.state.controller
    if not ui_authorised(request, request.app.state.settings):
        return Response(status_code=401)
    try:
        options = await controller.deployment_options(provider_id)
    except (KeyError, RuntimeError) as exc:
        return error(str(exc), 400, "deployment_options_unavailable")
    return JSONResponse({"options": options, "provider": provider_id})


@router.get("/providers/{provider_id}/logs", include_in_schema=False)
async def provider_logs(provider_id: str, request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not ui_authorised(request, settings):
        return Response(status_code=401)
    try:
        await controller.provider_logs(provider_id, 1)
    except KeyError as exc:
        return error(str(exc), 404, "provider_not_found")

    async def stream():
        previous = ""
        while True:
            payload = json.dumps(
                await controller.provider_logs(provider_id, 500),
                separators=(",", ":"),
            )
            if payload != previous:
                yield f"data: {payload}\n\n"
                previous = payload
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
