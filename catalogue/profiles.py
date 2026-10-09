from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ProfilePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    capabilities: list[str]
    hourly_cost_limit: float = Field(gt=0)
    idle_seconds: int = Field(ge=0)
    minimum_vram_gb: int = Field(ge=0)
    recommended_vram_gb: int = Field(ge=0)
    service_class: str
    benchmarks: list[dict[str, object]] = Field(default_factory=list)


def profile_path(package: str) -> Path | None:
    profiles_dir = Path(
        os.getenv("MODEL_CATALOGUE_DIR", "/opt/comfy-control/catalogue/profiles")
    )
    candidates = (
        profiles_dir / f"{package}.yaml",
        Path("catalogue/profiles") / f"{package}.yaml",
    )
    return next((path for path in candidates if path.is_file()), None)


def catalogue_root() -> Path:
    configured = Path(
        os.getenv("MODEL_CATALOGUE_DIR", "/opt/comfy-control/catalogue/profiles")
    )
    root = configured.parent if configured.name == "profiles" else configured
    return root if root.is_dir() else Path("catalogue")


def profile_details(package: str) -> dict[str, object]:
    path = profile_path(package)
    if path is None:
        return {"minimum_vram_gb": None, "assets": []}
    profile = yaml.safe_load(path.read_text())
    if not isinstance(profile, dict):
        return {"minimum_vram_gb": None, "assets": []}
    assets = []
    for source in profile.get("sources", []):
        if not isinstance(source, dict):
            continue
        destination = str(source.get("destination") or "").strip("/")
        includes = source.get("include") or []
        if isinstance(includes, str):
            includes = [includes]
        for item in includes:
            relative = str(item).removeprefix("split_files/")
            assets.append("/".join(value for value in (destination, relative) if value))
        if source.get("type") == "civitai" and destination:
            assets.append(destination)
    minimum_vram = profile.get("minimum_vram_gb")
    policy = ProfilePolicy.model_validate(profile)
    return {
        "assets": sorted(set(assets)),
        "capabilities": policy.capabilities,
        "description": policy.description,
        "hourly_cost_limit": policy.hourly_cost_limit,
        "idle_seconds": policy.idle_seconds,
        "minimum_vram_gb": minimum_vram if isinstance(minimum_vram, int) else None,
        "recommended_vram_gb": policy.recommended_vram_gb,
        "service_class": policy.service_class,
    }


def profile_policy(package: str) -> ProfilePolicy:
    path = profile_path(package)
    if path is None:
        raise KeyError(f"unknown model profile: {package}")
    value = yaml.safe_load(path.read_text())
    if not isinstance(value, dict):
        raise TypeError(f"model profile must contain a mapping: {package}")
    return ProfilePolicy.model_validate(value)


def required_vram_gb(packages: list[str]) -> int:
    requirements = [
        value
        for package in packages
        if isinstance((value := profile_details(package)["minimum_vram_gb"]), int)
    ]
    return max(requirements, default=0)
