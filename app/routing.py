from pathlib import Path
import yaml
from fastapi import HTTPException

from app.settings import settings


def route_for(alias, kind, payload):
    routes = yaml.safe_load(Path(settings.routes_file).read_text()) or {}
    if not isinstance(alias, str):
        raise HTTPException(400, "model must be an alias string")
    r = routes.get(alias)
    if not r or not r.get("enabled") or r["kind"] != kind:
        raise HTTPException(400, "Unknown or disabled model alias")
    if r["max_cost_micros"] <= 0 or r["max_cost_micros"] * 100 > r["user_price_micros"] * 60:
        raise HTTPException(503, "Route violates cost policy")
    # Only explicitly priced inputs are accepted. Fixed parameters cannot be overridden.
    if set(payload) - set(r["allowed_inputs"]):
        raise HTTPException(400, "Unsupported input parameter")
    import json

    if len(json.dumps(payload).encode()) > r.get("max_input_bytes", 16000):
        raise HTTPException(413, "Input exceeds priced limit")
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
    if kind == "responses" and not isinstance(payload.get("input"), str):
        raise HTTPException(400, "Responses input must be text")
    if r["provider"] == "fal" and not settings.fal_key:
        raise HTTPException(503, "fal provider is not configured")
    if r["provider"] == "runpod" and not settings.runpod_api_key:
        raise HTTPException(503, "Runpod provider is not configured")
    return r
