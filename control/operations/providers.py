import json
import time
import uuid

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from control.contracts import (
    ProviderActionResult,
    ProviderDeploymentOptions,
    ProviderDeploymentRequest,
    ProviderLogs,
    ProviderTestRequest,
    ProviderTestResult,
)
from control.dashboard.auth import bearer_authorised, ui_authorised
from control.http import (
    RequestBodyTooLarge,
    error,
    exception_message,
    limited_body,
)
from providers.deployment.common import DeploymentSelection

TEST_MAXIMUM_BYTES = 16 * 1024

router = APIRouter(prefix="/ops/providers", tags=["operations"])


@router.get(
    "/{provider_id}/deployment-options",
    operation_id="provider_deployment_options",
    response_model=ProviderDeploymentOptions,
)
async def provider_deployment_options(provider_id: str, request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not (ui_authorised(request, settings) or bearer_authorised(request, settings)):
        return Response(status_code=401)
    try:
        options = await controller.deployment_options(provider_id)
    except KeyError as exc:
        return error(str(exc), 404, "provider_not_found")
    except Exception as exc:  # noqa: BLE001 - provider API boundary
        return error(exception_message(exc), 502, "deployment_options_unavailable")
    return JSONResponse({"options": options, "provider": provider_id})


@router.post(
    "/{provider_id}/actions/{action_name}",
    operation_id="provider_action",
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "schema": ProviderDeploymentRequest.model_json_schema()
                }
            },
            "required": False,
        }
    },
    response_model=ProviderActionResult,
)
async def provider_action(
    provider_id: str, action_name: str, request: Request
) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not (ui_authorised(request, settings) or bearer_authorised(request, settings)):
        return Response(status_code=401)
    expected = f"{provider_id}/{action_name}"
    if request.headers.get("x-comfy-control-action", "") != expected:
        return error("provider action confirmation is missing", 400, "invalid_action")
    request_id = uuid.uuid4().hex[:16]
    controller.store.event(
        "info",
        f"provider action {action_name} started",
        provider=provider_id,
        request_id=request_id,
    )
    try:
        preferences = None
        selection = None
        body = await limited_body(request, TEST_MAXIMUM_BYTES)
        if body:
            deployment = ProviderDeploymentRequest.model_validate_json(body)
            selection = DeploymentSelection(
                memory_gb=deployment.memory_gb,
                option_id=deployment.option_id,
                variant=deployment.variant,
            )
            updates: dict[str, object] = {}
            if provider_id == "modal":
                updates["modal_gpu"] = deployment.option_id
            elif provider_id in {"vast", "vast-pod"} and deployment.memory_gb:
                updates["vast_minimum_gpu_memory_gb"] = max(
                    1, int(deployment.memory_gb)
                )
            if updates:
                preferences = controller.preferences.model_copy(update=updates)
        result = await controller.run_provider_action(
            provider_id,
            action_name,
            request_id,
            preferences=preferences,
            selection=selection,
        )
    except RequestBodyTooLarge:
        return error("request body is too large", 413, "request_too_large")
    except ValidationError as exc:
        return error(str(exc), 400, "invalid_request")
    except KeyError as exc:
        controller.store.event(
            "error",
            f"provider action {action_name} failed: {exception_message(exc)}",
            provider=provider_id,
            request_id=request_id,
        )
        return error(str(exc), 404, "action_not_found")
    except Exception as exc:  # noqa: BLE001 - provider SDK boundary
        controller.store.event(
            "error",
            f"provider action {action_name} failed: {exception_message(exc)}",
            provider=provider_id,
            request_id=request_id,
        )
        return error(exception_message(exc), 502, "action_failed")
    return JSONResponse(result)


