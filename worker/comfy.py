from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import httpx


@dataclass(frozen=True)
class OutputRef:
    filename: str
    subfolder: str = ""
    type: str = "output"
    media_type: str = "image/png"


class ComfyClient:
    def __init__(self, base_url: str, request_timeout: float = 60):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(base_url=self.base_url, timeout=request_timeout)
        self._object_info: dict[str, Any] | None = None

    async def close(self) -> None:
        await self.http.aclose()

    async def ready(self) -> bool:
        try:
            response = await self.http.get("/system_stats")
            return response.is_success
        except httpx.HTTPError:
            return False

    async def object_info(self) -> dict[str, Any]:
        if self._object_info is None:
            response = await self.http.get("/object_info")
            response.raise_for_status()
            self._object_info = response.json()
        return self._object_info

    async def submit(self, prompt: dict[str, Any]) -> str:
        response = await self.http.post("/prompt", json={"prompt": prompt})
        response.raise_for_status()
        data = response.json()
        if "prompt_id" not in data:
            raise RuntimeError(f"ComfyUI rejected workflow: {data}")
        return data["prompt_id"]

    async def cancel(self, prompt_id: str) -> None:
        """Remove a queued prompt or interrupt it when it is currently running."""
        try:
            response = await self.http.get("/queue")
            response.raise_for_status()
            queue = response.json()
            running = any(
                len(item) > 1 and item[1] == prompt_id
                for item in queue.get("queue_running", [])
            )
            await self.http.post("/queue", json={"delete": [prompt_id]})
            if running:
                await self.http.post("/interrupt")
        except httpx.HTTPError:
            return

    async def upload(
        self, filename: str, content: bytes | BinaryIO, content_type: str
    ) -> str:
        response = await self.http.post(
            "/upload/image",
            files={"image": (filename, content, content_type)},
            data={"overwrite": "true", "type": "input"},
        )
        response.raise_for_status()
        return response.json()["name"]

    async def wait(
        self, prompt_id: str, output_node: str, timeout: float
    ) -> list[OutputRef]:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            response = await self.http.get(f"/history/{prompt_id}")
            response.raise_for_status()
            history = response.json().get(prompt_id)
            if history:
                status = history.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI execution failed: {status}")
                outputs = history.get("outputs", {}).get(output_node, {})
                refs = self._output_refs(outputs)
                if refs:
                    return refs
                if status.get("completed"):
                    raise RuntimeError(
                        f"workflow completed without output from node {output_node}"
                    )
            await asyncio.sleep(0.25)
        raise TimeoutError(f"workflow {prompt_id} exceeded {timeout} seconds")

    @staticmethod
    def _output_refs(outputs: dict[str, Any]) -> list[OutputRef]:
        refs: list[OutputRef] = []
        for key, default_media in (
            ("audio", "audio/wav"),
            ("gifs", "video/mp4"),
            ("images", "image/png"),
            ("videos", "video/mp4"),
        ):
            for item in outputs.get(key, []):
                media_type = (
                    item.get("format")
                    or mimetypes.guess_type(item["filename"])[0]
                    or default_media
                )
                refs.append(
                    OutputRef(
                        filename=item["filename"],
                        media_type=media_type,
                        subfolder=item.get("subfolder", ""),
                        type=item.get("type", "output"),
                    )
                )
        return refs

    async def fetch_output(self, ref: OutputRef) -> httpx.Response:
        response = await self.http.get(
            "/view",
            params=self._output_params(ref),
        )
        response.raise_for_status()
        return response

    async def save_output(self, ref: OutputRef, destination: Path) -> str:
        async with self.http.stream(
            "GET", "/view", params=self._output_params(ref)
        ) as response:
            response.raise_for_status()
            with destination.open("wb") as output:
                async for chunk in response.aiter_bytes():
                    output.write(chunk)
            return response.headers.get("content-type", ref.media_type)

    async def stream_output(self, ref: OutputRef) -> AsyncIterator[bytes]:
        async with self.http.stream(
            "GET", "/view", params=self._output_params(ref)
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                yield chunk

    @staticmethod
    def _output_params(ref: OutputRef) -> dict[str, str]:
        return {
            "filename": ref.filename,
            "subfolder": ref.subfolder,
            "type": ref.type,
        }
