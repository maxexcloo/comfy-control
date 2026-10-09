import json

import httpx
import pytest

from control.config import Provider
from control.preferences import ControlPreferences
from providers.registry import provider_adapter, provider_panel_url
from providers.runpod import RunPodServerlessAdapter
from providers.vast import VastPodAdapter, VastServerlessAdapter


@pytest.mark.parametrize(
    ("kind", "resource", "state"),
    [
        ("runpod-pod", {"desiredStatus": "RUNNING"}, "running"),
        ("runpod", {"workersMin": 0, "workers": []}, "scaled-down"),
        ("runpod", {"workersMin": 1, "workers": []}, "starting"),
        (
            "runpod",
            {"workersMin": 1, "workers": [{"desiredStatus": "EXITED"}]},
            "error",
        ),
        (
            "runpod",
            {"workersMin": 1, "workers": [{"desiredStatus": "RUNNING"}]},
            "ready",
        ),
        ("salad", {"current_state": {"status": "RUNNING"}}, "running"),
        ("vast-pod", {"actual_status": "running"}, "running"),
        ("vast", {"endpoint_state": "ready"}, "ready"),
    ],
)
def test_provider_adapters_normalise_state(kind, resource, state):
    provider = Provider.model_validate(
        {
            "api_key": "key",
            "id": "provider",
            "management": {
                "function": "serve" if kind == "modal" else None,
                "kind": kind,
                "name": "comfy-control",
            },
        }
    )

    adapter = provider_adapter(provider)

    assert adapter is not None
    assert adapter.status(resource)[0] == state


def test_proxy_panel_url_is_derived_without_an_adapter():
    provider = Provider(
        api_key="key",
        base_url="https://proxy.example",
        id="proxy",
        type="proxy",
    )

    assert provider_panel_url(provider, {}, provider.base_url) == (
        "https://proxy.example/management.html"
    )


def test_runpod_status_exposes_counts_without_worker_secrets():
    resource = {
        "workersMax": 2,
        "workersMin": 1,
        "workers": [
            {
                "desiredStatus": "RUNNING",
                "env": {"WORKER_API_KEY": "must-not-leak"},
            }
        ],
    }

    state, details = RunPodServerlessAdapter("runpod").status(resource)

    assert state == "ready"
    assert details == {
        "workersMax": 2,
        "workersMin": 1,
        "workerStates": {"running": 1},
    }
    assert "must-not-leak" not in str(details)


@pytest.mark.parametrize(
    ("adapter", "error", "message"),
    [
        (
            RunPodServerlessAdapter("runpod"),
            "not allowed for QB API, load balancer endpoint",
            "Worker logs are unavailable for RunPod load-balancer endpoints",
        ),
        (
            VastServerlessAdapter("vast"),
            "provider has no ready worker",
            "Waiting for Vast serverless worker assignment",
        ),
    ],
)
def test_serverless_adapters_explain_worker_log_availability(adapter, error, message):
    assert adapter.worker_log_error(error) == message


def test_runpod_serverless_separates_gateway_and_worker_authentication():
    provider = Provider.model_validate(
        {
            "api_key": "worker-key",
            "id": "runpod",
            "management": {
                "kind": "runpod",
                "name": "comfy-control",
            },
        }
    )

    headers = RunPodServerlessAdapter("runpod").worker_headers(
        provider, ControlPreferences(runpod_api_key="runpod-key")
    )

    assert headers == {
        "Authorization": "Bearer runpod-key",
        "x-comfy-control-api-key": "worker-key",
    }


