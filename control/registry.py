from __future__ import annotations

from collections.abc import Mapping

import yaml

from catalogue.profiles import catalogue_root
from control.config import (
    ControlFile,
    Provider,
    ProviderAction,
    ProviderLifecycle,
    ProviderManagement,
    RoutedModel,
    Target,
    UsageProbe,
)

PROVIDER_CATALOGUE = (
    {"id": "cliproxyapi", "platform": "CLI Proxy API", "type": "proxy"},
    {"id": "modal", "platform": "Modal", "type": "serverless"},
    {"id": "runpod", "platform": "RunPod", "type": "serverless"},
    {"id": "runpod-pod", "platform": "RunPod (Pod)", "type": "pod"},
    {"id": "salad", "platform": "SaladCloud", "type": "serverless"},
    {"id": "vast", "platform": "Vast", "type": "serverless"},
    {"id": "vast-pod", "platform": "Vast (Pod)", "type": "pod"},
)

MODEL_ROUTES = {
    "image-edit": (
        "image_edit",
        (
            "modal",
            "runpod-pod",
            "runpod",
            "salad",
            "vast-pod",
            "vast",
        ),
    ),
    "image-generation": (
        "image_generation",
        (
            "modal",
            "runpod-pod",
            "runpod",
            "salad",
            "vast-pod",
            "vast",
        ),
    ),
    "image-to-video": (
        "video_generation",
        (
            "modal",
            "runpod-pod",
            "runpod",
            "salad",
            "vast-pod",
            "vast",
        ),
    ),
    "image-upscale": (
        "image_upscale",
        (
            "modal",
            "runpod-pod",
            "runpod",
            "salad",
            "vast-pod",
            "vast",
        ),
    ),
    "text-to-video": (
        "video_generation",
        (
            "modal",
            "runpod-pod",
            "runpod",
            "salad",
            "vast-pod",
            "vast",
        ),
    ),
}


def catalogue_model_packages() -> tuple[
    dict[str, dict[str, str]], dict[tuple[str, str], list[str]]
]:
    packages: dict[str, dict[str, str]] = {}
    aliases: dict[tuple[str, str], list[str]] = {}
    for path in sorted(catalogue_root().glob("*/model.yaml")):
        value = yaml.safe_load(path.read_text())
        if not isinstance(value, dict):
            continue
        profile = str(value.get("profile", ""))
        model_id = str(value.get("id", ""))
        operation = str(value.get("operation", ""))
        input_map = value.get("input_map")
        has_image = isinstance(input_map, dict) and "image" in input_map
        operation_id = {
            "image_edit": "image-edit",
            "image_generation": "image-generation",
            "image_upscale": "image-upscale",
            "video_generation": ("image-to-video" if has_image else "text-to-video"),
        }.get(operation)
        if (
            not profile
            or profile == "image-upscale"
            or not model_id
            or operation_id is None
        ):
            continue
        packages.setdefault(profile, {})[operation_id] = model_id
        aliases[(profile, operation_id)] = [
            str(alias) for alias in value.get("aliases", [])
        ]
    for package in ("flux-2-klein-9b", "krea-2-turbo"):
        packages.setdefault(package, {})["image-upscale"] = (
            "image-upscale/realesrgan-x4plus"
        )
    return packages, aliases


MODEL_PACKAGES, MODEL_ALIASES = catalogue_model_packages()
MODEL_PACKAGES["grok-imagine"] = {
    "image-edit": "grok-imagine-image-quality",
    "image-generation": "grok-imagine-image-quality",
    "image-to-video": "grok-imagine-video-1.5",
    "text-to-video": "grok-imagine-video-1.5",
}
PROVIDER_MODEL_PACKAGES = {
    "cliproxyapi": ("grok-imagine",),
    "modal": ("flux-2-klein-9b", "krea-2-turbo", "minimax-h3"),
    "runpod": ("flux-2-klein-9b", "krea-2-turbo", "minimax-h3"),
    "runpod-pod": ("flux-2-klein-9b", "krea-2-turbo", "minimax-h3"),
    "salad": ("flux-2-klein-9b", "krea-2-turbo", "minimax-h3"),
    "vast": ("flux-2-klein-9b", "krea-2-turbo", "minimax-h3"),
    "vast-pod": ("flux-2-klein-9b", "krea-2-turbo", "minimax-h3"),
}
ROUTE_FAMILIES = {
    "image-edit": "images",
    "image-generation": "images",
    "image-to-video": "videos",
    "image-upscale": "images",
    "text-to-video": "videos",
}


