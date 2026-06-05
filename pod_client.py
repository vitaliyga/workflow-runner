"""Async HTTP client for one ComfyUI instance."""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp


@dataclass
class PodConfig:
    name: str
    url: str
    max_parallel: int = 1
    api_key: str | None = None

    def __post_init__(self) -> None:
        # Strip trailing slash so f"{url}/upload/image" never becomes
        # "//upload/image" (which RunPod's proxy redirects in confusing ways).
        self.url = self.url.rstrip("/")


class PodError(RuntimeError):
    pass


class PodClient:
    def __init__(self, cfg: PodConfig, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.session = session
        self.client_id = str(uuid.uuid4())

    @property
    def _headers(self) -> dict[str, str]:
        h = {}
        if self.cfg.api_key:
            h["Authorization"] = f"Bearer {self.cfg.api_key}"
        return h

    async def upload_image(self, path: Path, subfolder: str = "") -> str:
        """Uploads to ComfyUI input dir. Returns the filename usable by LoadImage."""
        data = aiohttp.FormData()
        data.add_field("image", path.open("rb"), filename=path.name,
                       content_type="application/octet-stream")
        data.add_field("overwrite", "true")
        if subfolder:
            data.add_field("subfolder", subfolder)
        url = f"{self.cfg.url}/upload/image"
        async with self.session.post(url, data=data, headers=self._headers) as r:
            if r.status != 200:
                raise PodError(f"upload_image {r.status}: {await r.text()}")
            body = await r.json()
        name = body.get("name", path.name)
        sub = body.get("subfolder", "")
        return f"{sub}/{name}" if sub else name

    async def submit(self, workflow: dict[str, Any]) -> str:
        payload = {"prompt": workflow, "client_id": self.client_id}
        async with self.session.post(f"{self.cfg.url}/prompt",
                                     json=payload, headers=self._headers) as r:
            if r.status != 200:
                raise PodError(f"submit {r.status}: {await r.text()}")
            body = await r.json()
        return body["prompt_id"]

    async def wait(self, prompt_id: str, poll_interval: float = 2.0,
                   timeout: float = 600.0) -> dict[str, Any]:
        """Polls /history/<id> until the prompt finishes. Returns the history entry."""
        deadline = asyncio.get_event_loop().time() + timeout
        url = f"{self.cfg.url}/history/{prompt_id}"
        while True:
            async with self.session.get(url, headers=self._headers) as r:
                body = await r.json() if r.status == 200 else {}
            entry = body.get(prompt_id)
            if entry and entry.get("status", {}).get("completed"):
                return entry
            if entry and entry.get("status", {}).get("status_str") == "error":
                raise PodError(f"prompt {prompt_id} errored: {entry.get('status')}")
            if asyncio.get_event_loop().time() > deadline:
                raise PodError(f"prompt {prompt_id} timed out after {timeout}s")
            await asyncio.sleep(poll_interval)

    async def download(self, filename: str, subfolder: str, type_: str,
                       dest: Path) -> None:
        params = {"filename": filename, "subfolder": subfolder, "type": type_}
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with self.session.get(f"{self.cfg.url}/view",
                                    params=params, headers=self._headers) as r:
            if r.status != 200:
                raise PodError(f"download {r.status}: {await r.text()}")
            with dest.open("wb") as f:
                async for chunk in r.content.iter_chunked(64 * 1024):
                    f.write(chunk)

    @staticmethod
    def outputs_from_history(
        entry: dict[str, Any],
        only_nodes: set[str] | None = None,
    ) -> list[dict[str, str]]:
        """Flattens selected history outputs into file records.

        If `only_nodes` is provided, only those node IDs are considered. This
        keeps downloads aligned with the configured SaveImage/SaveVideo nodes
        instead of pulling every intermediate output from the graph.

        Supports both image outputs ('images' key) and video outputs
        ('gifs' / 'videos' keys) produced by VHS_VideoCombine and similar.
        """
        out: list[dict[str, str]] = []
        for node_id, node_out in entry.get("outputs", {}).items():
            if only_nodes is not None and len(only_nodes) > 0 and node_id not in only_nodes:
                continue
            # Images (SaveImage, PreviewImage, etc.)
            for batch_idx, img in enumerate(node_out.get("images", [])):
                out.append({
                    "node_id": str(node_id),
                    "batch_index": str(batch_idx),
                    "filename": img["filename"],
                    "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output"),
                })
            # Videos — VHS_VideoCombine returns under 'gifs' (legacy) or 'videos'
            for key in ("gifs", "videos"):
                for batch_idx, vid in enumerate(node_out.get(key, [])):
                    if isinstance(vid, dict) and vid.get("filename"):
                        out.append({
                            "node_id": str(node_id),
                            "batch_index": str(batch_idx),
                            "filename": vid["filename"],
                            "subfolder": vid.get("subfolder", ""),
                            "type": vid.get("type", "output"),
                        })
        return out

