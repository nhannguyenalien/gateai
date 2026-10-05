"""Bounded SSE relay. Ambiguous/disconnected requests retain their reservation."""

import json
import time

import anyio
import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from app.billing import finish, reserve
from app.db import db
from app.routing import route_for, upstream_payload
from app.settings import settings


def save_generation(jid, generation_id):
    if isinstance(generation_id, str) and generation_id.startswith("gen-") and len(generation_id) < 300:
        with db() as c:
            c.execute(
                "UPDATE jobs SET generation_id=%s WHERE id=%s AND generation_id IS NULL", (generation_id, jid)
            )


async def events(lines):
    """SSE messages can span multiple data lines and network chunks."""
    data = []
    size = 0
    async for line in lines:
        size += len(line)
        if size > 262144:
            raise ValueError("SSE event too large")
        if not line:
            if data:
                yield "\n".join(data)
            data, size = [], 0
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield "\n".join(data)


async def stream_chat(body, account, key, kind="chat"):
    payload = dict(body)
    alias = payload.pop("model", None)
    route = route_for(alias, kind, payload)
    job, new = reserve(account["id"], key, kind, alias, payload, route)
    if not new:
        # Reconnecting never resubmits or bills a generation twice.
        return JSONResponse(
            {
                "id": str(job["id"]),
                "status": job["status"],
                "message": "Read /v1/jobs/{id} for the stored result",
            },
            status_code=409,
        )
    with db() as c:
        c.execute("UPDATE jobs SET status='submitting',updated_at=now() WHERE id=%s", (job["id"],))
    direct = job["provider"] == "openrouter"
    url = "https://openrouter.ai/api/v1" if direct else settings.litellm_url + "/v1"
    token = settings.openrouter_api_key if direct else settings.litellm_master_key
    client = httpx.AsyncClient(timeout=httpx.Timeout(130, connect=10))
    try:
        request = client.build_request(
            "POST",
            url + ("/responses" if kind == "responses" else "/chat/completions"),
            headers={"Authorization": "Bearer " + token},
            json={
                **upstream_payload(payload, route, kind),
                "stream": True,
                **(
                    {"stream_options": {**payload.get("stream_options", {}), "include_usage": True}}
                    if kind == "chat"
                    else {}
                ),
            },
        )
        upstream = await client.send(request, stream=True)
        upstream.raise_for_status()
        if "text/event-stream" not in upstream.headers.get("content-type", ""):
            raise ValueError("Provider did not return SSE")
        save_generation(job["id"], upstream.headers.get("x-generation-id"))
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        await client.aclose()
        rejected = 400 <= status < 500 and status != 408
        finish(job["id"], "failed" if rejected else "needs_review", error=f"Provider HTTP {status}")
        return JSONResponse(
            {
                "error": {
                    "message": f"Provider rejected request (HTTP {status})",
                    "type": "provider_error",
                    "code": status,
                    "job_id": str(job["id"]),
                }
            },
            status_code=status if rejected else 502,
            headers={"X-Job-ID": str(job["id"])},
        )
    except BaseException as exc:
        with anyio.CancelScope(shield=True):
            await client.aclose()
        finish(job["id"], "needs_review", error="Stream could not be established; outcome uncertain")
        if not isinstance(exc, Exception):
            raise
        raise HTTPException(502, {"job_id": str(job["id"]), "message": "Provider stream unavailable"})

    async def relay():
        complete = False
        message = {"role": "assistant", "content": None}
        tool_calls = {}
        usage = None
        generation_id = None
        ended = False
        started = time.monotonic()
        total = 0
        try:
            async for event in events(upstream.aiter_lines()):
                total += len(event)
                if total > 2_000_000 or time.monotonic() - started > 180:
                    raise ValueError("Stream limit exceeded")
                if event == "[DONE]":
                    if not ended:
                        raise ValueError("Provider ended without finish reason")
                    result = {
                        "id": generation_id,
                        "object": "chat.completion",
                        "model": alias,
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    **message,
                                    **(
                                        {"tool_calls": [tool_calls[i] for i in sorted(tool_calls)]}
                                        if tool_calls
                                        else {}
                                    ),
                                },
                                "finish_reason": ended,
                            }
                        ],
                        "usage": usage,
                    }
                    finish(job["id"], "succeeded", result)
                    complete = True
                    yield "data: [DONE]\n\n"
                    break
                obj = json.loads(event)
                if kind == "responses":
                    event_type = obj.get("type", "")
                    if event_type in ("response.failed", "error") or obj.get("error"):
                        raise ValueError("Provider response failed")
                    response = obj.get("response", {})
                    save_generation(job["id"], response.get("id"))
                    if event_type in ("response.completed", "response.incomplete"):
                        finish(job["id"], "succeeded", response)
                        complete = True
                    yield ("event: " + event_type + "\n" if event_type else "") + "data: " + event + "\n\n"
                    if complete:
                        break
                    continue
                if obj.get("error"):
                    raise ValueError("Provider stream error")
                if obj.get("id") and obj["id"] != generation_id:
                    generation_id = obj["id"]
                    save_generation(job["id"], generation_id)
                if obj.get("usage") is not None:
                    usage = obj["usage"]
                for choice in obj.get("choices", []):
                    if choice.get("finish_reason") == "error":
                        raise ValueError("Provider stream error")
                    ended = choice.get("finish_reason") or ended
                    delta = choice.get("delta", {})
                    for field, value in delta.items():
                        if field == "tool_calls":
                            for fragment in value:
                                index = fragment.get("index", 0)
                                target = tool_calls.setdefault(index, {})
                                merge_delta(target, {k: v for k, v in fragment.items() if k != "index"})
                        else:
                            merge_delta(message, {field: value})
                yield "data: " + event + "\n\n"
            if not complete:
                raise ValueError("Truncated provider stream")
        except Exception:
            yield (
                "data: "
                + json.dumps(
                    {"error": {"message": "Stream interrupted; check job status", "job_id": str(job["id"])}}
                )
                + "\n\n"
            )
        finally:
            # This runs on disconnect as well. Never refund an uncertain provider call.
            if not complete:
                finish(job["id"], "needs_review", error="Stream interrupted; provider cost pending")
            with anyio.CancelScope(shield=True):
                await upstream.aclose()
                await client.aclose()

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={"X-Job-ID": str(job["id"]), "X-Accel-Buffering": "no"},
    )


def merge_delta(target, delta):
    """Accumulate text/function fragments; preserve provider reasoning extensions."""
    for key, value in delta.items():
        if isinstance(value, dict):
            merge_delta(target.setdefault(key, {}), value)
        elif isinstance(value, str) and key not in ("id", "type", "role"):
            target[key] = (target.get(key) or "") + value
        elif isinstance(value, list):
            target.setdefault(key, []).extend(value)
        elif value is not None:
            target[key] = value