def bearer(name: str, environment: Mapping[str, str]) -> dict[str, str]:
    return {"authorization": f"Bearer {environment[name]}"}


def internal_action(
    name: str, confirmation: str | None = None, resource_id_path: str | None = None
) -> ProviderAction:
    return ProviderAction(
        confirmation=confirmation, internal=name, resource_id_path=resource_id_path
    )


def managed_provider(
    *,
    api_key: str,
    identifier: str,
    kind: str,
    name: str,
    platform: str,
    provider_type: str,
    idle_seconds: int,
    aliases: list[str] | None = None,
    function: str | None = None,
    usage: UsageProbe | None = None,
    actions: dict[str, ProviderAction] | None = None,
    lifecycle: ProviderLifecycle | None = None,
) -> Provider:
    return Provider.model_validate(
        {
            "actions": actions or {},
            "aliases": aliases or [],
            "api_key": api_key,
            "id": identifier,
            "idle_seconds": idle_seconds,
            "lifecycle": lifecycle or ProviderLifecycle(),
            "management": ProviderManagement(function=function, kind=kind, name=name),
            "platform": platform,
            "type": provider_type,
            "usage": usage,
        }
    )


def providers(environment: Mapping[str, str]) -> list[Provider]:
    configured: list[Provider] = []
    worker_key = environment.get("WORKER_API_KEY", "")
    if environment.get("CLIPROXY_API_KEY") and environment.get("CLIPROXY_URL"):
        headers = (
            bearer("CLIPROXY_MANAGEMENT_KEY", environment)
            if environment.get("CLIPROXY_MANAGEMENT_KEY")
            else {}
        )
        configured.append(
            Provider(
                aliases=["cliproxy"],
                api_key=environment["CLIPROXY_API_KEY"],
                base_url=environment["CLIPROXY_URL"],
                health_path="/v1/models",
                id="cliproxyapi",
                idle_seconds=0,
                platform="CLI Proxy API",
                type="proxy",
                usage=UsageProbe(
                    headers=headers,
                    kind="cliproxyapi",
                    url=f"{environment['CLIPROXY_URL']}/v0/management/usage-statistics-enabled",
                ),
            )
        )
    if environment.get("MODAL_TOKEN_ID") and worker_key:
        configured.append(
            managed_provider(
                actions={
                    "deploy": internal_action("modal-deploy"),
                    "terminate": internal_action(
                        "modal-terminate",
                        "Terminate the Modal deployment? Its volume will be preserved.",
                    ),
                },
                api_key=worker_key,
                function="serve",
                identifier="modal",
                idle_seconds=0,
                kind="modal",
                name="comfy-control",
                platform="Modal",
                provider_type="serverless",
                usage=UsageProbe(kind="modal"),
            )
        )
    if environment.get("RUNPOD_API_KEY") and worker_key:
        headers = bearer("RUNPOD_API_KEY", environment)
        configured.extend(
            [
                managed_provider(
                    actions={
                        "deploy": internal_action(
                            "provider-deploy", resource_id_path="id"
                        ),
                        "terminate": internal_action(
                            "provider-terminate",
                            "Terminate the RunPod Pod? Its Pod volume will be deleted.",
                        ),
                    },
                    api_key=worker_key,
                    identifier="runpod-pod",
                    idle_seconds=900,
                    kind="runpod-pod",
                    lifecycle=ProviderLifecycle(
                        start=ProviderAction(
                            headers=headers,
                            url="https://rest.runpod.io/v1/pods/{resource_id}/start",
                        ),
                        stop=ProviderAction(
                            confirmation="Stop the RunPod Pod?",
                            headers=headers,
                            url="https://rest.runpod.io/v1/pods/{resource_id}/stop",
                        ),
                    ),
                    name="comfy-control",
                    platform="RunPod",
                    provider_type="pod",
                    usage=UsageProbe(
                        headers=headers,
                        kind="runpod",
                        url="https://api.runpod.io/graphql",
                    ),
                ),
                managed_provider(
                    actions={
                        "deploy": internal_action(
                            "provider-deploy", resource_id_path="id"
                        ),
                        "scale-down": ProviderAction(
                            headers=headers,
                            json={"workersMin": 0},
                            method="PATCH",
                            url="https://rest.runpod.io/v1/endpoints/{resource_id}",
                        ),
                        "scale-up": ProviderAction(
                            headers=headers,
                            json={"workersMin": 1},
                            method="PATCH",
                            url="https://rest.runpod.io/v1/endpoints/{resource_id}",
                        ),
                        "terminate": internal_action(
                            "provider-terminate",
                            "Terminate the RunPod Serverless endpoint and its workers?",
                        ),
                    },
                    api_key=worker_key,
                    identifier="runpod",
                    idle_seconds=0,
                    kind="runpod",
                    name="comfy-control",
                    platform="RunPod",
                    provider_type="serverless",
                    usage=UsageProbe(
                        headers=headers,
                        kind="runpod",
                        url="https://api.runpod.io/graphql",
                    ),
                ),
            ]
        )
    if environment.get("SALAD_API_KEY") and worker_key:
        configured.append(
            managed_provider(
                actions={
                    "deploy": internal_action("provider-deploy", resource_id_path="id"),
                    "terminate": internal_action(
                        "provider-terminate",
                        "Terminate the SaladCloud container group?",
                    ),
                },
                api_key=worker_key,
                identifier="salad",
                idle_seconds=900,
                kind="salad",
                name="comfy-control",
                platform="SaladCloud",
                provider_type="serverless",
                usage=UsageProbe(kind="salad"),
            )
        )
    if environment.get("VAST_API_KEY") and worker_key:
        headers = bearer("VAST_API_KEY", environment)
        usage = UsageProbe(
            headers=headers,
            kind="vast",
            url="https://console.vast.ai/api/v0/users/current/",
        )
        configured.extend(
            [
                managed_provider(
                    actions={
                        "deploy": internal_action(
                            "provider-deploy", resource_id_path="new_contract"
                        ),
                        "terminate": internal_action(
                            "provider-terminate",
                            "Terminate the Vast.ai Pod? Its instance disk will be deleted.",
                        ),
                    },
                    api_key=worker_key,
                    identifier="vast-pod",
                    idle_seconds=900,
                    kind="vast-pod",
                    lifecycle=ProviderLifecycle(
                        start=ProviderAction(
                            headers=headers,
                            json={"state": "running"},
                            method="PUT",
                            url="https://console.vast.ai/api/v0/instances/{resource_id}/",
                        ),
                        stop=ProviderAction(
                            confirmation="Stop the Vast.ai Pod?",
                            headers=headers,
                            json={"state": "stopped"},
                            method="PUT",
                            url="https://console.vast.ai/api/v0/instances/{resource_id}/",
                        ),
                    ),
                    name="comfy-control-pod",
                    platform="Vast",
                    provider_type="pod",
                    usage=usage,
                ),
                managed_provider(
                    actions={
                        "deploy": internal_action(
                            "provider-deploy", resource_id_path="id"
                        ),
                        "terminate": internal_action(
                            "provider-terminate",
                            "Terminate the Vast.ai Serverless endpoint?",
                        ),
                    },
                    api_key=worker_key,
                    identifier="vast",
                    idle_seconds=0,
                    kind="vast",
                    name="comfy-control-serverless",
                    platform="Vast",
                    provider_type="serverless",
                    usage=usage,
                ),
            ]
        )
    return sorted(configured, key=lambda provider: provider.id)


