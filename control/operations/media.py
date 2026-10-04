from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from control.config import ControlSettings
from control.contracts import MediaLineage, MediaSearch
from control.dashboard.auth import bearer_authorised
from control.http import error
from control.service import Controller

router = APIRouter(prefix="/ops/media", tags=["operations"])


def dependencies(request: Request) -> tuple[Controller, ControlSettings]:
    return request.app.state.controller, request.app.state.settings


@router.get("", operation_id="search_media", response_model=MediaSearch)
async def search_media(request: Request) -> Response:
    controller, settings = dependencies(request)
    if not bearer_authorised(request, settings):
        return Response(status_code=401)
    try:
        filters = []
        for name in ("model", "operation", "provider", "status"):
            if value := request.query_params.get(name):
                filters.append({"path": name, "operator": "equals", "value": value})
        for value in request.query_params.getlist("filter"):
            path, operator, raw = value.split("|", 2)
            try:
                parsed: object = float(raw)
            except ValueError:
                parsed = raw
            filters.append({"path": path, "operator": operator, "value": parsed})
        result = controller.store.media_library(
            query=request.query_params.get("q", ""),
            filters=filters,
            include_inputs=request.query_params.get("include_inputs") == "true",
            sort=request.query_params.get("sort", "newest"),
            limit=min(max(int(request.query_params.get("limit", "50")), 1), 500),
            offset=max(int(request.query_params.get("offset", "0")), 0),
        )
    except ValueError as exc:
        return error(str(exc), 400, "invalid_filter")
    return JSONResponse(result)


@router.get("/facets", operation_id="media_facets")
async def media_facets(request: Request) -> Response:
    controller, settings = dependencies(request)
    if not bearer_authorised(request, settings):
        return Response(status_code=401)
    return JSONResponse(controller.store.media_facets())


@router.get(
    "/{asset_id}/lineage", operation_id="media_lineage", response_model=MediaLineage
)
async def media_lineage(asset_id: int, request: Request) -> Response:
    controller, settings = dependencies(request)
    if not bearer_authorised(request, settings):
        return Response(status_code=401)
    if controller.store.media_asset(asset_id) is None:
        return error("media was not found", 404, "not_found")
    return JSONResponse(controller.store.media_lineage(asset_id))


@router.get("/{asset_id}", operation_id="media_detail")
async def media_detail(asset_id: int, request: Request) -> Response:
    controller, settings = dependencies(request)
    if not bearer_authorised(request, settings):
        return Response(status_code=401)
    detail = controller.store.media_detail(asset_id)
    if detail is None:
        return error("media was not found", 404, "not_found")
    return JSONResponse(detail)


@router.get("/{asset_id}/content", operation_id="media_content")
async def media_content(asset_id: int, request: Request) -> Response:
    controller, settings = dependencies(request)
    if not bearer_authorised(request, settings):
        return Response(status_code=401)
    asset = controller.store.media_asset(asset_id)
    if asset is None or not Path(asset.path).is_file():
        return error("media was not found", 404, "not_found")
    return FileResponse(
        asset.path,
        media_type=asset.content_type,
        headers={"X-Content-Type-Options": "nosniff"},
    )
