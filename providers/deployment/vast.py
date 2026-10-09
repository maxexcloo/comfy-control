from __future__ import annotations

from urllib.parse import quote

import httpx

from catalogue.profiles import required_vram_gb
from control.config import ControlSettings, Provider
from control.preferences import ControlPreferences
from providers.deployment.common import (
    WORKER_SERVERLESS_PORT,
    WORKER_STORAGE_GB,
    DeploymentSelection,
    checked_request,
    configured_environment,
    docker_flags,
    required_preference,
    response_json,
    worker_model_profiles,
)

MINIMUM_COMPUTE_CAPABILITY = 750
MINIMUM_CPU_CORES = 4
MINIMUM_CUDA_VERSION = 13.0
MINIMUM_DOWNLOAD_MBPS = 100
MINIMUM_RELIABILITY = 0.99


def offer_filters(minimum_vram: int) -> dict[str, object]:
    minimum_system_memory_mb = 64_000 if minimum_vram >= 80 else 16_000
    return {
        "type": "ondemand",
        "compute_cap": {"gte": MINIMUM_COMPUTE_CAPABILITY},
        "cpu_cores_effective": {"gte": MINIMUM_CPU_CORES},
        "cpu_ram": {"gte": minimum_system_memory_mb},
        "cuda_max_good": {"gte": MINIMUM_CUDA_VERSION},
        "gpu_ram": {"gte": minimum_vram * 1000},
        "inet_down": {"gte": MINIMUM_DOWNLOAD_MBPS},
        "num_gpus": {"eq": 1},
        "reliability": {"gte": MINIMUM_RELIABILITY},
        "rentable": {"eq": True},
        "rented": {"eq": False},
        "verified": {"eq": True},
    }


def headers(preferences: ControlPreferences) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {required_preference('VAST_API_KEY', preferences)}"
    }


async def deployment_options(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
) -> list[dict[str, object]]:
    minimum_vram = max(
        preferences.vast_minimum_gpu_memory_gb,
        required_vram_gb(worker_model_profiles(provider, preferences)),
    )
    filters: dict[str, object] = {
        **offer_filters(minimum_vram),
        "limit": 20,
        "order": [["dph_total", "asc"]],
    }
    if provider.management is not None and provider.management.kind == "vast-pod":
        filters.update(
            {
                "allocated_storage": WORKER_STORAGE_GB,
                "direct_port_count": {"gte": 1},
            }
        )
    response = await checked_request(
        client,
        "POST",
        "https://console.vast.ai/api/v0/bundles/",
        headers=headers(preferences),
        json=filters,
    )
    payload = response.json()
    values = payload.get("offers", []) if isinstance(payload, dict) else []
    if isinstance(values, dict):
        values = [values]
    return [
        {
            "available": True,
            "compute_capability": item.get("compute_cap"),
            "cpu_cores": item.get("cpu_cores_effective") or item.get("cpu_cores"),
            "cost_per_hour": item.get("dph_total"),
            "cuda_maximum": item.get("cuda_max_good"),
            "id": str(item.get("id") or item.get("ask_contract_id")),
            "label": str(item.get("gpu_name") or "Vast GPU"),
            "location": item.get("geolocation") or item.get("country"),
            "memory_gb": round(float(item.get("gpu_ram", 0)) / 1000, 1),
            "reliability": item.get("reliability2"),
            "system_memory_gb": (
                round(float(item.get("cpu_ram", 0)) / 1000, 1)
                if item.get("cpu_ram") is not None
                else None
            ),
        }
        for item in values
        if isinstance(item, dict) and (item.get("id") or item.get("ask_contract_id"))
    ]


async def deploy_pod(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
    settings: ControlSettings,
    *,
    selection: DeploymentSelection | None = None,
) -> httpx.Response:
    minimum_vram = max(
        preferences.vast_minimum_gpu_memory_gb,
        required_vram_gb(worker_model_profiles(provider, preferences)),
    )
    environment = configured_environment({}, provider, preferences, settings)
    search_filters: dict[str, object] = {
        **offer_filters(minimum_vram),
        "allocated_storage": WORKER_STORAGE_GB,
        "direct_port_count": {"gte": 1},
        "limit": 20,
        "order": [["dph_total", "asc"]],
    }
    if selection is not None and selection.option_id:
        try:
            offer_id = int(selection.option_id)
        except ValueError as exc:
            raise RuntimeError("selected Vast offer id is invalid") from exc
        search_filters.update({"limit": 1, "ask_contract_id": {"eq": offer_id}})
    search = await checked_request(
        client,
        "POST",
        "https://console.vast.ai/api/v0/bundles/",
        headers=headers(preferences),
        json=search_filters,
    )
    payload = search.json()
    offers = payload.get("offers", []) if isinstance(payload, dict) else []
    if isinstance(offers, dict):
        offers = [offers]
    offer = next(
        (
            item
            for item in offers
            if isinstance(item, dict)
            and (
                selection is None
                or not selection.option_id
                or str(item.get("id") or item.get("ask_contract_id"))
                == selection.option_id
            )
        ),
        None,
    )
    if offer is None or not (
        offer_id := offer.get("id") or offer.get("ask_contract_id")
    ):
        message = (
            "selected Vast offer is no longer available"
            if selection is not None and selection.option_id
            else "Vast.ai has no suitable rentable GPU offer"
        )
        raise RuntimeError(message)
    management = provider.management
    assert management is not None
    return await checked_request(
        client,
        "PUT",
        f"https://console.vast.ai/api/v0/asks/{quote(str(offer_id), safe='')}/",
        headers=headers(preferences),
        json={
            "cancel_unavail": True,
            "disk": WORKER_STORAGE_GB,
            "image": preferences.worker_image,
            "label": management.name,
            "runtype": "args",
            "target_state": "running",
            "env": {
                **environment,
                f"-p {management.port}:{management.port}": "1",
            },
        },
    )


