import json
from pathlib import Path
import yaml
from fastapi import HTTPException

from app.settings import settings


def available_models():
    routes = yaml.safe_load(Path(settings.routes_file).read_text()) or {}
    return {
        "object": "list",
        "data": [
            {
                "id": alias,
                "object": "model",
                "created": 0,
                "owned_by": "gateai",
                "kind": r["kind"],
                "upstream_model": r["model"],
                "streaming": r["kind"] == "chat",
                "endpoint": "/v1/embeddings" if r["kind"] == "embeddings" else "/v1/chat/completions",
                "dimensions": r.get("dimensions"),
                "max_input_bytes": r.get("max_input_bytes", 16000),
                "max_output_tokens": r.get("max_output_tokens", r.get("fixed", {}).get("max_tokens")),
                "native_api": r.get("native_api", False),
                "endpoints": (
                    ["/v1/chat/completions", "/v1/responses"]
                    if r.get("native_api")
                    else ["/v1/embeddings"]
                    if r["kind"] == "embeddings"
                    else ["/v1/chat/completions"]
                ),
            }
            for alias, r in routes.items()
            if r.get("enabled")
        ],
    }


def route_for(alias, kind, payload):
    routes = yaml.safe_load(Path(settings.routes_file).read_text()) or {}
    if not isinstance(alias, str):
        raise HTTPException(400, "model must be an alias string")
    r = routes.get(alias)
    if r is None:
        r = next((v for v in routes.values() if v.get("enabled") and v["model"] == alias), None)
    if (
        not r
        or not r.get("enabled")
        or not (r["kind"] == kind or (kind == "responses" and r.get("native_api") and r["kind"] == "chat"))
    ):
        raise HTTPException(400, "Unknown or disabled model alias")
    free = (
        r.get("free") is True
        and r["provider"] == "openrouter"
        and r["model"].endswith(":free")
        and r["max_cost_micros"] == 0
        and r["user_price_micros"] == 0
    )
    if not free and (r["max_cost_micros"] <= 0 or r["max_cost_micros"] * 100 > r["user_price_micros"] * 60):
        raise HTTPException(503, "Route violates cost policy")
    if r.get("native_api") and free and kind in ("chat", "responses"):
        validate_native(payload, kind, r)
        if not settings.openrouter_api_key:
            raise HTTPException(503, "OpenRouter is not configured")
        return r
    # Only explicitly priced inputs are accepted. Fixed parameters cannot be overridden.
    if set(payload) - set(r["allowed_inputs"]):
        raise HTTPException(400, "Unsupported input parameter")
    if len(json.dumps(payload).encode()) > r.get("max_input_bytes", 16000):
        raise HTTPException(413, "Input exceeds priced limit")
    if "stream" in payload and not isinstance(payload["stream"], bool):
        raise HTTPException(400, "stream must be boolean")
    if r["provider"] == "openrouter" and not settings.openrouter_api_key:
        raise HTTPException(503, "OpenRouter is not configured")
    if kind == "chat":
        messages = payload.get("messages")
        if (
            not isinstance(messages, list)
            or not messages
            or any(
                not isinstance(m, dict)
                or set(m) - {"role", "content"}
                or m.get("role") not in ("system", "user", "assistant")
                or not isinstance(m.get("content"), str)
                for m in messages
            )
        ):
            raise HTTPException(400, "Only text messages are supported by this priced route")
    if kind == "embeddings":
        if payload.get("encoding_format", "float") not in ("float", "base64"):
            raise HTTPException(400, "encoding_format must be float or base64")
        if "dimensions" in payload and (
            type(payload["dimensions"]) is not int or payload["dimensions"] != r["dimensions"]
        ):
            raise HTTPException(400, "This route only supports its configured dimensions")
        value = payload.get("input")
        items = [value] if isinstance(value, str) else value
        if (
            not isinstance(items, list)
            or not 1 <= len(items) <= 16
            or any(not isinstance(item, str) or not item.strip() for item in items)
        ):
            raise HTTPException(400, "input must be nonempty text or 1–16 nonempty text strings")
    if kind == "responses" and not isinstance(payload.get("input"), str):
        raise HTTPException(400, "Responses input must be text")
    if r["provider"] == "fal" and not settings.fal_key:
        raise HTTPException(503, "fal provider is not configured")
    if r["provider"] == "runpod" and not settings.runpod_api_key:
        raise HTTPException(503, "Runpod provider is not configured")
    return r


