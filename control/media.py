from __future__ import annotations

import asyncio
import base64
import ipaddress
import socket
import uuid
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urljoin, urlparse

import httpx

from control.http import exception_message
from control.media_types import (
    image_media_type,
    media_extension,
    media_type_from_filename,
    safe_filename,
)
from control.preferences import ControlPreferences
from control.store import ControlStore


class ControllerContext(Protocol):
    preferences: ControlPreferences
    providers: dict[str, Any]
    store: ControlStore

    def worker_url(self, runtime: Any, path: str) -> str: ...


class ControlMedia:
    def __init__(self, controller: ControllerContext, root: Path):
        self.client = httpx.AsyncClient(follow_redirects=True, timeout=120)
        self.controller = controller
        self.media_path = root / "media"
        self.media_path.mkdir(parents=True, exist_ok=True)
        self.store = controller.store
        self.uploads_path = root / "uploads"

    async def close(self) -> None:
        await self.client.aclose()

    def save(
        self,
        history_id: str,
        content: bytes,
        content_type: str,
        filename: str,
    ) -> int:
        extension = media_extension(content_type)
        directory = self.media_path / history_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{uuid.uuid4().hex}{extension}"
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_bytes(content)
        temporary.replace(path)
        return self.store.save_media(
            history_id,
            content_type.split(";", 1)[0],
            safe_filename(filename, extension),
            path,
            len(content),
        )

    def save_input(
        self,
        history_id: str,
        content: bytes,
        content_type: str,
        filename: str,
        field_name: str,
        *,
        source_url: str | None = None,
    ) -> int:
        extension = media_extension(content_type)
        directory = self.media_path / history_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"input-{uuid.uuid4().hex}{extension}"
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_bytes(content)
        temporary.replace(path)
        return self.store.save_input_media(
            history_id,
            content_type.split(";", 1)[0],
            safe_filename(filename, extension),
            path,
            len(content),
            field_name=field_name,
            source_url=source_url,
        )

    async def resolve_input(
        self, reference: object
    ) -> tuple[bytes, str, str, str | None]:
        if isinstance(reference, dict):
            reference = reference.get("url")
        if not isinstance(reference, str) or not reference:
            raise ValueError("image must be an uploaded file, data URL, or HTTP URL")
        if reference.startswith("data:"):
            header, separator, encoded = reference.partition(",")
            if not separator or ";base64" not in header:
                raise ValueError("image data URL must use base64 encoding")
            content_type = header[5:].split(";", 1)[0]
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ValueError("image data URL is invalid") from exc
            if len(content) > self.controller.preferences.maximum_request_bytes:
                raise ValueError("remote image is too large")
            return content, content_type, "input" + media_extension(content_type), None

        parsed = urlparse(reference)
        local_media = parsed.path.startswith("/media/") or parsed.path.startswith(
            "/ops/media/"
        )
        if local_media and parsed.path.endswith("/content"):
            try:
                parts = parsed.path.split("/")
                asset_id = int(parts[3] if parts[1] == "ops" else parts[2])
            except (IndexError, ValueError) as exc:
                raise ValueError("media URL is invalid") from exc
            asset = self.store.media_asset(asset_id)
            if asset is None or not Path(asset.path).is_file():
                raise ValueError("media URL was not found")
            return (
                Path(asset.path).read_bytes(),
                asset.content_type,
                Path(asset.path).name,
                reference,
            )
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("image URL must use HTTP or HTTPS")
        if parsed.username or parsed.password:
            raise ValueError("image URL must not contain credentials")

        current = reference
        for _ in range(6):
            current_url = urlparse(current)
            assert current_url.hostname is not None
            addresses = await asyncio.to_thread(
                socket.getaddrinfo,
                current_url.hostname,
                current_url.port or (443 if current_url.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            for address in addresses:
                ip = ipaddress.ip_address(address[4][0])
                if not ip.is_global:
                    raise ValueError("image URL resolves to a private address")
            async with self.client.stream(
                "GET", current, follow_redirects=False
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("image URL redirect has no location")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                if not content_type.startswith("image/"):
                    raise ValueError("image URL did not return an image")
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > self.controller.preferences.maximum_request_bytes:
                        raise ValueError("remote image is too large")
                filename = unquote(Path(current_url.path).name) or "input"
                return bytes(chunks), content_type, filename, reference
        raise ValueError("image URL redirected too many times")

    async def download(
        self,
        history_id: str,
        provider: str,
        url: str,
        fallback_name: str,
    ) -> None:
        runtime = self.controller.providers[provider]
        base_url = self.controller.worker_url(runtime, "")
        resolved = urljoin(f"{base_url}/", url)
        for _ in range(6):
            headers = {}
            local = resolved == base_url or resolved.startswith(f"{base_url}/")
            if local:
                headers["Authorization"] = f"Bearer {runtime.config.api_key}"
            client = runtime.client if local else self.client
            async with client.stream(
                "GET", resolved, headers=headers, follow_redirects=False
            ) as response:
                if response.is_redirect and response.headers.get("location"):
                    resolved = urljoin(resolved, response.headers["location"])
                    continue
                response.raise_for_status()
                filename = unquote(Path(urlparse(resolved).path).name) or fallback_name
                content_type = response.headers.get(
                    "content-type", media_type_from_filename(filename)
                )
                extension = media_extension(content_type)
                directory = self.media_path / history_id
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f"{uuid.uuid4().hex}{extension}"
                temporary = path.with_suffix(path.suffix + ".part")
                size = 0
                try:
                    with temporary.open("wb") as file:
                        async for chunk in response.aiter_bytes():
                            file.write(chunk)
                            size += len(chunk)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
                self.store.save_media(
                    history_id,
                    content_type.split(";", 1)[0],
                    safe_filename(filename, extension),
                    path,
                    size,
                )
                return
        raise ValueError("media URL redirected too many times")

    async def archive_images(
        self, history_id: str, provider: str, response: httpx.Response
    ) -> None:
        try:
            value = response.json()
            data = value.get("data", []) if isinstance(value, dict) else []
            for index, item in enumerate(data):
                if not isinstance(item, dict):
                    continue
                if encoded := item.get("b64_json"):
                    content = base64.b64decode(encoded, validate=True)
                    content_type = image_media_type(content)
                    self.save(
                        history_id,
                        content,
                        content_type,
                        f"image-{index + 1}{media_extension(content_type)}",
                    )
                elif url := item.get("url"):
                    await self.download(
                        history_id, provider, str(url), f"image-{index + 1}.png"
                    )
        except Exception as exc:  # noqa: BLE001 - archival must not fail inference
            self.store.event(
                "error",
                f"media archival failed: {exception_message(exc)}",
                provider=provider,
                request_id=history_id,
            )

    async def archive_video(
        self, history_id: str, provider: str, response: dict[str, object]
    ) -> None:
        try:
            output_url = response.get("output_url")
            if not output_url:
                raise RuntimeError("video provider returned no output URL")
            await self.download(history_id, provider, str(output_url), "video.mp4")
        except Exception as exc:  # noqa: BLE001 - archival must not fail inference
            self.store.event(
                "error",
                f"media archival failed: {exception_message(exc)}",
                provider=provider,
                request_id=history_id,
            )