@router.get(
    "/{provider_id}/logs", operation_id="provider_logs", response_model=ProviderLogs
)
async def provider_logs(provider_id: str, request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not (ui_authorised(request, settings) or bearer_authorised(request, settings)):
        return Response(status_code=401)
    try:
        limit = min(max(int(request.query_params.get("limit", "200")), 1), 500)
        return JSONResponse(await controller.provider_logs(provider_id, limit))
    except ValueError:
        return error("log limit must be a number", 400, "invalid_request")
    except KeyError as exc:
        return error(str(exc), 404, "provider_not_found")


@router.post(
    "/{provider_id}/test",
    operation_id="test_provider",
    response_model=ProviderTestResult,
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {"schema": ProviderTestRequest.model_json_schema()}
            },
            "required": True,
        }
    },
)
async def provider_test(provider_id: str, request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not (ui_authorised(request, settings) or bearer_authorised(request, settings)):
        return Response(status_code=401)
    try:
        body = await limited_body(request, TEST_MAXIMUM_BYTES)
        values = json.loads(body)
        if not isinstance(values, dict):
            raise TypeError("request body must be an object")
        prompt = str(values.get("prompt", "")).strip()
        if not prompt or len(prompt) > 2000:
            raise ValueError("prompt must contain between 1 and 2000 characters")
        requested_model = str(values.get("model", ""))
        size = str(values.get("size", "512x512"))
        if size not in {"512x512", "768x768", "1024x1024"}:
            raise ValueError("unsupported test size")
        model, targets = controller.resolve_model(
            requested_model, "image_generation", provider_id
        )
    except RequestBodyTooLarge:
        return error("request body is too large", 413, "request_too_large")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return error(str(exc), 400, "invalid_request")
    except KeyError as exc:
        return error(str(exc), 404, "model_not_found")
    target = targets[0]
    request_id = uuid.uuid4().hex[:16]
    history_id = f"test_{uuid.uuid4().hex}"
    parameters = {
        "model": requested_model,
        "prompt": prompt,
        "provider": provider_id,
        "size": size,
    }
    controller.store.save_history(
        history_id,
        "image_generation",
        requested_model,
        json.dumps(parameters, separators=(",", ":")),
    )
    controller.store.update_history(
        history_id,
        "in_progress",
        provider=target.provider,
        provider_model=target.model,
    )
    attempt_id = controller.store.start_attempt(
        history_id, target.provider, target.model
    )
    started = time.monotonic()
    try:
        if controller.providers[target.provider].config.type == "proxy":
            response = await controller.forward(
                target,
                "POST",
                "/v1/images/generations",
                json.dumps(
                    {
                        "model": target.model,
                        "n": 1,
                        "prompt": prompt,
                        "response_format": "b64_json",
                        "size": size,
                    },
                    separators=(",", ":"),
                ).encode(),
                {"content-type": "application/json"},
                request_id,
            )
            if not response.is_success:
                raise RuntimeError(f"provider returned HTTP {response.status_code}")
            await controller.media.archive_images(history_id, target.provider, response)
        else:
            width, height = size.split("x", 1)
            outputs = await controller.execute_internal(
                target,
                history_id,
                "image_generation",
                {"height": int(height), "prompt": prompt, "width": int(width)},
            )
            for content, content_type, filename in outputs:
                controller.media.save(history_id, content, content_type, filename)
        controller.store.finish_attempt(attempt_id, "completed")
        controller.store.update_history(
            history_id, "completed", provider=target.provider
        )
        controller.store.event(
            "info",
            "dashboard test completed",
            provider=target.provider,
            request_id=request_id,
        )
    except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
        message = exception_message(exc)
        controller.store.finish_attempt(attempt_id, "failed", message)
        controller.store.update_history(history_id, "failed", error=message)
        controller.store.event(
            "error", message, provider=target.provider, request_id=request_id
        )
        return error(message, 502, "test_failed")
    media = controller.store.media_for_history(history_id)
    return JSONResponse(
        {
            "duration_seconds": round(time.monotonic() - started, 2),
            "history_id": history_id,
            "media": [
                {
                    "content_type": item.content_type,
                    "filename": item.filename,
                    "id": item.id,
                    "size": item.size,
                }
                for item in media
            ],
            "model": model.id,
            "provider": target.provider,
            "status": "completed",
        }
    )