async def deploy_serverless(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
    settings: ControlSettings,
    *,
    selection: DeploymentSelection | None = None,
) -> httpx.Response:
    minimum_vram = max(
        preferences.vast_minimum_gpu_memory_gb,
        required_vram_gb(worker_model_profiles(provider, preferences)),
        int(selection.memory_gb) if selection and selection.memory_gb else 0,
    )
    environment = configured_environment(
        {"VAST_WORKER_PORT": str(WORKER_SERVERLESS_PORT)},
        provider,
        preferences,
        settings,
    )
    management = provider.management
    assert management is not None
    worker_port = WORKER_SERVERLESS_PORT
    template_response = await checked_request(
        client,
        "POST",
        "https://console.vast.ai/api/v0/template/",
        headers=headers(preferences),
        json={
            "args_str": "python -m worker.vast_gateway",
            "docker_login_pass": "",
            "docker_login_repo": "",
            "docker_login_user": "",
            "env": docker_flags(environment, worker_port),
            "image": preferences.worker_image,
            "name": f"{management.name}-template",
            "private": True,
            "recommended_disk_space": WORKER_STORAGE_GB,
            "runtype": "args",
        },
    )
    template_payload = template_response.json()
    template = (
        template_payload.get("template") if isinstance(template_payload, dict) else None
    )
    if not isinstance(template, dict) or not template.get("hash_id"):
        raise RuntimeError("Vast.ai template response has no hash_id")
    endpoint = await checked_request(
        client,
        "POST",
        "https://console.vast.ai/api/v0/endptjobs/",
        headers=headers(preferences),
        json={
            "cold_workers": 0,
            "endpoint_name": management.name,
            "inactivity_timeout": max(provider.idle_seconds or 60, 60),
            "max_workers": preferences.vast_maximum_workers,
            "min_load": 0,
            "target_util": 0.9,
        },
    )
    endpoint_payload = endpoint.json()
    endpoint_id = (
        endpoint_payload.get("result") if isinstance(endpoint_payload, dict) else None
    )
    if endpoint_id is None:
        raise RuntimeError("Vast.ai endpoint response has no result")
    workergroup = await checked_request(
        client,
        "POST",
        "https://console.vast.ai/api/v0/workergroups/",
        headers=headers(preferences),
        json={
            "cold_workers": 0,
            "endpoint_id": endpoint_id,
            "endpoint_name": management.name,
            "gpu_ram": minimum_vram,
            "max_workers": preferences.vast_maximum_workers,
            "search_params": (
                "verified=true rentable=true rented=false num_gpus=1 "
                f"gpu_ram>={minimum_vram} "
                f"compute_cap>={MINIMUM_COMPUTE_CAPABILITY} "
                f"cuda_max_good>={MINIMUM_CUDA_VERSION} "
                f"cpu_cores_effective>={MINIMUM_CPU_CORES} "
                f"cpu_ram>={64000 if minimum_vram >= 80 else 16000} "
                f"inet_down>={MINIMUM_DOWNLOAD_MBPS} "
                f"reliability>={MINIMUM_RELIABILITY}"
            ),
            "template_hash": str(template["hash_id"]),
            "test_workers": 1,
        },
    )
    return response_json(
        201,
        {
            "endpoint": endpoint_payload,
            "id": endpoint_id,
            "template": {"hash_id": template["hash_id"], "id": template.get("id")},
            "workergroup": workergroup.json(),
        },
    )


async def terminate(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
    resource_id: str,
) -> httpx.Response:
    management = provider.management
    assert management is not None
    if management.kind == "vast-pod":
        return await checked_request(
            client,
            "DELETE",
            f"https://console.vast.ai/api/v0/instances/{quote(resource_id, safe='')}/",
            headers=headers(preferences),
        )
    groups = await checked_request(
        client,
        "GET",
        "https://console.vast.ai/api/v0/workergroups/",
        headers=headers(preferences),
    )
    payload = groups.json()
    for group in payload.get("results", []) if isinstance(payload, dict) else []:
        if not isinstance(group, dict):
            continue
        if (
            str(group.get("endpoint_id")) != resource_id
            and group.get("endpoint_name") != management.name
        ):
            continue
        await checked_request(
            client,
            "DELETE",
            "https://console.vast.ai/api/v0/workergroups/"
            f"{quote(str(group['id']), safe='')}/",
            headers=headers(preferences),
        )
    return await checked_request(
        client,
        "DELETE",
        f"https://console.vast.ai/api/v0/endptjobs/{quote(resource_id, safe='')}/",
        headers=headers(preferences),
    )
