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

    async def free(self, unload_models: bool = True) -> None:
        """Ask ComfyUI to drop its caches (and optionally unload models) —
        the /free endpoint. Best-effort: чистка памяти не должна ронять ран."""
        payload = {"free_memory": True, "unload_models": bool(unload_models)}
        try:
            async with self.session.post(f"{self.cfg.url}/free",
                                         json=payload, headers=self._headers) as r:
                await r.read()
        except Exception:
            pass

    async def queue_ids(self) -> set[str] | None:
        """prompt_id'ы, которые ComfyUI сейчас считает или держит в очереди.

        None — если /queue не ответил (тогда вызывающий не делает выводов).
        Нужно для «пропала ли задача»: см. lost-детектор в `wait`.
        """
        try:
            async with self.session.get(f"{self.cfg.url}/queue",
                                        headers=self._headers) as r:
                if r.status != 200:
                    return None
                body = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None
        if not isinstance(body, dict):
            return None
        ids: set[str] = set()
        for key in ("queue_running", "queue_pending"):
            for item in body.get(key) or []:
                # элемент очереди — список [number, prompt_id, prompt, extra, outputs]
                if isinstance(item, (list, tuple)) and len(item) > 1:
                    ids.add(str(item[1]))
        return ids

    async def wait(self, prompt_id: str, poll_interval: float = 2.0,
                   timeout: float | None = None, should_cancel=None,
                   unreachable_limit: int = 30, on_poll=None,
                   lost_limit: int = 3, lost_check_every: int = 15,
                   ) -> dict[str, Any]:
        """Polls /history/<id> until the prompt finishes. Returns the history entry.

        `timeout=None` (или <= 0) — ждать столько, сколько нужно: медленный под
        (VRAM-своп, 22B-модели, длинный клип) может считать одно видео часами, и
        глухой таймаут просто убивал уже почти готовую генерацию. Вместо часов по
        будильнику здесь два точных признака беды, которые ловят настоящие
        поломки, а не «долго считает»:

        * **недостижимость** — `unreachable_limit` подряд неудачных опросов
          (сеть, 5xx, 403, битый JSON). Упавший/перезапущенный ComfyUI или прокси
          с 403 выглядят ровно как «результата ещё нет», поэтому раньше джоба
          молча висела до таймаута;
        * **потеря задачи** — раз в `lost_check_every` опросов сверяемся с
          /queue: если prompt_id не считается, не стоит в очереди и не попал в
          /history, значит его сняли снаружи (interrupt, рестарт, чужой clear) и
          результата не будет никогда. `lost_limit` подряд таких проверок →
          ошибка, очередь идёт дальше.

        If `should_cancel()` returns true, stops waiting early (caller должен был
        отправить /interrupt, чтобы реально прервать генерацию).

        `on_poll(state, detail)` — необязательный колбэк для наблюдаемости:
        вызывается на каждой итерации со state 'ok'/'unreachable'/'pending'.
        """
        loop = asyncio.get_event_loop()
        unlimited = timeout is None or timeout <= 0
        deadline = None if unlimited else loop.time() + float(timeout)
        url = f"{self.cfg.url}/history/{prompt_id}"
        unreachable = 0
        lost = 0
        polls = 0
        last_error: str | None = None
        while True:
            if should_cancel and should_cancel():
                raise PodError(f"prompt {prompt_id} cancelled")

            entry = None
            reachable = False
            try:
                async with self.session.get(url, headers=self._headers) as r:
                    if r.status == 200:
                        body = await r.json(content_type=None)
                        entry = body.get(prompt_id) if isinstance(body, dict) else None
                        unreachable = 0
                        reachable = True
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

            # Задача пропала? Проверяем редко (это лишний запрос) и только когда
            # ComfyUI отвечает — иначе пустой /queue во время рестарта соврал бы.
            polls += 1
            if reachable and entry is None and polls % lost_check_every == 0:
                ids = await self.queue_ids()
                if ids is None:
                    pass                      # /queue не ответил — не судим
                elif prompt_id in ids:
                    lost = 0
                else:
                    lost += 1
                    if lost >= lost_limit:
                        raise PodError(
                            f"prompt {prompt_id} исчез из ComfyUI: не в очереди и "
                            f"не в /history ({lost} проверки подряд). Скорее всего "
                            f"его прервали или ComfyUI перезапустился.")
            elif entry is not None:
                lost = 0

            if deadline is not None and loop.time() > deadline:
                raise PodError(f"prompt {prompt_id} timed out after {timeout}s")
            await asyncio.sleep(poll_interval)

    # -- live progress -----------------------------------------------------
    @staticmethod
    def progress_from_ws(msg: dict[str, Any], prompt_id: str) -> dict[str, Any] | None:
        """Достаёт прогресс из одного websocket-сообщения ComfyUI.

        ComfyUI 0.3x присылает два вида, оба — только тому client_id, который
        отправил промт (поэтому подписка идёт с тем же `self.client_id`, что и
        `submit`):

            progress        {value, max, prompt_id, node}
            progress_state  {prompt_id, nodes: {id: {value, max, state, ...}}}

        Возвращает подмножество ключей value/max/node/nodes_done/nodes_seen —
        или None, если сообщение не про прогресс (или про чужой промт).
        """
        kind = msg.get("type")
        data = msg.get("data")
        if not isinstance(data, dict):
            return None

        if kind == "progress":
            pid = data.get("prompt_id")
            if pid is not None and str(pid) != prompt_id:
                return None
            try:
                value = float(data["value"])
                maximum = float(data["max"])
            except (KeyError, TypeError, ValueError):
                return None
            if maximum <= 0:
                return None
            return {"value": value, "max": maximum,
                    "node": str(data.get("node") or "")}

        if kind == "progress_state":
            pid = data.get("prompt_id")
            if pid is not None and str(pid) != prompt_id:
                return None
            nodes = data.get("nodes")
            if not isinstance(nodes, dict):
                return None
            out: dict[str, Any] = {
                "nodes_done": sum(1 for n in nodes.values()
                                  if isinstance(n, dict) and n.get("state") == "finished"),
                "nodes_seen": len(nodes),
            }
            # Внутришаговый прогресс берём у работающей ноды с осмысленным max
            # (сэмплер: max = число шагов; у обычной ноды max = 1 — не интересно).
            for n in nodes.values():
                if not isinstance(n, dict) or n.get("state") != "running":
                    continue
                try:
                    value = float(n["value"])
                    maximum = float(n["max"])
                except (KeyError, TypeError, ValueError):
                    continue
                if maximum > 1:
                    out.update({"value": value, "max": maximum,
                                "node": str(n.get("display_node_id")
                                            or n.get("node_id") or "")})
                    break
            return out

        return None

    async def watch_progress(self, prompt_id: str, on_progress,
                             reconnect_delay: float = 3.0) -> None:
        """Подписывается на websocket ComfyUI и зовёт `on_progress(info)`.

        Best-effort и намеренно бесконечный: задача живёт рядом с `wait`, её
        отменяет вызывающий. Любая ошибка сокета — это лишь потеря телеметрии,
        поэтому она не должна валить генерацию, которая идёт нормально: сокет
        просто переподключается.
        """
        params = {"clientId": self.client_id}
        while True:
            try:
                async with self.session.ws_connect(f"{self.cfg.url}/ws",
                                                   params=params,
                                                   headers=self._headers,
                                                   heartbeat=30.0) as ws:
                    async for msg in ws:
                        if msg.type is not aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            payload = json.loads(msg.data)
                        except ValueError:
                            continue
                        if not isinstance(payload, dict):
                            continue
                        info = self.progress_from_ws(payload, prompt_id)
                        if info:
                            on_progress(info)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(reconnect_delay)

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

