from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from control.contracts import OperationsStatus
from control.dashboard.auth import bearer_authorised, ui_authorised
from control.http import error

PAGE_SIZE = 20

router = APIRouter(prefix="/ops", tags=["operations"])


@router.get(
    "/status", operation_id="operations_status", response_model=OperationsStatus
)
async def operations_status(request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not (ui_authorised(request, settings) or bearer_authorised(request, settings)):
        return Response(status_code=401)
    try:
        event_page = max(1, int(request.query_params.get("event_page", "1")))
        history_page = max(1, int(request.query_params.get("history_page", "1")))
    except ValueError:
        return error("page must be a number", 400, "invalid_request")
    event_count = controller.store.event_count()
    history_count = controller.store.history_count()
    async with controller.configuration_lock:
        usage, provider_statuses = await asyncio.gather(
            controller.usage(), controller.provider_statuses()
        )
    return JSONResponse(
        {
            "providers": [
                {
                    "id": runtime.config.id,
                    "configured": True,
                    "platform": runtime.config.platform,
                    "type": runtime.config.type,
                    "usage": usage[runtime.config.id],
                    "details": provider_statuses[runtime.config.id]["details"],
                    "error": provider_statuses[runtime.config.id].get("error"),
                    "panel_url": provider_statuses[runtime.config.id]["panel_url"],
                    "resource_id": controller.resource_id(runtime.config.id),
                    "state": runtime.state,
                    "active_requests": runtime.active_requests,
                    "actions": [
                        {
                            "confirmation": action.confirmation,
                            "name": name,
                        }
                        for name, action in controller.available_actions(
                            runtime.config.id
                        ).items()
                    ],
                    "idle_seconds": runtime.config.idle_seconds,
                    "models": sorted(
                        model.id
                        for model in controller.config.models
                        if model.operation == "image_generation"
                        and any(
                            target.provider == runtime.config.id
                            for target in model.targets
                        )
                    ),
                }
                for runtime in controller.providers.values()
            ]
            + [
                {
                    "active_requests": 0,
                    "configured": False,
                    "error": None,
                    "id": provider["id"],
                    "idle_seconds": 0,
                    "panel_url": None,
                    "platform": provider["platform"],
                    "resource_id": None,
                    "state": "not-configured",
                    "type": provider["type"],
                    "actions": [],
                    "details": {},
                    "models": [],
                    "usage": {"status": "unconfigured"},
                }
                for provider in controller.available_providers
                if provider["id"] not in controller.providers
            ],
            "history": controller.store.histories(
                PAGE_SIZE, (history_page - 1) * PAGE_SIZE
            ),
            "history_pagination": {
                "count": history_count,
                "page": history_page,
                "pages": max(1, (history_count + PAGE_SIZE - 1) // PAGE_SIZE),
            },
            "jobs": controller.store.jobs(50),
            "events": controller.store.events(PAGE_SIZE, (event_page - 1) * PAGE_SIZE),
            "event_pagination": {
                "count": event_count,
                "page": event_page,
                "pages": max(1, (event_count + PAGE_SIZE - 1) // PAGE_SIZE),
            },
        }
    )