CHAT_PARAMETERS = set(
    "messages prompt stream stream_options tools tool_choice parallel_tool_calls response_format max_tokens max_completion_tokens temperature top_p top_k min_p top_a seed stop frequency_penalty presence_penalty repetition_penalty logit_bias logprobs top_logprobs reasoning reasoning_effort include_reasoning user prediction verbosity n".split()
)
RESPONSES_PARAMETERS = set(
    "input instructions stream tools tool_choice parallel_tool_calls max_output_tokens temperature top_p top_logprobs reasoning text include metadata user store background truncation service_tier safety_identifier prompt_cache_key".split()
)


def validate_native(payload, kind, route):
    allowed = CHAT_PARAMETERS if kind == "chat" else RESPONSES_PARAMETERS
    unknown = set(payload) - allowed
    if unknown:
        raise HTTPException(400, "Unsupported parameters: " + ", ".join(sorted(unknown)))
    if len(json.dumps(payload).encode()) > route["max_input_bytes"]:
        raise HTTPException(413, "Input exceeds gateway byte limit")
    for field in ("stream", "parallel_tool_calls", "include_reasoning", "logprobs", "store", "background"):
        if field in payload and not isinstance(payload[field], bool):
            raise HTTPException(400, field + " must be boolean")
    if payload.get("store") or payload.get("background"):
        raise HTTPException(400, "Only stateless, foreground Responses requests are supported")
    if type(payload.get("n", 1)) is not int or payload.get("n", 1) != 1:
        raise HTTPException(400, "Only n=1 is supported")
    if "max_tokens" in payload and "max_completion_tokens" in payload:
        raise HTTPException(400, "Use only one token limit parameter")
    for field in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        if field in payload:
            value = payload[field]
            if type(value) is not int or not 1 <= value <= route["max_output_tokens"]:
                raise HTTPException(400, f"{field} must be 1–{route['max_output_tokens']}")
    if "stream_options" in payload and not isinstance(payload["stream_options"], dict):
        raise HTTPException(400, "stream_options must be an object")
    # Server-executed tools/plugins can bill separately even on a free model.
    if "tools" in payload:
        tools = payload["tools"]
        if not isinstance(tools, list) or any(
            not isinstance(t, dict) or t.get("type") != "function" for t in tools
        ):
            raise HTTPException(400, "Only client-executed function tools are enabled")
    if kind == "chat":
        messages = payload.get("messages")
        if messages is None and isinstance(payload.get("prompt"), str) and payload["prompt"]:
            return
        if not isinstance(messages, list) or not messages:
            raise HTTPException(400, "messages must be a nonempty array")
        for m in messages:
            if not isinstance(m, dict) or m.get("role") not in (
                "system",
                "developer",
                "user",
                "assistant",
                "tool",
                "function",
            ):
                raise HTTPException(400, "Invalid message role")
            content = m.get("content")
            if content is not None and not isinstance(content, (str, list)):
                raise HTTPException(400, "Message content must be text, content parts, or null")
            if isinstance(content, list) and any(
                not isinstance(part, dict)
                or part.get("type") not in ("text", "image_url", "input_audio", "video_url")
                for part in content
            ):
                raise HTTPException(400, "Unsupported content part; file parsing is not enabled")
            if m["role"] == "tool" and not isinstance(m.get("tool_call_id"), str):
                raise HTTPException(400, "Tool results require tool_call_id")
    elif not isinstance(payload.get("input"), (str, list)):
        raise HTTPException(400, "Responses input must be text or an array of items")
    elif isinstance(payload["input"], list):
        for item in payload["input"]:
            if not isinstance(item, dict):
                raise HTTPException(400, "Responses input items must be objects")
            if isinstance(item.get("content"), list) and any(
                part.get("type") == "input_file" for part in item["content"] if isinstance(part, dict)
            ):
                raise HTTPException(400, "File parsing is not enabled")


def upstream_payload(payload, route, kind):
    result = {**payload, **route.get("fixed", {}), "model": route["model"]}
    if route.get("native_api"):
        limit_key = "max_output_tokens" if kind == "responses" else "max_tokens"
        if not any(k in result for k in ("max_tokens", "max_completion_tokens", "max_output_tokens")):
            result[limit_key] = route.get("default_output_tokens", 4096)
        # Require requested capabilities rather than silently ignoring tools/schema.
        result["provider"] = {"require_parameters": True}
    return result
