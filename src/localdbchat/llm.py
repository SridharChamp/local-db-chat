"""Client for a local Ollama server. Refuses to talk to anything off this machine."""

from __future__ import annotations

import httpx

from .util import is_loopback_url


class LLMError(Exception):
    """The local model could not answer; the message says how to fix it."""


class OllamaClient:
    def __init__(self, url: str, model: str, *, embed_model: str = "", timeout: float = 120.0,
                 num_ctx: int = 8192, keep_alive: str = "30m", allow_remote: bool = False):
        url = (url or "").rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise LLMError(f"ollama_url must start with http:// or https:// (got '{url}').")
        if not allow_remote and not is_loopback_url(url):
            raise LLMError(
                f"Refusing to send data to {url}: it is not this machine. local-db-chat only talks "
                "to a model on localhost. Set allow_remote_llm: true only if that host is yours.")
        if not model:
            raise LLMError("No model configured. Set 'model' in config.yaml, e.g. qwen2.5-coder:7b.")
        self.url = url
        self.model = model
        self.embed_model = embed_model
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        # trust_env=False: never route through HTTP(S)_PROXY, which could send data off the machine.
        self._http = httpx.Client(base_url=url, timeout=httpx.Timeout(timeout, connect=5.0),
                                  trust_env=False)

    # ----------------------------------------------------------------- calls
    def chat(self, messages: list[dict], *, temperature: float = 0.0) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            # Deterministic decoding: the same question gives the same SQL.
            "options": {"temperature": temperature, "top_k": 1, "seed": 7, "num_ctx": self.num_ctx},
        }
        data = self._post("/api/chat", payload, self.model)
        content = ((data.get("message") or {}).get("content") or "").strip()
        if not content:
            raise LLMError("The model returned an empty reply. Try again or use a larger model.")
        return content

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.embed_model:
            raise LLMError("No embed_model configured.")
        data = self._post("/api/embed", {"model": self.embed_model, "input": texts,
                                         "keep_alive": self.keep_alive}, self.embed_model)
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise LLMError("The embedding model returned an unexpected response.")
        return vectors

    def list_models(self) -> list[str]:
        try:
            resp = self._http.get("/api/tags")
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise LLMError(self._not_running()) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach Ollama at {self.url}: {exc}") from exc
        return [m.get("name", "") for m in resp.json().get("models", [])]

    def warm_up(self) -> None:
        """Load the model into memory now, so the first question is not slow."""
        self._post("/api/generate", {"model": self.model, "prompt": "", "keep_alive": self.keep_alive},
                   self.model)

    def close(self) -> None:
        self._http.close()

    # --------------------------------------------------------------- helpers
    def _post(self, path: str, payload: dict, model: str) -> dict:
        try:
            resp = self._http.post(path, json=payload)
        except httpx.ConnectError as exc:
            raise LLMError(self._not_running()) from exc
        except httpx.TimeoutException as exc:
            raise LLMError("The model took too long to reply. A smaller model or a GPU will help.") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach Ollama at {self.url}: {exc}") from exc
        if resp.status_code == 404 or "not found" in resp.text.lower() and resp.status_code >= 400:
            raise LLMError(f"The model '{model}' is not installed. Run: ollama pull {model}")
        if resp.status_code >= 400:
            raise LLMError(f"Ollama returned an error ({resp.status_code}): {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise LLMError("Ollama returned a reply that is not JSON.") from exc

    def _not_running(self) -> str:
        return (f"Ollama is not running at {self.url}. Install it from ollama.com, then run "
                f"'ollama serve' and 'ollama pull {self.model}'.")
