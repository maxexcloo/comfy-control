from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from control.dashboard.auth import ui_authorised, valid_csrf
from control.dashboard.fields import prepare_field, settings_group
from control.dashboard.rendering import render_dashboard
from control.http import error
from control.preferences import ConfigurationConflict

router = APIRouter(tags=["dashboard"])


@router.get("/settings", include_in_schema=False)
async def settings_page(request: Request) -> Response:
    controller = request.app.state.controller
    if not ui_authorised(request, request.app.state.settings):
        return RedirectResponse("/login", status_code=303)
    description = controller.describe_configuration()
    unordered: dict[str, dict[str, list[dict[str, object]]]] = {}
    for field in description["fields"]:
        category, group = settings_group(str(field["name"]))
        if category == "Providers":
            continue
        prepared = prepare_field(field)
        unordered.setdefault(category, {}).setdefault(group, []).append(prepared)
    category_order = {"Display": 0, "Models": 2, "Routing": 1, "Worker": 3}
    categories = {
        category: {
            group: sorted(fields, key=lambda item: str(item["label"]).casefold())
            for group, fields in sorted(groups.items())
        }
        for category, groups in sorted(
            unordered.items(),
            key=lambda item: (category_order.get(item[0], 99), item[0]),
        )
    }
    return render_dashboard(
        request,
        "dashboard_settings.html",
        "settings",
        settings={"categories": categories, "revision": description["revision"]},
    )


@router.post("/settings", include_in_schema=False)
async def save_settings(request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not ui_authorised(request, settings):
        return Response(status_code=401)
    form = await request.form()
    if not valid_csrf(request, settings, str(form.get("csrf_token", ""))):
        return error("invalid CSRF token", 403, "invalid_csrf")
    values: dict[str, object] = {}
    for field in controller.describe_configuration()["fields"]:
        if field.get("locked"):
            continue
        name = str(field["name"])
        raw = str(form.get(name, ""))
        if field.get("secret") and not raw:
            continue
        if field.get("type") == "number":
            values[name] = float(raw) if "." in raw else int(raw)
        elif field.get("type") == "models":
            values[name] = [str(item) for item in form.getlist(name)]
        elif field.get("type") == "list":
            values[name] = [item.strip() for item in raw.split(",") if item.strip()]
        elif field.get("type") == "routes":
            values[name] = json.loads(raw)
        else:
            values[name] = raw
    try:
        await controller.update_preferences(values, int(str(form["revision"])))
    except (ConfigurationConflict, RuntimeError, TypeError, ValueError) as exc:
        return error(str(exc), 400, "invalid_configuration")
    return RedirectResponse("/settings?message=Settings+saved", status_code=303)