@pytest.mark.asyncio
async def test_runpod_usage_reports_credit():
    request = None

    def respond(received: httpx.Request) -> httpx.Response:
        nonlocal request
        request = received
        return httpx.Response(
            200,
            json={"data": {"myself": {"clientBalance": 42}}},
        )

    provider = Provider.model_validate(
        {
            "api_key": "worker-key",
            "id": "runpod",
            "management": {
                "kind": "runpod",
                "name": "comfy-control",
            },
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        metrics = await RunPodServerlessAdapter("runpod").usage(
            client,
            provider,
            ControlPreferences(runpod_api_key="runpod-key"),
            lambda _: {},
        )

    assert metrics == [{"label": "Credit", "unit": "USD", "value": 42}]
    assert request is not None
    assert request.headers["authorization"] == "Bearer runpod-key"


@pytest.mark.asyncio
async def test_vast_usage_reports_credit_and_month_spend():
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v0/users/current/":
            return httpx.Response(
                200, json={"balance": 0, "credit": 25, "total_spend": -82.5}
            )
        return httpx.Response(
            200,
            json={"results": [{"amount": 1.25}, {"amount": 2}]},
        )

    provider = Provider.model_validate(
        {
            "api_key": "worker-key",
            "id": "vast",
            "management": {"kind": "vast", "name": "comfy-control"},
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        metrics = await VastServerlessAdapter("vast").usage(
            client,
            provider,
            ControlPreferences(vast_api_key="vast-key"),
            lambda _: {},
        )

    assert metrics == [
        {
            "label": "Credit",
            "maximum": 28.25,
            "unit": "USD",
            "value": 25,
        },
        {"label": "Month Spend", "unit": "USD", "value": 3.25},
        {"label": "Total Spend", "unit": "USD", "value": 82.5},
    ]
    charge_request = next(
        request for request in requests if request.url.path == "/api/v0/charges/"
    )
    assert charge_request.url.params["limit"] == "500"
    assert "day" in json.loads(charge_request.url.params["select_filters"])


@pytest.mark.asyncio
async def test_vast_usage_keeps_credit_when_charges_are_unavailable():
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v0/users/current/":
            return httpx.Response(200, json={"balance": 0, "credit": 8.91})
        return httpx.Response(503)

    provider = Provider.model_validate(
        {
            "api_key": "worker-key",
            "id": "vast",
            "management": {"kind": "vast", "name": "comfy-control"},
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        metrics = await VastServerlessAdapter("vast").usage(
            client,
            provider,
            ControlPreferences(vast_api_key="vast-key"),
            lambda _: {},
        )

    assert metrics == [{"label": "Credit", "unit": "USD", "value": 8.91}]


@pytest.mark.asyncio
async def test_vast_serverless_routes_authenticated_execution():
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "run.vast.ai":
            return httpx.Response(
                200,
                json={
                    "cost": 100,
                    "endpoint": "comfy-control",
                    "reqnum": 17,
                    "request_idx": 4,
                    "signature": "signed",
                    "url": "https://worker.example",
                },
            )
        return httpx.Response(
            200,
            json={
                "result": {
                    "execution_id": "image-1",
                    "outputs": [
                        {
                            "content": "aW1hZ2U=",
                            "content_type": "image/png",
                            "filename": "output.png",
                        }
                    ],
                }
            },
        )

    provider = Provider.model_validate(
        {
            "api_key": "worker-key",
            "id": "vast",
            "management": {"kind": "vast", "name": "comfy-control"},
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        outputs = await VastServerlessAdapter("vast").execute_serverless(
            client,
            provider,
            ControlPreferences(vast_api_key="vast-key"),
            {
                "execution_id": "image-1",
                "model": "flux/text-to-image",
                "operation": "image_generation",
                "parameters": {"prompt": "A wombat"},
            },
            [("image", ("input.png", b"input", "image/png"))],
            None,
        )

    worker_request = requests[1]
    worker_payload = json.loads(worker_request.content)
    assert worker_request.headers["authorization"] == "Bearer vast-key"
    assert worker_request.url.params["api_key"] == "vast-key"
    assert worker_payload["auth_data"]["signature"] == "signed"
    assert worker_payload["payload"]["files"][0]["content"] == "aW5wdXQ="
    assert outputs == [(b"image", "image/png", "output.png")]


@pytest.mark.asyncio
async def test_vast_pod_can_be_discovered_while_ports_are_pending():
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "instances": [
                    {
                        "actual_status": "loading",
                        "id": 123,
                        "label": "comfy-control",
                        "public_ipaddr": "192.0.2.1",
                        "ports": {},
                    }
                ]
            },
        )

    provider = Provider.model_validate(
        {
            "api_key": "worker-key",
            "id": "vast-pod",
            "management": {"kind": "vast-pod", "name": "comfy-control"},
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        discovery = await VastPodAdapter("vast-pod").discover(
            client,
            provider,
            ControlPreferences(vast_api_key="vast-key"),
            "123",
            route=False,
        )

    assert discovery.base_url is None
    assert discovery.resource_id == "123"


@pytest.mark.parametrize(
    ("kind", "module"),
    [
        ("modal", "providers.modal"),
        ("runpod-pod", "providers.runpod"),
        ("salad", "providers.salad"),
        ("vast-pod", "providers.vast"),
    ],
)
def test_provider_implementations_are_isolated_by_provider(kind, module):
    provider = Provider.model_validate(
        {
            "api_key": "key",
            "id": "provider",
            "management": {
                "function": "serve" if kind == "modal" else None,
                "kind": kind,
                "name": "comfy-control",
            },
        }
    )

    adapter = provider_adapter(provider)

    assert adapter is not None
    assert adapter.__class__.__module__ == module
