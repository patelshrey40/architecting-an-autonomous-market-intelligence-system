from __future__ import annotations

import json

import requests


class LLMProvider:
    name = "base"

    def generate_structured(self, role, schema, context):
        raise NotImplementedError


class HeuristicProvider(LLMProvider):
    name = "heuristic"

    def generate_structured(self, role, schema, context):
        fallback = context.get("fallback")
        if fallback is None:
            raise ValueError("No heuristic fallback payload was supplied for %s." % role)
        return fallback


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key, model, base_url, timeout_seconds=45):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def generate_structured(self, role, schema, context):
        instructions = context.get("instructions") or "Return only JSON matching the schema."
        user_input = context.get("input") or ""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": user_input},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": role.replace("-", "_"),
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        response = requests.post(
            "%s/chat/completions" % self.base_url,
            headers={
                "Authorization": "Bearer %s" % self.api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        content = (
            body.get("choices", [{}])[0]
            .get("message", {})
            .get("content")
        )
        if not content:
            raise ValueError("OpenAI response did not include structured content for %s." % role)
        if isinstance(content, list):
            text = "".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict)
            )
        else:
            text = content
        return json.loads(text)


def build_llm_provider(settings):
    if settings.openai_api_key:
        return OpenAIProvider(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
            timeout_seconds=settings.openai_timeout_seconds,
        )
    return HeuristicProvider()
