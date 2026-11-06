"""Gemini access with one primary attempt and one optional fallback."""

from __future__ import annotations

import time
from typing import Any, Protocol, TypeVar

import httpx
import requests
from google.auth.exceptions import GoogleAuthError
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
Message = tuple[str, str]  # ("system" | "user" | "assistant", text)

TRANSIENT_MARKERS = (
    "429", "503", "500", "502", "504", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED",
    "overloaded", "timed out", "timeout", "Internal", "rate limit", "quota", "ResourceExhausted",
    "ServiceUnavailable", "InternalServerError", "DeadlineExceeded", "TooManyRequests", "GatewayTimeout",
)


class ProviderError(Exception):
    """The model provider could not serve the request."""


def is_transient(exc: BaseException) -> bool:
    # Parsing/validation and credential failures cannot be fixed by another model.
    if isinstance(exc, (ValueError, GoogleAuthError)):
        return False
    retryable = getattr(exc, "is_retryable", None)
    if isinstance(retryable, bool):
        return retryable
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code in (429, 500, 502, 503, 504)
    if isinstance(exc, (TimeoutError, ConnectionError, httpx.TimeoutException, httpx.NetworkError,
                        httpx.RemoteProtocolError, requests.Timeout, requests.ConnectionError)):
        return True
    text = f"{type(exc).__name__}: {exc}"
    return any(marker.lower() in text.lower() for marker in TRANSIENT_MARKERS)


class LLM(Protocol):
    def structured(self, messages: list[Message], schema: type[T], *, deadline: float | None = None) -> T: ...
    def text(self, messages: list[Message], *, deadline: float | None = None) -> str: ...


def _to_langchain(messages: list[Message]):
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    out = []
    for role, content in messages:
        if role == "system":
            out.append(SystemMessage(content=content))
        elif role == "assistant":
            out.append(AIMessage(content=content))
        else:
            out.append(HumanMessage(content=content))
    return out


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type", "text") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)


class EmptyResponse(Exception):
    """A successful provider response contained no usable output."""


class GeminiLLM:
    """Typed structured output and text, using the same bounded fallback policy."""

    def __init__(self, model: str, api_key: str | None, *, thinking_budget: int | None = None,
                 timeout: float = 60.0, tracer: Any = None,
                 fallback_model: str | None = None) -> None:
        from langchain_google_genai import ChatGoogleGenerativeAI

        if not api_key:
            raise ProviderError("GOOGLE_API_KEY is not set. Put a Gemini key in .env")
        # The installed Google SDK treats zero as its default retry count.
        # One means a single attempt; this wrapper owns fallback.
        kwargs = dict(model=model, google_api_key=api_key, max_retries=1, timeout=timeout)
        if thinking_budget is not None:
            kwargs["thinking_budget"] = thinking_budget
        self.chat = ChatGoogleGenerativeAI(**kwargs)
        self._chat_kwargs, self._fallback_chat = kwargs, None
        self.model, self.timeout, self.tracer = model, timeout, tracer
        fallback_model = (fallback_model or "").strip()
        self.fallback_model = fallback_model if fallback_model and fallback_model != model else None

    def structured(self, messages: list[Message], schema: type[T], *, deadline: float | None = None) -> T:
        inputs = _to_langchain(messages)

        def request(chat, timeout: float) -> T:
            runnable = chat.with_structured_output(schema, method="json_schema")
            result = runnable.invoke(inputs, timeout=timeout)
            if result is None:
                raise EmptyResponse(f"no {schema.__name__} returned")
            return schema.model_validate(result)

        return self._call(request, f"structured:{schema.__name__}", deadline)

    def text(self, messages: list[Message], *, deadline: float | None = None) -> str:
        inputs = _to_langchain(messages)

        def request(chat, timeout: float) -> str:
            response = chat.invoke(inputs, timeout=timeout)
            content = _content_to_text(getattr(response, "content", response)).strip()
            if not content:
                raise EmptyResponse("the model returned an empty answer")
            return content

        return self._call(request, "text", deadline)

    def _call(self, fn, label: str, deadline: float | None):
        for model in (self.model, self.fallback_model):
            self._remaining_timeout(deadline)
            try:
                chat = self.chat
                if model != self.model:
                    if self._fallback_chat is None:
                        from langchain_google_genai import ChatGoogleGenerativeAI

                        self._fallback_chat = ChatGoogleGenerativeAI(**{
                            **self._chat_kwargs, "model": model,
                        })
                    chat = self._fallback_chat
                # Lazy client creation also consumes the existing turn budget.
                return fn(chat, self._remaining_timeout(deadline))
            except Exception as exc:
                can_fallback = (model == self.model and self.fallback_model
                                and (isinstance(exc, EmptyResponse) or is_transient(exc)))
                if not can_fallback:
                    raise ProviderError(f"model request failed: {type(exc).__name__}: {str(exc)[:160]}") from exc
                self._remaining_timeout(deadline)
                if self.tracer is not None:
                    self.tracer.event("model_fallback", from_model=self.model,
                                      to_model=self.fallback_model, label=label,
                                      error_type=type(exc).__name__)

    def _remaining_timeout(self, deadline: float | None) -> float:
        remaining = self.timeout if deadline is None else deadline - time.monotonic()
        if remaining < 0.001:
            raise ProviderError("the turn deadline was reached")
        return min(self.timeout, remaining)
