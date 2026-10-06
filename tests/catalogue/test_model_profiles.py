from pathlib import Path

from catalogue.profiles import profile_details, required_vram_gb

ROOT = Path(__file__).parents[2]


def test_control_image_includes_model_profiles():
    dockerfile = (ROOT / "control/Dockerfile").read_text()

    assert "COPY catalogue ./catalogue" in dockerfile


def test_control_and_worker_images_use_compatible_python():
    control = (ROOT / "control/Dockerfile").read_text().splitlines()[0]
    worker = (ROOT / "worker/Dockerfile").read_text().splitlines()[0]

    assert control == worker == "FROM python:3.14.8-slim-bookworm"


def test_worker_image_uses_one_pinned_cuda_runtime():
    dockerfile = (ROOT / "worker/Dockerfile").read_text()
    constraints = (ROOT / "worker/constraints.txt").read_text().splitlines()

    assert dockerfile.startswith("FROM python:3.14.8-slim-bookworm\n")
    assert "--extra build --extra vast" in dockerfile
    assert "--extra modal" not in dockerfile
    assert "nvidia/cuda" not in dockerfile
    assert constraints == [
        "torch==2.14.1",
        "torchaudio==2.11.0",
        "torchvision==0.29.1",
    ]


def test_model_profile_details_and_combined_vram_requirement():
    details = profile_details("flux-2-klein-9b")

    assert details["minimum_vram_gb"] == 24
    assert "diffusion_models/flux-2-klein-9b-fp8.safetensors" in details["assets"]
    assert required_vram_gb(["flux-2-klein-9b", "krea-2-turbo"]) == 48