def routes() -> dict[str, list[dict[str, str]]]:
    return {
        "images": [
            {"model": "flux-2-klein-9b", "provider": provider}
            for provider in MODEL_ROUTES["image-generation"][1]
        ],
        "videos": [
            {"model": "minimax-h3", "provider": provider}
            for provider in MODEL_ROUTES["text-to-video"][1]
        ],
    }


def control_file(
    environment: Mapping[str, str],
    configured_routes: Mapping[str, list[object]] | None = None,
) -> ControlFile:
    configured_providers = providers(environment)
    enabled = {provider.id for provider in configured_providers}
    installed = {
        item.strip()
        for item in environment.get("MODEL_PROFILES", "flux-2-klein-9b").split(",")
        if item.strip()
    }
    selected_routes = routes() if configured_routes is None else configured_routes
    available_packages = set(MODEL_PACKAGES) - {"grok-imagine"}
    unknown_packages = sorted(installed - available_packages)
    if unknown_packages:
        raise ValueError(f"unknown model packages: {', '.join(unknown_packages)}")
    models = []
    for identifier, (operation, _) in MODEL_ROUTES.items():
        targets = []
        for choice in selected_routes.get(ROUTE_FAMILIES[identifier], []):
            if hasattr(choice, "provider") and hasattr(choice, "model"):
                provider = str(choice.provider)
                package = str(choice.model)
            elif isinstance(choice, dict):
                provider = str(choice.get("provider", ""))
                package = str(choice.get("model", ""))
            else:
                continue
            if package not in PROVIDER_MODEL_PACKAGES.get(provider, ()):
                raise ValueError(
                    f"model package {package} is not available on provider {provider}"
                )
            if not any(
                ROUTE_FAMILIES[operation] == ROUTE_FAMILIES[identifier]
                for operation in MODEL_PACKAGES[package]
            ):
                raise ValueError(
                    f"model package {package} does not support "
                    f"{ROUTE_FAMILIES[identifier]}"
                )
            target_model = MODEL_PACKAGES.get(package, {}).get(identifier)
            if (
                provider not in enabled
                or target_model is None
                or (provider != "cliproxyapi" and package not in installed)
            ):
                continue
            target = Target(model=target_model, provider=provider)
            if target not in targets:
                targets.append(target)
        if targets:
            models.append(
                RoutedModel(id=identifier, operation=operation, targets=targets)
            )
    explicit_models = tuple(
        sorted(
            (
                (alias, package, operation_id)
                for (package, operation_id), aliases in MODEL_ALIASES.items()
                for alias in aliases
                if alias != "image-upscale"
            ),
            key=lambda item: item[0],
        )
    )
    for public_id, package, operation_id in explicit_models:
        if package not in installed:
            continue
        operation = MODEL_ROUTES[operation_id][0]
        family = ROUTE_FAMILIES[operation_id]
        target_model = MODEL_PACKAGES[package][operation_id]
        targets = [
            Target(model=target_model, provider=choice.provider)
            for choice in selected_routes.get(family, [])
            if hasattr(choice, "provider")
            and hasattr(choice, "model")
            and choice.model == package
            and choice.provider in enabled
        ]
        if not targets:
            targets = [
                Target(model=target_model, provider=str(choice.get("provider")))
                for choice in selected_routes.get(family, [])
                if isinstance(choice, dict)
                and choice.get("model") == package
                and choice.get("provider") in enabled
            ]
        if targets:
            unique_targets = []
            for target in targets:
                if target not in unique_targets:
                    unique_targets.append(target)
            models.append(
                RoutedModel(
                    id=public_id,
                    operation=operation,
                    targets=unique_targets,
                )
            )
    if "cliproxyapi" in enabled:
        models.extend(
            [
                RoutedModel(
                    id="grok-image",
                    operation="image_generation",
                    targets=[
                        Target(
                            model="grok-imagine-image-quality",
                            provider="cliproxyapi",
                        )
                    ],
                ),
                RoutedModel(
                    id="grok-image-edit",
                    operation="image_edit",
                    targets=[
                        Target(
                            model="grok-imagine-image-quality",
                            provider="cliproxyapi",
                        )
                    ],
                ),
                RoutedModel(
                    id="grok-video",
                    operation="video_generation",
                    targets=[
                        Target(
                            model="grok-imagine-video-1.5",
                            provider="cliproxyapi",
                        )
                    ],
                ),
            ]
        )
    return ControlFile(models=models, providers=configured_providers)
