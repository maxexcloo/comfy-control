from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from control.contracts import InferenceModelList
from control.dashboard.auth import bearer_authorised
from control.http import error

router = APIRouter(prefix="/v1", tags=["inference"])


@router.get("/models", operation_id="list_models", response_model=InferenceModelList)
async def models(request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not bearer_authorised(request, settings):
        return error("invalid API key", 401, "invalid_api_key")
    model_ids = {model.id for model in controller.config.models}
    for model in controller.config.models:
        for target in model.targets:
            provider = controller.providers[target.provider].config
            for provider_name in (provider.id, *provider.aliases):
                model_ids.add(f"{provider_name}/{target.model}")
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {
                    "created": 0,
                    "id": model_id,
                    "object": "model",
                    "owned_by": "comfy-control",
                }
                for model_id in sorted(model_ids)
            ],
        }
    )
