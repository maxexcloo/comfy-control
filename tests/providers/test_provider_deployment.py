import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from control.config import (
    ControlSettings,
    Provider,
    ProviderManagement,
)
from control.preferences import ControlPreferences, RoutePreference
from providers.base import ProviderNotDeployed, StartRecovery
from providers.deployment import (
    deploy_provider,
    deployment_options,
    terminate_provider,
)
from providers.deployment.common import (
    DeploymentSelection,
    configured_environment,
    serverless_environment,
)
from providers.modal import ModalAdapter, provider_action
from providers.runpod import RunPodPodAdapter

ROOT = Path(__file__).parents[2]


def runpod_gpu_response(items: list[dict[str, object]]) -> httpx.Response:
    return httpx.Response(200, json={"data": {"gpuTypes": items}})


def settings(tmp_path: Path) -> ControlSettings:
    return ControlSettings(
        api_key="control-key",
        config_file=tmp_path / "control.yaml",
        database_path=tmp_path / "control.db",
        ui_password="ui-password",
        ui_username="comfy",
    )


def preferences() -> ControlPreferences:
    return ControlPreferences.from_environment().model_copy(
        update={"worker_api_key": "worker-key"}
    )


def provider(kind: str, name: str = "comfy-control") -> Provider:
    return Provider(
        id=kind,
        api_key="worker-key",
        management=ProviderManagement(kind=kind, name=name, function="serve"),
    )


