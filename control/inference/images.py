from __future__ import annotations

import json
import secrets
import uuid

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response

from control.contracts import (
    ImageGenerationRequest,
    ImageGenerationResponse,
    ImageUpscaleRequest,
)
from control.dashboard.auth import bearer_authorised
from control.http import (
    RequestBodyTooLarge,
    error,
    exception_message,
    limited_body,
)
from control.inference.common import (
    archived_image_content,
    canonical_parameters,
    internal_image_content,
    normalise_grok_image_options,
)
from control.service import history_parameters, rewrite_json_model

router = APIRouter(prefix="/v1", tags=["inference"])


async def generate_image(request: Request, operation: str, path: str) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not bearer_authorised(request, settings):
        return error("invalid API key", 401, "invalid_api_key")
    try:
        body = await limited_body(request, controller.preferences.maximum_request_bytes)
    except RequestBodyTooLarge:
        return error("request body is too large", 413, "request_too_large")
    try:
        original = json.loads(body)
        if not isinstance(original, dict):
            raise TypeError("request body must be an object")
        original = normalise_grok_image_options(original)
        requested_model = str(original.get("model", ""))
        provider = str(original.get("provider", "")).strip() or None
        _, targets = controller.resolve_model(requested_model, operation, provider)
        if provider is None and len(targets) > 1:
            targets = await controller.economical_targets(targets)
        canonical_parameters(original)
        count = int(original.get("n", 1))
        if count < 1 or count > 4:
            raise ValueError("n must be between 1 and 4")
        if original.get("response_format", "b64_json") not in {"b64_json", "url"}:
            raise ValueError("response_format must be b64_json or url")
    except (AttributeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return error(str(exc), 400, "invalid_request")
    except KeyError as exc:
        return error(str(exc), 404, "model_not_found")
    if original.get("seed") is None:
        original["seed"] = secrets.randbelow(2**63)
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    supplied_history_id = request.headers.get("x-comfy-history-id", "")
    history_id = (
        supplied_history_id
        if supplied_history_id.startswith("image_")
        and supplied_history_id[6:].isalnum()
        and controller.store.history(supplied_history_id) is None
        else f"image_{uuid.uuid4().hex}"
    )
    controller.store.save_history(
        history_id,
        operation,
        requested_model,
        json.dumps(history_parameters(original), separators=(",", ":")),
    )
    controller.store.start_phase(history_id, "queued")
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"authorization", "content-length", "host"}
    }
    failures = []
    forwarded = dict(original)
    forwarded.pop("provider", None)
    forwarded_body = json.dumps(forwarded, separators=(",", ":")).encode()
    for target in targets:
        controller.store.start_phase(
            history_id,
            "waiting_for_capacity",
            {"model": target.model, "provider": target.provider},
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
        try:
            is_proxy = controller.providers[target.provider].config.type == "proxy"
            if not is_proxy:
                parameters = canonical_parameters(forwarded)
                count = int(forwarded.get("n", 1))
                outputs = []
                for index in range(count):
                    indexed = dict(parameters)
                    if indexed.get("seed") is not None:
                        indexed["seed"] = int(indexed["seed"]) + index
                    result = await controller.execute_internal(
                        target,
                        f"{history_id}_{index}",
                        operation,
                        indexed,
                        history_id=history_id,
                    )
                    outputs.extend(result)
                response = None
            else:
                outputs = None
                controller.store.start_phase(history_id, "running")
                response = await controller.forward(
                    target,
                    request.method,
                    path,
                    rewrite_json_model(forwarded_body, target.model),
                    headers,
                    request_id,
                )
        except Exception as exc:  # noqa: BLE001 - provider boundary must record failures
            message = exception_message(exc)
            controller.store.finish_attempt(attempt_id, "failed", message)
            failures.append(f"{target.provider}: {message}")
            controller.store.event(
                "error", message, provider=target.provider, request_id=request_id
            )
            continue
        if response is None or response.is_success:
            controller.store.finish_attempt(attempt_id, "completed")
            controller.store.start_phase(history_id, "archiving")
            if response is not None:
                await controller.media.archive_images(
                    history_id, target.provider, response
                )
            controller.store.update_history(
                history_id, "completed", provider=target.provider
            )
            controller.store.finish_phases(history_id)
            controller.store.event(
                "info",
                f"{operation} completed",
                provider=target.provider,
                request_id=request_id,
            )
            content = (
                archived_image_content(request, controller, history_id, response)
                if response is not None
                else internal_image_content(
                    request,
                    controller,
                    history_id,
                    outputs,
                    str(forwarded.get("response_format", "b64_json")),
                )
            )
            return Response(
                content=content,
                status_code=response.status_code if response is not None else 200,
                media_type=(
                    response.headers.get("content-type")
                    if response is not None
                    else "application/json"
                ),
                headers={
                    "x-comfy-history-id": history_id,
                    "x-comfy-provider": target.provider,
                    "x-request-id": request_id,
                },
            )
        controller.store.finish_attempt(
            attempt_id, "failed", f"HTTP {response.status_code}"
        )
        failures.append(f"{target.provider}: HTTP {response.status_code}")
        if response.status_code < 500 and response.status_code != 429:
            controller.store.update_history(
                history_id,
                "failed",
                provider=target.provider,
                error=f"HTTP {response.status_code}",
            )
            return Response(
                content=response.content,
                status_code=response.status_code,
                media_type=response.headers.get("content-type"),
            )
    failure = "; ".join(failures) or "all providers failed"
    controller.store.update_history(history_id, "failed", error=failure)
    controller.store.finish_phases(history_id)
    return error(failure, 502, "providers_failed")


@router.post(
    "/images/generations",
    operation_id="create_image",
    response_model=ImageGenerationResponse,
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "schema": ImageGenerationRequest.model_json_schema()
                }
            },
            "required": True,
        }
    },
)
async def image_generations(request: Request) -> Response:
    return await generate_image(request, "image_generation", "/v1/images/generations")


