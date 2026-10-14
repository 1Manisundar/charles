"""Live provider: Claude through the official Anthropic SDK.

Output is constrained to the agent's schema with structured outputs, so a
response either validates or is rejected - the orchestrator never parses free
text. SDK errors are mapped onto the engine's error taxonomy, and the SDK's own
retries are disabled so the orchestrator is the single authority on retry
policy (and every retry shows up in the audit log and the reliability metrics).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from ..models import AgentOutputError, PermanentError, TransientError
from .base import ProviderRequest, ProviderResponse

DEFAULT_MODEL = "claude-opus-5-5"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str = DEFAULT_MODEL, client: Any = None, max_tokens: int = 16000, effort: str = "high"):
        if client is None:
            import anthropic

            # Credentials come from the environment (ANTHROPIC_API_KEY or an
            # `ant auth login` profile); nothing is read from disk by this code.
            client = anthropic.Anthropic(max_retries=0)
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        import anthropic

        user = (
            "<context>\n"
            + json.dumps(request.payload, indent=2, sort_keys=True, default=str)
            + "\n</context>\n\nProduce the output for your role from this context."
        )
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=self.max_tokens,
                system=request.instructions,
                messages=[{"role": "user", "content": user}],
                output_format=request.schema,
                output_config={"effort": self.effort},
            )
        except anthropic.RateLimitError as exc:
            raise TransientError(f"rate limited: {exc.message}") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as exc:
            raise PermanentError(f"provider rejected the request: {exc.message}") from exc
        except anthropic.BadRequestError as exc:
            raise PermanentError(f"invalid request: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientError(f"provider error {exc.status_code}") from exc
            raise PermanentError(f"provider error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:  # includes timeouts
            raise TransientError(f"connection problem: {exc}") from exc
        except ValidationError as exc:
            raise AgentOutputError(f"output did not match the {request.schema.__name__} schema: {exc}") from exc

        if response.stop_reason == "refusal":
            raise PermanentError("the model declined this request")
        if response.stop_reason == "max_tokens":
            raise AgentOutputError("output was truncated at max_tokens; produce a smaller, more focused result")
        if response.parsed_output is None:
            raise AgentOutputError(f"no {request.schema.__name__} could be parsed from the response")
        return ProviderResponse(
            output=response.parsed_output,
            model=response.model,
            usage={"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens},
        )