@pytest.mark.asyncio
async def test_deploys_runpod_pod_from_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "runpod-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/pods"
        assert request.headers["authorization"] == "Bearer runpod-key"
        payload = json.loads(request.content)
        assert payload["dockerStartCmd"] == []
        assert payload["env"]["API_KEY"] == "worker-key"
        assert payload["allowedCudaVersions"] == ["13.0"]
        assert payload["cloudType"] == "COMMUNITY"
        assert payload["computeType"] == "GPU"
        assert payload["gpuTypeIds"] == ["L40S"]
        assert payload["networkVolumeId"] == "network-volume-1"
        assert payload["ports"] == ["8000/http"]
        assert "volumeInGb" not in payload
        return httpx.Response(201, json={"id": "pod-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await deploy_provider(
            client,
            provider("runpod-pod"),
            preferences().model_copy(
                update={"runpod_network_volume_id": "network-volume-1"}
            ),
            settings(tmp_path),
            selection=DeploymentSelection(
                option_id="community:L40S", variant="community"
            ),
        )

    assert response.json()["id"] == "pod-1"


@pytest.mark.asyncio
async def test_runpod_deployment_options_include_live_cost_and_availability(
    monkeypatch,
):
    monkeypatch.setenv("RUNPOD_API_KEY", "runpod-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/graphql"
        return runpod_gpu_response(
            [
                {
                    "displayName": "NVIDIA L40S",
                    "id": "L40S",
                    "memoryInGb": 48,
                    "community": {
                        "stockStatus": "High",
                        "uninterruptablePrice": 0.42,
                    },
                }
            ]
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        options = await deployment_options(client, provider("runpod"), preferences())

    assert options == [
        {
            "availability": "High",
            "available": True,
            "benchmarked_p95_seconds": None,
            "compatible": True,
            "cost_per_hour": 0.42,
            "experimental": True,
            "id": "L40S",
            "label": "NVIDIA L40S",
            "memory_gb": 48,
            "minimum_memory_gb": 24,
            "proven": False,
            "provider_option_id": "L40S",
            "recommended_memory_gb": 32,
            "type": "pod",
            "within_cost_limit": True,
            "workload": "flux-2-klein-9b",
        }
    ]


@pytest.mark.asyncio
async def test_runpod_pod_options_use_live_cloud_stock(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "runpod-key")

    def handler(_: httpx.Request) -> httpx.Response:
        return runpod_gpu_response(
            [
                {
                    "displayName": "NVIDIA L40S",
                    "id": "L40S",
                    "memoryInGb": 48,
                    "community": {
                        "stockStatus": None,
                        "uninterruptablePrice": None,
                    },
                    "secure": {
                        "stockStatus": "Medium",
                        "uninterruptablePrice": 0.5,
                    },
                }
            ]
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        options = await deployment_options(
            client, provider("runpod-pod"), preferences()
        )

    assert [(option["id"], option["cost_per_hour"]) for option in options] == [
        ("secure:L40S", 0.5),
        ("community:L40S", None),
    ]
    assert [option["available"] for option in options] == [True, False]


@pytest.mark.asyncio
async def test_runpod_pod_options_exclude_serverless_mig_types(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "runpod-key")

    def handler(_: httpx.Request) -> httpx.Response:
        return runpod_gpu_response(
            [
                {
                    "displayName": "PRO 6000 MIG 24GB",
                    "id": "NVIDIA RTX PRO 6000 Blackwell Server Edition MIG 1g.24gb",
                    "memoryInGb": 24,
                    "secure": {
                        "stockStatus": "High",
                        "uninterruptablePrice": 0.59,
                    },
                }
            ]
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        options = await deployment_options(
            client, provider("runpod-pod"), preferences()
        )

    assert options == []


@pytest.mark.asyncio
async def test_runpod_replaces_stranded_pod_with_cheapest_compatible_gpu(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RUNPOD_API_KEY", "runpod-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/graphql":
            return runpod_gpu_response(
                [
                    {
                        "displayName": "NVIDIA L40S",
                        "id": "L40S",
                        "memoryInGb": 48,
                        "community": {
                            "stockStatus": "High",
                            "uninterruptablePrice": 0.6,
                        },
                        "secure": {
                            "stockStatus": "Medium",
                            "uninterruptablePrice": 0.8,
                        },
                    },
                    {
                        "displayName": "NVIDIA RTX 4090",
                        "id": "RTX4090",
                        "memoryInGb": 24,
                        "community": {
                            "stockStatus": "High",
                            "uninterruptablePrice": 0.4,
                        },
                    },
                ]
            )
        payload = json.loads(request.content)
        assert payload["cloudType"] == "COMMUNITY"
        assert payload["gpuTypeIds"] == ["RTX4090"]
        return httpx.Response(201, json={"id": "pod-new"})

    failed_start = httpx.Response(
        500,
        request=httpx.Request("POST", "https://rest.runpod.io/v1/pods/pod-old/start"),
        text="There are not enough free GPUs on the host machine to start this pod.",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        recovery = await RunPodPodAdapter("runpod-pod").recover_start(
            client,
            provider("runpod-pod"),
            preferences(),
            settings(tmp_path),
            "pod-old",
            failed_start,
        )

    assert recovery == StartRecovery("pod-new", "pod-old")


def test_worker_ui_credentials_match_control_ui_credentials(tmp_path):
    environment = configured_environment(
        {}, provider("runpod-pod"), preferences(), settings(tmp_path)
    )

    assert environment["CONTROL_UI_PASSWORD"] == "ui-password"
    assert environment["CONTROL_UI_USERNAME"] == "comfy"


def test_worker_receives_only_models_routed_to_its_provider(tmp_path):
    selected = preferences().model_copy(
        update={
            "model_profiles": ["flux-2-klein-9b", "krea-2-turbo"],
            "routes": {
                "images": [
                    RoutePreference(model="flux-2-klein-9b", provider="runpod-pod"),
                    RoutePreference(model="krea-2-turbo", provider="modal"),
                ]
            },
        }
    )

    environment = configured_environment(
        {}, provider("runpod-pod"), selected, settings(tmp_path)
    )

    assert environment["MODEL_PROFILES"] == "flux-2-klein-9b,image-upscale"


@pytest.mark.asyncio
async def test_vast_pod_options_include_storage_and_port_requirements(monkeypatch):
    monkeypatch.setenv("VAST_API_KEY", "vast-key")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["allocated_storage"] == 100
        assert payload["direct_port_count"] == {"gte": 1}
        return httpx.Response(200, json={"offers": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        options = await deployment_options(client, provider("vast-pod"), preferences())

    assert options == []


def test_runpod_serverless_uses_initialising_health_check():
    assert serverless_environment()["HEALTH_CHECK_PATH"] == "/ping"


def test_modal_termination_uses_supported_cli(tmp_path, monkeypatch):
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(
        "providers.modal.subprocess.run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    response = provider_action(
        "modal-terminate", provider("modal"), preferences(), settings(tmp_path)
    )

    assert response.status_code == 200
    response.raise_for_status()
    assert calls == [
        (
            ["modal", "app", "stop", "comfy-control", "--yes"],
            {
                "capture_output": True,
                "check": True,
                "text": True,
            },
        )
    ]


def test_modal_function_preserves_worker_python(monkeypatch):
    function_options: dict[str, object] = {}
    image_options: dict[str, object] = {}
    models = object()

    class FakeApp:
        def __init__(self, name):
            assert name == "comfy-control"

        def function(self, **options):
            function_options.update(options)
            return lambda value: value

    fake_modal = SimpleNamespace(
        App=FakeApp,
        Image=SimpleNamespace(
            from_registry=lambda *args, **kwargs: (
                image_options.update(kwargs)
                or SimpleNamespace(entrypoint=lambda value: value)
            )
        ),
        Secret=SimpleNamespace(from_dict=lambda value: value),
        Volume=SimpleNamespace(from_name=lambda *args, **kwargs: models),
        web_server=lambda *args, **kwargs: lambda value: value,
    )
    monkeypatch.setitem(sys.modules, "modal", fake_modal)

    module = runpy.run_path(ROOT / "providers/deployment/modal.py")
    module["build_app"]({})

    assert image_options == {"force_build": True}
    assert "serialized" not in function_options
    assert function_options["env"] == {
        "MODELS_DIR": "/models",
        "MODEL_PROFILES": "",
        "WORKER_MODE": "serverless",
    }
    assert function_options["volumes"] == {"/models": models}


@pytest.mark.asyncio
async def test_modal_discovery_clears_a_stale_resource(monkeypatch):
    class ModalNotFoundError(Exception):
        pass

    fake_modal = SimpleNamespace(
        exception=SimpleNamespace(NotFoundError=ModalNotFoundError)
    )
    monkeypatch.setitem(sys.modules, "modal", fake_modal)
    monkeypatch.setattr(
        "providers.modal.web_url",
        lambda *_: (_ for _ in ()).throw(ModalNotFoundError()),
    )
    adapter = ModalAdapter("modal")

    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderNotDeployed):
            await adapter.discover(
                client,
                provider("modal"),
                preferences(),
                "comfy-control",
                route=True,
            )


@pytest.mark.asyncio
async def test_deploys_runpod_serverless_template_and_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "runpod-key")
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/graphql":
            return runpod_gpu_response(
                [
                    {
                        "displayName": "NVIDIA L40S",
                        "id": "NVIDIA L40S",
                        "memoryInGb": 48,
                        "community": {
                            "stockStatus": "High",
                            "uninterruptablePrice": 0.59,
                        },
                    }
                ]
            )
        if request.method == "GET":
            return httpx.Response(200, json=[])
        payload = json.loads(request.content)
        if request.url.path == "/v1/templates":
            assert payload["containerDiskInGb"] == 100
            assert payload["dockerStartCmd"] == [
                "python",
                "-m",
                "worker.supervisor",
            ]
            assert payload["env"]["MODELS_DIR"] == "/runpod-volume"
            assert payload["isServerless"] is True
            return httpx.Response(200, json={"id": "template-1"})
        assert payload["templateId"] == "template-1"
        assert "endpointType" not in payload
        assert payload["minCudaVersion"] == "13.0"
        assert payload["networkVolumeId"] == "network-volume-1"
        assert payload["workersMin"] == 0
        return httpx.Response(201, json={"id": "endpoint-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await deploy_provider(
            client,
            provider("runpod"),
            preferences().model_copy(
                update={"runpod_network_volume_id": "network-volume-1"}
            ),
            settings(tmp_path),
        )

    assert requests == [
        "/v1/templates",
        "/v1/templates",
        "/graphql",
        "/v1/endpoints",
    ]
    assert response.json()["id"] == "endpoint-1"


@pytest.mark.asyncio
async def test_updates_existing_runpod_serverless_template(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "runpod-key")
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/graphql":
            return runpod_gpu_response(
                [
                    {
                        "displayName": "NVIDIA L40S",
                        "id": "NVIDIA L40S",
                        "memoryInGb": 48,
                        "community": {
                            "stockStatus": "High",
                            "uninterruptablePrice": 0.59,
                        },
                    }
                ]
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[{"id": "template-1", "name": "comfy-control-template"}],
            )
        payload = json.loads(request.content)
        if request.url.path.endswith("/update"):
            assert payload["env"]["MODEL_PROFILES"] == ("flux-2-klein-9b,image-upscale")
            assert "isServerless" not in payload
            return httpx.Response(200, json={"id": "template-1"})
        assert payload["templateId"] == "template-1"
        assert payload["minCudaVersion"] == "13.0"
        return httpx.Response(201, json={"id": "endpoint-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await deploy_provider(
            client,
            provider("runpod"),
            preferences(),
            settings(tmp_path),
        )

    assert requests == [
        "/v1/templates",
        "/v1/templates/template-1/update",
        "/graphql",
        "/v1/endpoints",
    ]
    assert response.json()["id"] == "endpoint-1"


@pytest.mark.asyncio
async def test_runpod_filters_gpus_by_installed_model_vram(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "runpod-key")
    endpoint_payload: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal endpoint_payload
        if request.url.path == "/v1/templates" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/v1/templates":
            return httpx.Response(200, json={"id": "template-1"})
        if request.url.path == "/graphql":
            return runpod_gpu_response(
                [
                    {
                        "displayName": "NVIDIA GeForce RTX 4090",
                        "id": "NVIDIA GeForce RTX 4090",
                        "memoryInGb": 24,
                        "community": {
                            "stockStatus": "High",
                            "uninterruptablePrice": 0.34,
                        },
                    },
                    {
                        "displayName": "NVIDIA L40S",
                        "id": "NVIDIA L40S",
                        "memoryInGb": 48,
                        "community": {
                            "stockStatus": "High",
                            "uninterruptablePrice": 0.59,
                        },
                    },
                ]
            )
        endpoint_payload = json.loads(request.content)
        return httpx.Response(201, json={"id": "endpoint-1"})

    selected = preferences().model_copy(
        update={"model_profiles": ["flux-2-klein-9b", "krea-2-turbo"]}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await deploy_provider(client, provider("runpod"), selected, settings(tmp_path))

    assert endpoint_payload["gpuTypeIds"] == ["NVIDIA L40S"]


@pytest.mark.asyncio
async def test_deploys_salad_group_with_discovered_gpu(tmp_path, monkeypatch):
    monkeypatch.setenv("SALAD_API_KEY", "salad-key")
    monkeypatch.setenv("SALAD_ORGANISATION", "acme")
    monkeypatch.setenv("SALAD_PROJECT", "media")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/gpu-classes"):
            return httpx.Response(
                200, json={"items": [{"id": "gpu-1", "name": "NVIDIA L40S"}]}
            )
        payload = json.loads(request.content)
        assert payload["container"]["resources"]["gpu_classes"] == ["gpu-1"]
        assert payload["container"]["environment_variables"]["API_KEY"] == "worker-key"
        assert payload["readiness_probe"]["http"]["scheme"] == "http"
        return httpx.Response(201, json={"id": "group-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await deploy_provider(
            client, provider("salad"), preferences(), settings(tmp_path)
        )

    assert response.json()["id"] == "group-1"


@pytest.mark.asyncio
async def test_vast_pod_requires_worker_compatible_gpu(tmp_path, monkeypatch):
    monkeypatch.setenv("VAST_API_KEY", "vast-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path == "/api/v0/bundles/":
            assert payload["compute_cap"] == {"gte": 750}
            assert payload["cuda_max_good"] == {"gte": 13.0}
            assert payload["gpu_ram"] == {"gte": 24000}
            assert payload["ask_contract_id"] == {"eq": 123}
            assert payload["limit"] == 1
            assert payload["reliability"] == {"gte": 0.99}
            return httpx.Response(
                200,
                json={"offers": [{"dph_total": 0.4, "id": 123}]},
            )
        assert request.url.path == "/api/v0/asks/123/"
        assert payload["runtype"] == "args"
        assert payload["target_state"] == "running"
        assert payload["env"]["-p 8000:8000"] == "1"
        assert payload["env"]["API_KEY"] == "worker-key"
        return httpx.Response(200, json={"new_contract": 456, "success": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await deploy_provider(
            client,
            provider("vast-pod"),
            preferences(),
            settings(tmp_path),
            selection=DeploymentSelection(option_id="123"),
        )

    assert response.json()["new_contract"] == 456


@pytest.mark.asyncio
async def test_deploys_and_terminates_vast_serverless(tmp_path, monkeypatch):
    monkeypatch.setenv("VAST_API_KEY", "vast-key")
    methods: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/api/v0/template/":
            payload = json.loads(request.content)
            assert "-p 9000:9000" in payload["env"]
            return httpx.Response(
                200, json={"template": {"hash_id": "hash-1", "id": 5}}
            )
        if request.method == "POST" and request.url.path == "/api/v0/endptjobs/":
            return httpx.Response(200, json={"result": 7})
        if request.method == "POST" and request.url.path == "/api/v0/workergroups/":
            payload = json.loads(request.content)
            assert "compute_cap>=750" in payload["search_params"]
            assert "cuda_max_good>=13.0" in payload["search_params"]
            assert "gpu_ram>=24" in payload["search_params"]
            assert "reliability>=0.99" in payload["search_params"]
            assert payload["gpu_ram"] == 24
            return httpx.Response(200, json={"id": 9})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "results": [{"endpoint_id": 7, "endpoint_name": "comfy", "id": 9}]
                },
            )
        return httpx.Response(200, json={"success": True})

    managed = provider("vast", "comfy")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        deployed = await deploy_provider(
            client, managed, preferences(), settings(tmp_path)
        )
        terminated = await terminate_provider(client, managed, preferences(), "7")

    assert deployed.json()["id"] == 7
    deployed.raise_for_status()
    assert terminated.is_success
    assert ("DELETE", "/api/v0/workergroups/9/") in methods
    assert ("DELETE", "/api/v0/endptjobs/7/") in methods
