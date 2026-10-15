"""Small cancellable JSON decisions from the same local language model."""

import json

import httpx

from esi02.dialogue import DialogueLLM


def request(settings, system, data, schema, cancellation, max_tokens=384, *, trim=None):
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False, separators=(",", ":"))}]
    with httpx.Client(base_url=settings.llm_url, trust_env=False, timeout=settings.llm_timeout) as client:
        cancellation.attach(client.close)
        try:
            cancellation.check()
            while True:
                try:
                    messages = DialogueLLM(settings)._budget(client, messages, cancellation, max_tokens)
                    break
                except ValueError:
                    if trim is None or not trim(data):
                        raise
                    messages[1]["content"] = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            response = client.post("/v1/chat/completions", json={
                "model": settings.llm_model, "messages": messages, "max_tokens": max_tokens,
                "temperature": 0.15, "stream": False, "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "bounded_decision", "strict": True, "schema": schema.model_json_schema()}},
            })
            response.raise_for_status()
            cancellation.check()
            return schema.model_validate_json(response.json()["choices"][0]["message"]["content"])
        finally:
            cancellation.detach()