@router.post(
    "/images/upscales",
    operation_id="upscale_image",
    response_model=ImageGenerationResponse,
    responses={
        200: {
            "headers": {
                "x-comfy-duration-seconds": {
                    "description": "End-to-end provider attempt duration.",
                    "schema": {"type": "number"},
                },
                "x-comfy-history-id": {"schema": {"type": "string"}},
                "x-comfy-provider": {"schema": {"type": "string"}},
            }
        }
    },
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "allOf": [
                            ImageUpscaleRequest.model_json_schema(),
                            {
                                "type": "object",
                                "properties": {
                                    "image": {"format": "binary", "type": "string"}
                                },
                                "required": ["image"],
                            },
                        ]
                    }
                }
            },
            "required": True,
        }
    },
)
async def image_upscales(request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not bearer_authorised(request, settings):
        return error("invalid API key", 401, "invalid_api_key")
    try:
        await limited_body(request, controller.preferences.maximum_request_bytes)
    except RequestBodyTooLarge:
        return error("request body is too large", 413, "request_too_large")
    try:
        form = await request.form()
        model_id = str(form.get("model", "image-upscale"))
        provider = str(form.get("provider", "")).strip() or None
        _, targets = controller.resolve_model(model_id, "image_upscale", provider)
        if provider is None and len(targets) > 1:
            targets = await controller.economical_targets(targets)
        images = [
            value
            for key, value in form.multi_items()
            if key == "image" and hasattr(value, "read")
        ]
        if len(images) != 1:
            raise ValueError("exactly one image is required")
        image = images[0]
        content = await image.read()
        content_type = image.content_type or "application/octet-stream"
        if not content_type.startswith("image/"):
            raise ValueError("image must use an image media type")
        options = ImageUpscaleRequest.model_validate(
            {
                key: value
                for key, value in form.items()
                if key in ImageUpscaleRequest.model_fields
            }
        )
        parameters = canonical_parameters(options.model_dump())
    except ValueError as exc:
        return error(str(exc), 400, "invalid_request")
    except KeyError as exc:
        return error(str(exc), 404, "model_not_found")

    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    history_id = f"upscale_{uuid.uuid4().hex}"
    filename = image.filename or "input"
    files = [("image", (filename, content, content_type))]
    history = options.model_dump(exclude_none=True)
    history["input_media"] = [
        {
            "content_type": content_type,
            "filename": filename,
            "size": len(content),
        }
    ]
    controller.store.save_history(
        history_id,
        "image_upscale",
        model_id,
        json.dumps(history_parameters(history), separators=(",", ":")),
    )
    controller.store.start_phase(history_id, "queued")
    controller.media.save_input(history_id, content, content_type, filename, "image")
    failures: list[str] = []
    for target in targets:
        controller.store.start_phase(
            history_id,
            "waiting_for_capacity",
            {"model": target.model, "provider": target.provider},
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
        try:
            outputs = await controller.execute_internal(
                target,
                history_id,
                "image_upscale",
                parameters,
                files,
                history_id=history_id,
            )
        except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
            message = exception_message(exc)
            controller.store.finish_attempt(attempt_id, "failed", message)
            failures.append(f"{target.provider}: {message}")
            continue
        controller.store.finish_attempt(attempt_id, "completed")
        controller.store.start_phase(history_id, "archiving")
        controller.store.update_history(
            history_id, "completed", provider=target.provider
        )
        content_body = internal_image_content(
            request,
            controller,
            history_id,
            outputs,
            options.response_format,
        )
        controller.store.finish_phases(history_id)
        attempt = controller.store.attempts(history_id)[-1]
        duration = float(attempt["finished_at"]) - float(attempt["started_at"])
        return Response(
            content=content_body,
            media_type="application/json",
            headers={
                "x-comfy-duration-seconds": f"{duration:.3f}",
                "x-comfy-history-id": history_id,
                "x-comfy-provider": target.provider,
                "x-request-id": request_id,
            },
        )
    failure = "; ".join(failures) or "all providers failed"
    controller.store.update_history(history_id, "failed", error=failure)
    controller.store.finish_phases(history_id)
    return error(failure, 502, "providers_failed")


@router.post(
    "/images/edits",
    operation_id="edit_image",
    response_model=ImageGenerationResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "additionalProperties": True,
                        "type": "object",
                        "properties": {
                            "image": {
                                "format": "binary",
                                "type": "array",
                                "items": {"format": "binary", "type": "string"},
                            },
                            "model": {"type": "string"},
                            "prompt": {"type": "string"},
                            "provider": {"type": "string"},
                        },
                        "required": ["image", "model", "prompt"],
                    }
                }
            },
        }
    },
)
async def image_edits(request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not bearer_authorised(request, settings):
        return error("invalid API key", 401, "invalid_api_key")
    try:
        await limited_body(request, controller.preferences.maximum_request_bytes)
    except RequestBodyTooLarge:
        return error("request body is too large", 413, "request_too_large")
    try:
        form = await request.form()
        model_id = str(form.get("model", ""))
        provider = str(form.get("provider", "")).strip() or None
        _, targets = controller.resolve_model(model_id, "image_edit", provider)
        if provider is None and len(targets) > 1:
            targets = await controller.economical_targets(targets)
    except ValueError as exc:
        return error(str(exc), 400, "invalid_request")
    except KeyError as exc:
        return error(str(exc), 404, "model_not_found")
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    fields: list[tuple[str, str]] = []
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    for key, value in form.multi_items():
        if hasattr(value, "read"):
            files.append(
                (
                    key,
                    (
                        value.filename or "upload",
                        await value.read(),
                        value.content_type or "application/octet-stream",
                    ),
                )
            )
        elif key not in {"model", "provider"}:
            fields.append((key, str(value)))
    normalised_fields = normalise_grok_image_options(
        {"model": model_id, **dict(fields)}
    )
    fields = [
        (key, str(value))
        for key, value in normalised_fields.items()
        if key not in {"model", "provider"}
    ]
    field_values = dict(fields)
    try:
        count = int(field_values.get("n", 1))
        if count < 1 or count > 4:
            raise ValueError("n must be between 1 and 4")
        if field_values.get("response_format", "b64_json") not in {
            "b64_json",
            "url",
        }:
            raise ValueError("response_format must be b64_json or url")
        if field_values.get("size", "") not in {"", "auto"}:
            raise ValueError("size is not supported for image edits")
        canonical_parameters(field_values)
    except (TypeError, ValueError) as exc:
        return error(str(exc), 400, "invalid_request")
    if field_values.get("seed") is None:
        field_values["seed"] = str(secrets.randbelow(2**63))
        fields.append(("seed", field_values["seed"]))
    history_id = f"edit_{uuid.uuid4().hex}"
    parameters = dict(fields)
    parameters["model"] = model_id
    if provider:
        parameters["provider"] = provider
    parameters["input_media"] = [
        {
            "content_type": content_type,
            "filename": filename,
            "size": len(content),
        }
        for _, (filename, content, content_type) in files
    ]
    controller.store.save_history(
        history_id,
        "image_edit",
        model_id,
        json.dumps(history_parameters(parameters), separators=(",", ":")),
    )
    controller.store.start_phase(history_id, "queued")
    for field_name, (filename, content, content_type) in files:
        controller.media.save_input(
            history_id, content, content_type, filename, field_name
        )
    failures: list[str] = []
    for target in targets:
        controller.store.start_phase(
            history_id,
            "waiting_for_capacity",
            {"model": target.model, "provider": target.provider},
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
        encoded = httpx.Request(
            "POST",
            "http://multipart.invalid",
            data=dict(fields) | {"model": target.model},
            files=files,
        )
        try:
            is_proxy = controller.providers[target.provider].config.type == "proxy"
            if not is_proxy:
                parameters = canonical_parameters(field_values)
                outputs = []
                for index in range(count):
                    indexed = dict(parameters)
                    if indexed.get("seed") is not None:
                        indexed["seed"] = int(indexed["seed"]) + index
                    result = await controller.execute_internal(
                        target,
                        f"{history_id}_{index}",
                        "image_edit",
                        indexed,
                        files,
                        history_id=history_id,
                    )
                    outputs.extend(result)
                response = None
            else:
                outputs = None
                controller.store.start_phase(history_id, "running")
                response = await controller.forward(
                    target,
                    "POST",
                    "/v1/images/edits",
                    encoded.read(),
                    {"content-type": encoded.headers["content-type"]},
                    request_id,
                )
        except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
            message = exception_message(exc)
            controller.store.finish_attempt(attempt_id, "failed", message)
            failures.append(f"{target.provider}: {message}")
            continue
        if response is None or response.is_success:
            controller.store.finish_attempt(attempt_id, "completed")
            controller.store.start_phase(history_id, "archiving")
            if response is not None:
                await controller.media.archive_images(
                    history_id, target.provider, response
                )
            controller.store.update_history(
                history_id, "completed", provider=target.provider
            )
            controller.store.finish_phases(history_id)
            content = (
                archived_image_content(request, controller, history_id, response)
                if response is not None
                else internal_image_content(
                    request,
                    controller,
                    history_id,
                    outputs,
                    field_values.get("response_format", "b64_json"),
                )
            )
            return Response(
                content=content,
                status_code=response.status_code if response is not None else 200,
                media_type=(
                    response.headers.get("content-type")
                    if response is not None
                    else "application/json"
                ),
                headers={
                    "x-comfy-history-id": history_id,
                    "x-comfy-provider": target.provider,
                    "x-request-id": request_id,
                },
            )
        controller.store.finish_attempt(
            attempt_id, "failed", f"HTTP {response.status_code}"
        )
        failures.append(f"{target.provider}: HTTP {response.status_code}")
        if response.status_code < 500 and response.status_code != 429:
            controller.store.update_history(
                history_id,
                "failed",
                provider=target.provider,
                error=f"HTTP {response.status_code}",
            )
            return Response(
                content=response.content,
                status_code=response.status_code,
                media_type=response.headers.get("content-type"),
            )
    failure = "; ".join(failures) or "all providers failed"
    controller.store.update_history(history_id, "failed", error=failure)
    controller.store.finish_phases(history_id)
    return error(failure, 502, "providers_failed")
