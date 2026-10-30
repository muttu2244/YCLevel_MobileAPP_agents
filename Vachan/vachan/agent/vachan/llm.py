"""Model access — provider agnostic, standard library only.

Two shapes cover every free option in the plan:

  * OllamaClient         a model on this machine          (localhost:11434)
  * OpenAICompatClient   any OpenAI-compatible endpoint   (GitHub Models, Groq,
                         Gemini's compat endpoint, Together, Azure AI Foundry)

Microsoft Agent Framework plugs in at exactly this seam. Its chat clients take
(system, user) and return text, so when you have Foundry access you can replace
`client_for()` with an ``agent_framework.openai.OpenAIChatClient`` and delete
nothing else — the agents only ever call ``complete()``. Verify the current MAF
class names against its docs before wiring it; they moved during preview.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


class LLMError(RuntimeError):
    """A model call failed. Callers fall back to the rule-based path."""


@dataclass
class ChatClient:
    base_url: str
    model: str
    api_key: str = ""
    timeout: int = 120

    def complete(self, system: str, user: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers={"content-type": "application/json", **headers}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise LLMError(f"{url} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(
                f"{url} unreachable ({exc.reason}). Is the model server running?"
            ) from exc
        except TimeoutError as exc:
            raise LLMError(f"{url} timed out after {self.timeout}s") from exc


class OllamaClient(ChatClient):
    """Fully local, fully free. `ollama pull llama3.1` then `ollama serve`."""

    def complete(self, system: str, user: str) -> str:
        out = self._post(
            self.base_url.rstrip("/") + "/api/chat",
            {
                "model": self.model,
                "stream": False,
                "format": "json",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            {},
        )
        return (out.get("message") or {}).get("content") or out.get("response") or ""


class OpenAICompatClient(ChatClient):
    def complete(self, system: str, user: str) -> str:
        out = self._post(
            self.base_url.rstrip("/") + "/chat/completions",
            {
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            {"authorization": f"Bearer {self.api_key}"} if self.api_key else {},
        )
        return out["choices"][0]["message"]["content"]


def client_for(cfg) -> ChatClient | None:
    """None means "use the rule-based fallback" — never an error."""
    if cfg.is_mock:
        return None
    if cfg.mode == "ollama":
        return OllamaClient(base_url=cfg.base_url, model=cfg.model)
    if not cfg.api_key:
        raise LLMError(
            "VACHAN_API_KEY is empty. Set it, or run with VACHAN_LLM_MODE=mock."
        )
    return OpenAICompatClient(base_url=cfg.base_url, model=cfg.model, api_key=cfg.api_key)


def parse_json(text: str) -> dict:
    """Models wrap JSON in prose or fences no matter how you ask. Survive it."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        if len(parts) >= 2:
            cleaned = parts[1]
            if cleaned[:4].lower() == "json":
                cleaned = cleaned[4:]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise LLMError(f"model did not return JSON: {text[:200]!r}")
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"model returned invalid JSON: {exc}") from exc
