"""S3-compatible document store -- Cloudflare R2, MinIO, or AWS S3.

R2 is the intended production target: S3-compatible API, and no egress fees, which matters
because every statement a client downloads is egress.

ENCRYPTION: provider-side, at rest. Objects are directly readable by a holder of a valid
signed URL, which is what makes signed-URL delivery work at all -- a browser cannot decrypt
client-side ciphertext. The consequence is that the storage provider can technically read
what it holds, so the Data Processing Agreement with them is load-bearing rather than a
formality. See docs/COMPLIANCE.md.
"""

from __future__ import annotations

import os
from typing import Any

from app.storage.base import DEFAULT_URL_TTL_SECONDS, ObjectNotFoundError

BUCKET_ENV = "PORTFOLIO_STORAGE_BUCKET"
ENDPOINT_ENV = "PORTFOLIO_STORAGE_ENDPOINT"


class S3DocumentStore:
    def __init__(
        self,
        bucket: str | None = None,
        client: Any = None,
        endpoint_url: str | None = None,
    ):
        self.bucket = bucket or os.environ.get(BUCKET_ENV)
        if not self.bucket:
            raise ValueError(f"{BUCKET_ENV} is not set")
        self._client = client
        self._endpoint_url = endpoint_url or os.environ.get(ENDPOINT_ENV)

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("s3", endpoint_url=self._endpoint_url)
        return self._client

    def put(self, key: str, content: bytes, content_type: str = "application/pdf") -> str:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=content, ContentType=content_type)
        return key

    def get(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise ObjectNotFoundError(key) from exc
            raise

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            # Only a definite "no such object" is an answer. Bad credentials (403), a
            # missing bucket or a throttle is the store failing, and must raise -- read
            # as False, it made /health/storage report a broken archive as healthy.
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return False
            raise

    def delete(self, key: str) -> bool:
        existed = self.exists(key)
        self.client.delete_object(Bucket=self.bucket, Key=key)
        return existed

    def delete_prefix(self, prefix: str) -> int:
        """Remove everything under a prefix. Used by erasure.

        Paginated: an investor with years of monthly statements exceeds the 1000-key
        response limit, and stopping at the first page would leave documents behind while
        reporting success -- the worst possible outcome for an erasure.
        """
        paginator = self.client.get_paginator("list_objects_v2")
        removed = 0
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if not keys:
                continue
            self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})
            removed += len(keys)
        return removed

    def signed_url(self, key: str, ttl_seconds: int = DEFAULT_URL_TTL_SECONDS) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )
