"""Optional S3 mirroring. Activated when S3_BUCKET env var is set.

Env vars (standard boto3 lookup applies for credentials):
  S3_BUCKET           required to enable
  S3_PREFIX           optional, prepended to every key (default "")
  S3_REGION           optional, region for the client
  S3_ENDPOINT_URL     optional, for non-AWS providers (Cloudflare R2, MinIO, etc.)
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY   standard

Uploads happen alongside (not instead of) local writes — local files are
always the source of truth; S3 is a mirror.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path


log = logging.getLogger("s3")


class S3Uploader:
    def __init__(self, bucket: str, prefix: str = "",
                 region: str | None = None, endpoint_url: str | None = None):
        import boto3  # imported here so the dep is optional at runtime
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
        )

    def _key(self, rel: str) -> str:
        rel = rel.lstrip("/")
        return f"{self.prefix}/{rel}" if self.prefix else rel

    async def upload(self, local: Path, rel_key: str) -> str:
        """Uploads local file to s3://<bucket>/<prefix>/<rel_key>. Returns the
        full key. Runs the blocking boto3 call in a thread."""
        key = self._key(rel_key)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._client.upload_file(str(local), self.bucket, key),
        )
        return key


def from_env() -> S3Uploader | None:
    """Returns an uploader if S3_BUCKET is set and boto3 is importable.
    Returns None otherwise — caller treats absence as 'feature disabled'."""
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        return None
    try:
        u = S3Uploader(
            bucket=bucket,
            prefix=os.environ.get("S3_PREFIX", ""),
            region=os.environ.get("S3_REGION") or None,
            endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
        )
        log.info("S3 mirror enabled: s3://%s/%s", bucket, u.prefix or "")
        return u
    except ImportError:
        log.warning("S3_BUCKET set but boto3 not installed — install with "
                    "`uv pip install boto3` to enable S3 mirroring")
        return None
