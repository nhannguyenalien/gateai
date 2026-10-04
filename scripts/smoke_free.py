"""Synthetic free-model smoke test. Set GATEWAY_URL and GATEWAY_API_KEY."""

import json
import os
import uuid

import httpx


def main():
    base = os.environ["GATEWAY_URL"].rstrip("/")
    headers = {"Authorization": "Bearer " + os.environ["GATEWAY_API_KEY"]}
    with httpx.Client(base_url=base, headers=headers, timeout=180) as client:
        for stream in (False, True):
            key = str(uuid.uuid4())
            body = {
                "model": "chat-free",
                "messages": [{"role": "user", "content": "Reply with the word hello."}],
                "stream": stream,
            }
            if stream:
                with client.stream(
                    "POST", "/v1/chat/completions", json=body, headers={"Idempotency-Key": key}
                ) as response:
                    response.raise_for_status()
                    done, chunks = False, 0
                    for line in response.iter_lines():
                        if line == "data: [DONE]":
                            done = True
                        elif line.startswith("data: "):
                            assert not json.loads(line[6:]).get("error"), "Provider stream failed"
                            chunks += 1
                    assert done and chunks, "Incomplete stream"
                    print("Streaming: PASS", flush=True)
            else:
                response = client.post("/v1/chat/completions", json=body, headers={"Idempotency-Key": key})
                response.raise_for_status()
                assert response.json().get("choices"), "No completion"
                repeat = client.post("/v1/chat/completions", json=body, headers={"Idempotency-Key": key})
                repeat.raise_for_status()
                assert repeat.json() == response.json(), "Replay changed"
                print("Non-streaming and idempotency: PASS", flush=True)


if __name__ == "__main__":
    main()
