from urllib.parse import urlparse

import httpx

from app.settings import settings


def fal_url(url):
    p = urlparse(url)
    if p.scheme != "https" or p.netloc != "queue.fal.run":
        raise ValueError("Invalid provider URL")
    return url


def submit(job):
    r = job["route"]
    payload = {**job["payload"], **r["fixed"]}
    with httpx.Client(timeout=130) as c:
        if job["provider"] == "openrouter":
            res = c.post(
                "https://openrouter.ai/api/v1/" + ("embeddings" if job["kind"] == "embeddings" else "chat/completions"),
                headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
                json={"model": r["model"], **payload},
            )
        elif job["provider"] == "litellm":
            path = "/v1/responses" if job["kind"] == "responses" else "/v1/chat/completions"
            res = c.post(
                settings.litellm_url + path,
                headers={"Authorization": f"Bearer {settings.litellm_master_key}"},
                json={"model": r["model"], **payload},
            )
        elif job["provider"] == "fal":
            if not settings.fal_key:
                raise ValueError("FAL_KEY missing")
            res = c.post(
                "https://queue.fal.run/" + r["model"],
                headers={"Authorization": f"Key {settings.fal_key}"},
                json=payload,
            )
        elif job["provider"] == "runpod":
            if not settings.runpod_api_key:
                raise ValueError("RUNPOD_API_KEY missing")
            res = c.post(
                "https://api.runpod.ai/v2/" + r["model"] + "/run",
                headers={"Authorization": f"Bearer {settings.runpod_api_key}"},
                json={"input": payload},
            )
        else:
            raise ValueError("Unknown provider")
        res.raise_for_status()
        data = res.json()
        if data.get("error"):
            raise ValueError("Provider returned an error")
        return data


def poll(job):
    ref = job["provider_ref"]
    with httpx.Client(timeout=30) as c:
        if job["provider"] == "fal":
            headers = {"Authorization": f"Key {settings.fal_key}"}
            res = c.get(fal_url(ref["status_url"]), headers=headers)
            res.raise_for_status()
            data = res.json()
            if data["status"] != "COMPLETED":
                return None
            if data.get("error"):
                return ("failed", None)
            res = c.get(fal_url(ref["response_url"]), headers=headers)
            res.raise_for_status()
            return ("succeeded", res.json())
        res = c.get(
            f"https://api.runpod.ai/v2/{job['route']['model']}/status/{ref['id']}",
            headers={"Authorization": f"Bearer {settings.runpod_api_key}"},
        )
        res.raise_for_status()
        data = res.json()
        if data["status"] == "COMPLETED":
            return ("succeeded", data.get("output"))
        if data["status"] in ("FAILED", "CANCELLED", "TIMED_OUT"):
            return ("failed", None)
        return None
