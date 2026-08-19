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

    async def upload_file(self, path: Path, subfolder: str = "") -> str:
        """Uploads any input file (photo, reference video, audio) into ComfyUI's
        input dir and returns the name a loader node can use.

        ComfyUI's `/upload/image` route stores whatever multipart file it gets —
        it does not decode it as an image — so `LoadVideo` reference clips go the
        same way. The form field must still be called "image" (that is the route's
        parameter name), hence the one endpoint for both.
        """
        data = aiohttp.FormData()
        data.add_field("image", path.open("rb"), filename=path.name,
                       content_type="application/octet-stream")
        data.add_field("overwrite", "true")
        if subfolder:
            data.add_field("subfolder", subfolder)
        url = f"{self.cfg.url}/upload/image"
        async with self.session.post(url, data=data, headers=self._headers) as r:
            if r.status != 200:
                raise PodError(f"upload_file {path.name} {r.status}: {await r.text()}")
            body = await r.json()
        name = body.get("name", path.name)
        sub = body.get("subfolder", "")
        return f"{sub}/{name}" if sub else name

    async def upload_image(self, path: Path, subfolder: str = "") -> str:
        """Back-compat alias — same endpoint, kept for existing call sites."""
        return await self.upload_file(path, subfolder)

    async def submit(self, workflow: dict[str, Any]) -> str:
        payload = {"prompt": workflow, "client_id": self.client_id}
        async with self.session.post(f"{self.cfg.url}/prompt",
                                     json=payload, headers=self._headers) as r:
            if r.status != 200:
                raise PodError(f"submit {r.status}: {await r.text()}")
            body = await r.json()
        return body["prompt_id"]

    async def interrupt(self) -> None:
        """Tell ComfyUI to abort the currently-executing prompt (best-effort)."""
        try:
            async with self.session.post(f"{self.cfg.url}/interrupt",
                                         headers=self._headers) as r:
                await r.read()
        except Exception:
            pass

    async def wait(self, prompt_id: str, poll_interval: float = 2.0,
                   timeout: float = 600.0, should_cancel=None,
                   unreachable_limit: int = 30, on_poll=None) -> dict[str, Any]:
        """Polls /history/<id> until the prompt finishes. Returns the history entry.

        If `should_cancel()` returns true, stops waiting early (caller должен был
        отправить /interrupt, чтобы реально прервать генерацию).

        Отдельно от таймаута отслеживается **достижимость** ComfyUI. Раньше
        упавший/перезапущенный ComfyUI (или прокси, отдающий 403) выглядел ровно
        как «результата ещё нет»: джоба молча висела в `running` все 30 минут, в
        логе — ни строчки. Теперь `unreachable_limit` подряд неудачных опросов
        (сеть, 5xx, 403, битый JSON) завершают ожидание внятной ошибкой, а
        очередь идёт дальше вместо простоя на полчаса.

        `on_poll(state, detail)` — необязательный колбэк для наблюдаемости:
        вызывается на каждой итерации со state 'ok'/'unreachable'/'pending'.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        url = f"{self.cfg.url}/history/{prompt_id}"
        unreachable = 0
        last_error: str | None = None
        while True:
            if should_cancel and should_cancel():
                raise PodError(f"prompt {prompt_id} cancelled")

            entry = None
            try:
                async with self.session.get(url, headers=self._headers) as r:
                    if r.status == 200:
                        body = await r.json(content_type=None)
                        entry = body.get(prompt_id) if isinstance(body, dict) else None
                        unreachable = 0
                    else:
                        # 403 — типовой признак «ходим снаружи через proxy»;
                        # 5xx/404 — ComfyUI ещё не поднялся после рестарта.
                        unreachable += 1
                        last_error = f"HTTP {r.status}"
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                unreachable += 1
                last_error = f"{type(e).__name__}: {e}"

            if entry and entry.get("status", {}).get("completed"):
                return entry
            if entry and entry.get("status", {}).get("status_str") == "error":
                raise PodError(f"prompt {prompt_id} errored: {entry.get('status')}")

            if on_poll:
                on_poll("unreachable" if unreachable else "pending", last_error)

            if unreachable >= unreachable_limit:
                raise PodError(
                    f"ComfyUI недостижим: {unreachable} неудачных опросов подряд "
                    f"({last_error}). Проверь, что он жив и что в настройках указан "
                    f"внутренний адрес (http://127.0.0.1:8188 / :8083), а не внешний "
                    f"proxy-порт. prompt {prompt_id}")
            if loop.time() > deadline:
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

        Supports image outputs ('images' key) and video outputs ('gifs' /
        'videos' keys, VHS_VideoCombine). When `only_nodes` explicitly targets
        save nodes, it is permissive: ANY list of dicts carrying 'filename' is
        accepted (covers SaveVideo / CreateVideo, whose output key varies by
        ComfyUI version). The untargeted case stays conservative (known keys
        only) so intermediate previews aren't pulled.
        """
        targeted = bool(only_nodes)
        known_keys = ("images", "gifs", "videos")
        out: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add(node_id: str, batch_idx: int, rec: dict[str, Any]) -> None:
            fn = rec.get("filename")
            if not fn:
                return
            dedup = (str(fn), str(rec.get("subfolder", "")))
            if dedup in seen:
                return
            seen.add(dedup)
            out.append({
                "node_id": str(node_id),
                "batch_index": str(batch_idx),
                "filename": fn,
                "subfolder": rec.get("subfolder", ""),
                "type": rec.get("type", "output"),
            })

        for node_id, node_out in entry.get("outputs", {}).items():
            if targeted and node_id not in only_nodes:
                continue
            if not isinstance(node_out, dict):
                continue
            # Keys to scan: known ones always; everything else only when this
            # node was explicitly targeted as a save node.
            keys = list(node_out.keys()) if targeted else known_keys
            for key in keys:
                vals = node_out.get(key)
                if not isinstance(vals, list):
                    continue
                for batch_idx, rec in enumerate(vals):
                    if isinstance(rec, dict) and rec.get("filename"):
                        add(node_id, batch_idx, rec)
        return out

