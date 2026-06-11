"""Job queue + pool of pod workers."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import yaml

from csv_loader import Job
from path_builder import output_dir, save_prefix
from pod_client import PodClient, PodConfig, PodError
from s3_uploader import S3Uploader
from workflow_builder import JobParams, WorkflowRegistry, build_workflow


log = logging.getLogger("pool")


@dataclass
class JobItem:
    idx: int
    job: Job
    attempts: int = 0


def load_pods(path: Path) -> list[PodConfig]:
    data = yaml.safe_load(path.read_text())
    return [PodConfig(**p) for p in data["pods"]]


class PodPool:
    def __init__(
        self,
        pods: list[PodConfig],
        workflows: WorkflowRegistry,
        inputs_dir: Path,
        outputs_dir: Path,
        day_tag: str | None = None,
        run_tag: str | None = None,
        max_attempts: int = 3,
        dry_run: bool = False,
        s3: S3Uploader | None = None,
    ):
        self.pods = pods
        self.workflows = workflows
        self.inputs_dir = inputs_dir
        self.outputs_dir = outputs_dir
        self.day_tag = day_tag or time.strftime("%d-%m")
        self.run_tag = run_tag
        self.max_attempts = max_attempts
        self.dry_run = dry_run
        self.s3 = s3
        self.queue: asyncio.Queue[JobItem | None] = asyncio.Queue()
        # cache of (pod_name, image_path) -> remote filename to skip re-upload
        self._upload_cache: dict[tuple[str, str], str] = {}

    @staticmethod
    def _job_images(job: Job) -> tuple[str, ...]:
        return tuple(img for img in (job.input_images or (job.input_image,)) if img)

    async def run(self, jobs: list[Job]) -> None:
        for i, j in enumerate(jobs):
            await self.queue.put(JobItem(idx=i, job=j))

        if self.dry_run:
            workers: list[asyncio.Task] = []
            for pod in self.pods:
                for slot in range(pod.max_parallel):
                    workers.append(asyncio.create_task(
                        self._worker(None, slot, pod_name=pod.name),
                        name=f"{pod.name}#{slot}"))
            for _ in workers:
                await self.queue.put(None)
            await asyncio.gather(*workers)
            return

        timeout = aiohttp.ClientTimeout(total=None, sock_read=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            workers = []
            for pod in self.pods:
                client = PodClient(pod, session)
                for slot in range(pod.max_parallel):
                    workers.append(asyncio.create_task(
                        self._worker(client, slot), name=f"{pod.name}#{slot}"))

            for _ in workers:
                await self.queue.put(None)
            await asyncio.gather(*workers)

    async def _worker(self, client: PodClient | None, slot: int,
                      pod_name: str | None = None) -> None:
        name = pod_name or (client.cfg.name if client else "mock")
        while True:
            item = await self.queue.get()
            if item is None:
                self.queue.task_done()
                return
            try:
                if self.dry_run:
                    await self._handle_dry(name, item)
                else:
                    await self._handle(client, item)
            except Exception as e:
                item.attempts += 1
                log.warning("[%s#%d] job %d failed (attempt %d): %s",
                            name, slot, item.idx, item.attempts, e)
                if item.attempts < self.max_attempts:
                    await self.queue.put(item)
                else:
                    log.error("[%s#%d] job %d giving up after %d attempts",
                              name, slot, item.idx, item.attempts)
            finally:
                self.queue.task_done()

    async def _handle(self, client: PodClient, item: JobItem) -> list[str]:
        job = item.job
        bundle = self.workflows.get(job.workflow)

        local_images = [self.inputs_dir / name for name in self._job_images(job)]
        if not local_images:
            raise FileNotFoundError(f"input image missing: {job.input_image}")
        for local_image in local_images:
            if not local_image.exists():
                raise FileNotFoundError(f"input image missing: {local_image}")

        remote_images: list[str] = []
        for local_image in local_images:
            cache_key = (client.cfg.name, str(local_image))
            remote_name = self._upload_cache.get(cache_key)
            if not remote_name:
                remote_name = await client.upload_image(local_image)
                self._upload_cache[cache_key] = remote_name
            remote_images.append(remote_name)

        prefix = save_prefix(job, item.idx, self.day_tag, self.run_tag)
        params = JobParams(
            lora_name=job.lora_name,
            lora_strength_model=job.lora_strength_model,
            lora_strength_clip=job.lora_strength_clip,
            prompt_positive=job.prompt_positive,
            prompt_negative=job.prompt_negative,
            input_image=remote_images[0],
            input_images=tuple(remote_images),
            seed=job.seed,
            steps=job.steps,
            cfg=job.cfg,
            sampler_name=job.sampler_name,
            scheduler=job.scheduler,
            denoise=job.denoise,
            save_prefix=prefix,
            extra=job.extra,
        )
        wf = build_workflow(bundle.template, bundle.mapping, params)

        log.info("[%s] submit job %d wf=%s girl=%s lora=%s",
                 client.cfg.name, item.idx, job.workflow, job.girl, job.lora_name)
        prompt_id = await client.submit(wf)
        entry = await client.wait(prompt_id)

        out_dir = output_dir(self.outputs_dir, job, self.day_tag, self.run_tag)
        out_dir.mkdir(parents=True, exist_ok=True)

        outputs = client.outputs_from_history(entry, set(bundle.mapping.save_images))
        if not outputs:
            raise PodError(f"no outputs returned for prompt {prompt_id}")

        local_files: list[Path] = []
        outputs_by_node: dict[str, list[dict[str, str]]] = {}
        for i, img in enumerate(outputs):
            node_id = str(img.get("node_id") or "")
            batch_idx = int(img.get("batch_index") or i)
            # Keep the folder flat. The ComfyUI filename already contains the
            # save prefix (including node suffix when there are multiple save
            # nodes), so it is unique without an extra node subfolder.
            dest = out_dir / Path(img["filename"]).name
            await client.download(img["filename"], img["subfolder"],
                                  img["type"], dest)
            local_files.append(dest)
            outputs_by_node.setdefault(node_id, []).append(img)

        if self.s3:
            for p in local_files:
                rel = p.relative_to(self.outputs_dir).as_posix()
                try:
                    key = await self.s3.upload(p, rel)
                    log.info("[%s] s3 ↑ %s", client.cfg.name, key)
                except Exception as e:
                    log.warning("[%s] s3 upload failed for %s: %s",
                                client.cfg.name, p.name, e)

        log.info("[%s] done job %d -> %s", client.cfg.name, item.idx, out_dir)
        return [p.relative_to(self.outputs_dir).as_posix() for p in local_files]

    async def _handle_dry(self, pod_name: str, item: JobItem) -> list[str]:
        """Same patch flow as _handle but no network. Writes the patched
        workflow JSON and a placeholder result + manifest into outputs/."""
        job = item.job
        bundle = self.workflows.get(job.workflow)

        local_images = [self.inputs_dir / name for name in self._job_images(job)]
        # We don't require the image to exist in dry-run — we just record
        # what would be uploaded.

        prefix = save_prefix(job, item.idx, self.day_tag, self.run_tag)
        params = JobParams(
            lora_name=job.lora_name,
            lora_strength_model=job.lora_strength_model,
            lora_strength_clip=job.lora_strength_clip,
            prompt_positive=job.prompt_positive,
            prompt_negative=job.prompt_negative,
            input_image=job.input_image,
            input_images=job.input_images,
            seed=job.seed,
            steps=job.steps,
            cfg=job.cfg,
            sampler_name=job.sampler_name,
            scheduler=job.scheduler,
            denoise=job.denoise,
            save_prefix=prefix,
            extra=job.extra,
        )
        wf = build_workflow(bundle.template, bundle.mapping, params)

        await asyncio.sleep(0.05)        # simulate latency so concurrency is visible

        out_dir = output_dir(self.outputs_dir, job, self.day_tag, self.run_tag)
        out_dir.mkdir(parents=True, exist_ok=True)

        # Dry run writes the patched workflow only; no photo-side JSON.
        patched_path = out_dir / f"{item.idx:05d}_seed{job.seed}.workflow.json"
        patched_path.write_text(json.dumps(wf, ensure_ascii=False, indent=2))

        for sid in bundle.mapping.save_images:
            placeholder = out_dir / f"{item.idx:05d}_seed{job.seed}_n{sid}.placeholder.txt"
            placeholder.write_text(
                f"dry-run: would download from pod\n"
                f"workflow={job.workflow} node={sid}\n"
                f"prefix={params.save_prefix}\n"
                f"input_images_local={','.join(str(p) for p in local_images)}\n"
            )
        if self.s3:
            sample_key = self.s3._key(
                (out_dir.relative_to(self.outputs_dir) /
                 f"{item.idx:05d}_seed{job.seed}.png").as_posix())
            log.info("[%s] DRY would upload to s3://%s/%s",
                     pod_name, self.s3.bucket, sample_key)
        log.info("[%s] DRY job %d wf=%s -> %s", pod_name, item.idx, job.workflow, out_dir)
        return [
            (patched_path.relative_to(self.outputs_dir).as_posix()),
            *[
                (out_dir / f"{item.idx:05d}_seed{job.seed}_n{sid}.placeholder.txt")
                .relative_to(self.outputs_dir).as_posix()
                for sid in bundle.mapping.save_images
            ],
        ]
