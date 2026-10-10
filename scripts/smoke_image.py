"""Live smoke test: one FLUX image via OpenRouter Image API through gateway provider code.

Usage: OPENROUTER_API_KEY in .env or environment; `uv run python scripts/smoke_image.py "prompt"`.
Spends real credit (one image). Saves the PNG locally and prints usage.cost.
"""
import base64
import sys

import yaml

from app.providers import submit
from app.settings import settings

route = yaml.safe_load(open(settings.routes_file))["image-flux"]
prompt = sys.argv[1] if len(sys.argv) > 1 else "A friendly 3D robot mascot waving, soft studio light"
if not settings.openrouter_api_key:
    sys.exit("OPENROUTER_API_KEY missing")
job = {"provider": "openrouter", "kind": "image", "route": route, "payload": {"prompt": prompt}}
data = submit(job)
item = data["data"][0]
raw = base64.b64decode(item["b64_json"], validate=True)
out = "smoke_image.png" if item.get("media_type", "image/png") == "image/png" else "smoke_image.bin"
open(out, "wb").write(raw)
print("bytes", len(raw), "media", item.get("media_type"), "usage", data.get("usage"))
print("price ceiling micros", route["max_cost_micros"], "->", out)
