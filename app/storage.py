"""Copy provider assets to private S3/R2; persist keys, mint URLs only on authorized reads."""

import base64
import hashlib
import tempfile
from urllib.parse import urlparse
import boto3
import httpx
from app.settings import settings


def client():
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url or None,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region,
    )


def archive(result, job_id):
    if not settings.s3_bucket:
        return result

    def copy(value):
        if isinstance(value, list):
            return [copy(v) for v in value]
        if isinstance(value, dict):
            return {k: copy(v) for k, v in value.items()}
        if not isinstance(value, str) or not value.startswith("https://"):
            return value
        p = urlparse(value)
        allowed = [h.strip() for h in settings.asset_allowed_hosts.split(",") if h.strip()]
        if (
            p.port not in (None, 443)
            or p.username
            or not any(p.hostname == h or p.hostname.endswith("." + h) for h in allowed)
        ):
            raise ValueError("Output host not allowed; update trusted provider asset hosts")
        key = f"jobs/{job_id}/{hashlib.sha256(value.encode()).hexdigest()}"
        with (
            tempfile.TemporaryFile() as f,
            httpx.stream("GET", value, timeout=120, follow_redirects=False) as response,
        ):
            response.raise_for_status()
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > settings.asset_max_bytes:
                    raise ValueError("Output exceeds storage limit")
                f.write(chunk)
            f.seek(0)
            client().upload_fileobj(
                f,
                settings.s3_bucket,
                key,
                ExtraArgs={
                    "ContentType": response.headers.get("content-type", "application/octet-stream"),
                    "ContentDisposition": "attachment",
                },
            )
        return {"gateai_object_key": key}

    return copy(result)


def signed(result):
    if isinstance(result, dict):
        if set(result) == {"gateai_object_key"}:
            return client().generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.s3_bucket, "Key": result["gateai_object_key"]},
                ExpiresIn=900,
            )
        return {k: signed(v) for k, v in result.items()}
    if isinstance(result, list):
        return [signed(v) for v in result]
    return result


def archive_images(data, job_id):
    """Store base64 images from a synchronous provider response; never keep bytes in the DB."""
    if not settings.s3_bucket:
        raise ValueError("S3 storage is required for base64 image output")
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("Provider returned no images")
    out = []
    for i, item in enumerate(items):
        raw = base64.b64decode(item["b64_json"], validate=True)
        if len(raw) > settings.asset_max_bytes:
            raise ValueError("Output exceeds storage limit")
        media = item.get("media_type") or "image/png"
        key = f"jobs/{job_id}/{i}-{hashlib.sha256(raw).hexdigest()[:16]}"
        client().put_object(
            Bucket=settings.s3_bucket,
            Key=key,
            Body=raw,
            ContentType=media,
            ContentDisposition="attachment",
        )
        out.append({"gateai_object_key": key})
    return {"images": out}
