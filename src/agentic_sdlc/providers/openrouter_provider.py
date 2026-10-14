"""Live provider: any model on OpenRouter, through its OpenAI-compatible HTTP API.

Output is requested as JSON constrained by the agent's schema
(``response_format: json_schema``) and routed only to model endpoints that
support it. The schema is also stated in the prompt, because enforcement varies
between hosting providers; whatever comes back is validated against the
pydantic schema, and a mismatch is an ``AgentOutputError`` that the engine
retries with the validation message as feedback.

Rate limits are the one thing handled here rather than in the engine: a 429
carries a server-chosen wait, and honouring it is cheaper than spending one of
the node's bounded attempts. Every wait is bounded and counted.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import ValidationError

from ..models import AgentOutputError, PermanentError, TransientError
from .base import ProviderRequest, ProviderResponse

API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt a pydantic JSON schema to strict structured-output rules.

    Strict mode wants every property listed as required and no ``default``
    keywords; optional fields stay optional through their ``null`` type.
    Keys of a ``properties`` mapping are field names, not keywords: a field
    called ``title`` must survive the stripping of the ``title`` keyword.
    """
    if isinstance(schema, dict):
        out = {
            k: ({name: strict_schema(sub) for name, sub in v.items()} if k == "properties" else strict_schema(v))
            for k, v in schema.items()
            if k not in ("default", "examples", "title")
        }
        if "properties" in out:
            out["required"] = list(out["properties"])
            out.setdefault("additionalProperties", False)
        return out
    if isinstance(schema, list):
        return [strict_schema(item) for item in schema]
    return schema


def extract_json(text: str) -> Any:
    """Parse the model's reply, tolerating code fences or prose around one JSON object."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise AgentOutputError("the reply contained no JSON object") from None
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AgentOutputError(f"the reply was not valid JSON: {exc}") from None


class OpenRouterProvider:
    name = "openrouter"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        max_tokens: int = 32000,  # reasoning models spend part of this before the JSON starts
        timeout_s: float = 300.0,
        rate_limit_waits: int = 3,
        max_wait_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise PermanentError("OPENROUTER_API_KEY is not set")
        self.model, self.max_tokens = model, max_tokens
        self.rate_limit_waits, self.max_wait_s, self.sleep = rate_limit_waits, max_wait_s, sleep
        self.client = client or httpx.Client(timeout=timeout_s)
        self._headers = {
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "https://github.com/agentic-sdlc",
            "X-Title": "agentic-sdlc",
        }

    def _body(self, request: ProviderRequest) -> dict[str, Any]:
        schema = request.schema.model_json_schema()
        system = (
            f"{request.instructions}\n\n"
            "Reply with exactly one JSON object that conforms to the JSON Schema below. "
            "No prose and no code fences outside the JSON.\n\n" + json.dumps(schema)
        )
        user = (
            "<context>\n"
            + json.dumps(request.payload, indent=2, sort_keys=True, default=str)
            + "\n</context>\n\nProduce the output for your role from this context."
        )
        return {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": self.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": request.schema.__name__, "strict": True, "schema": strict_schema(schema)},
            },
            # Only route to endpoints that honour response_format.
            "provider": {"require_parameters": True},
        }

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        for wait_round in range(self.rate_limit_waits + 1):
            try:
                response = self.client.post(API_URL, headers=self._headers, json=body)
            except httpx.TimeoutException as exc:
                raise TransientError(f"OpenRouter request timed out: {exc}") from exc
            except httpx.TransportError as exc:
                raise TransientError(f"connection problem: {exc}") from exc

            if response.status_code == 429:
                message = response.text[:300]
                if "per-day" in message or "per day" in message:
                    raise PermanentError(f"daily free-model quota exhausted: {message}")
                if wait_round == self.rate_limit_waits:
                    raise TransientError(f"still rate limited after {wait_round} waits: {message}")
                retry_after = response.headers.get("retry-after")
                wait = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else 10.0
                self.sleep(min(wait, self.max_wait_s))
                continue
            if response.status_code in (408, 500, 502, 503, 504, 524, 529):
                raise TransientError(f"OpenRouter error {response.status_code}: {response.text[:300]}")
            if response.status_code >= 400:
                raise PermanentError(f"OpenRouter rejected the request ({response.status_code}): {response.text[:500]}")
            return response.json()
        raise AssertionError("unreachable")  # pragma: no cover

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        payload = self._post(self._body(request))
        if payload.get("error"):  # errors can also arrive with HTTP 200
            error = payload["error"]
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            if code in (429, 500, 502, 503, 504):
                raise TransientError(f"upstream model error {code}: {message}")
            raise PermanentError(f"upstream model error {code}: {message}")
        choices = payload.get("choices") or []
        if not choices:
            raise TransientError("the model returned no choices")
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise AgentOutputError("output was truncated at max_tokens; produce a smaller, more focused result")
        content = (choice.get("message") or {}).get("content") or ""
        if not content.strip():
            raise AgentOutputError("the model returned an empty reply")
        try:
            output = request.schema.model_validate(extract_json(content))
        except ValidationError as exc:
            raise AgentOutputError(f"output did not match the {request.schema.__name__} schema: {exc}") from exc
        usage = payload.get("usage") or {}
        return ProviderResponse(
            output=output,
            model=payload.get("model", self.model),
            usage={
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        )
