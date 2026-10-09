from __future__ import annotations

PROVIDER_SETTINGS_PREFIXES = {
    "cliproxyapi": "cliproxy_",
    "modal": "modal_",
    "runpod": "runpod_",
    "runpod-pod": "runpod_",
    "salad": "salad_",
    "vast": "vast_",
    "vast-pod": "vast_",
}


def settings_group(name: str) -> tuple[str, str]:
    if name == "display_time_zone":
        return "Display", "Dates & Times"
    for prefix, label in (
        ("cliproxy_", "CLI Proxy API"),
        ("modal_", "Modal"),
        ("runpod_", "RunPod"),
        ("salad_", "SaladCloud"),
        ("vast_", "Vast"),
    ):
        if name.startswith(prefix):
            return "Providers", label
    if name in {"civitai_token", "hf_token", "worker_api_key"}:
        return "Worker", "Credentials"
    if name == "routes":
        return "Routing", "Provider Routes"
    if name == "model_profiles":
        return "Models", "Installation"
    return "Worker", "Runtime"


def title_label(value: object) -> str:
    label = str(value).title()
    for title, name in {
        "Api": "API",
        "Cli": "CLI",
        "Comfyui": "ComfyUI",
        "Gb": "GB",
        "Gpu": "GPU",
        "Id": "ID",
        "Mib": "MiB",
        "Runpod": "RunPod",
        "Saladcloud": "SaladCloud",
        "Url": "URL",
    }.items():
        label = label.replace(title, name)
    return label


def prepare_field(field: dict[str, object]) -> dict[str, object]:
    prepared = dict(field)
    prepared["label"] = title_label(prepared["label"])
    if prepared["name"] == "modal_gpu":
        prepared["options"] = ["A100", "H100", "L40S"]
    elif prepared["name"] == "display_time_zone":
        prepared["options"] = [
            "America/Los_Angeles",
            "America/New_York",
            "Australia/Brisbane",
            "Australia/Perth",
            "Australia/Sydney",
            "Europe/London",
            "Pacific/Auckland",
            "UTC",
        ]
    return prepared


def provider_fields(
    description: dict[str, object], provider_id: str
) -> list[dict[str, object]]:
    prefix = PROVIDER_SETTINGS_PREFIXES.get(provider_id, "")
    label_prefixes = {
        "modal": "Modal ",
        "runpod": "RunPod ",
        "runpod-pod": "RunPod ",
        "salad": "SaladCloud ",
        "vast": "Vast.Ai ",
        "vast-pod": "Vast.Ai ",
    }
    cliproxy_labels = {
        "cliproxy_api_key": "API Key",
        "cliproxy_management_key": "Management Key",
        "cliproxy_url": "URL",
    }
    fields = []
    for field in description["fields"]:
        name = str(field["name"])
        if not prefix or not name.startswith(prefix):
            continue
        prepared = prepare_field(field)
        if name in cliproxy_labels:
            prepared["label"] = cliproxy_labels[name]
        elif label_prefix := label_prefixes.get(provider_id):
            prepared["label"] = str(prepared["label"]).removeprefix(label_prefix)
        fields.append(prepared)
    return sorted(fields, key=lambda item: str(item["label"]).casefold())
