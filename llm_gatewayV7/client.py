"""Python client for LLM Gateway V7. Adds an embed() method on top of V3."""
import os, json, time, httpx
from typing import Any, Optional

# Retry config for transient gateway errors (502/503).
# Free-tier LLM providers rate-limit rapid successive calls,
# so a short backoff between retries is usually enough.
MAX_RETRIES = 3
RETRY_DELAY_SECS = 2

DEFAULT_URL = os.getenv("LLM_GATEWAY_V7_URL", "http://localhost:8107")


class LLM:
    def __init__(self, base_url: str = DEFAULT_URL, timeout: float = 600):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def chat(self, prompt: str = None, *,
             messages: Optional[list] = None,
             system: Any = None,
             provider: str = None, model: str = None,
             max_tokens: int = 2048, temperature: float = 0.7,
             tools: Optional[list] = None,
             tool_choice: Any = None,
             cache_system: Optional[bool] = None,
             reasoning: Optional[str] = None,
             response_format: Any = None,
             auto_route: Optional[str] = None) -> dict:
        body = {
            "prompt": prompt, "messages": messages, "system": system,
            "provider": provider, "model": model,
            "max_tokens": max_tokens, "temperature": temperature, "stream": False,
            "tools": tools, "tool_choice": tool_choice,
            "cache_system": cache_system, "reasoning": reasoning,
            "response_format": response_format,
            "auto_route": auto_route,
        }
        body = {k: v for k, v in body.items() if v is not None}

        # Retry on 5xx errors — free-tier providers often rate-limit
        # rapid successive calls during agent loops.
        last_resp = None
        for attempt in range(MAX_RETRIES):
            last_resp = httpx.post(f"{self.base_url}/v1/chat", json=body, timeout=self.timeout)
            if last_resp.status_code < 500:
                last_resp.raise_for_status()
                return last_resp.json()
            # 5xx — wait before retrying (rate limit cooldown)
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_SECS)
        # All retries exhausted — raise the last error
        raise httpx.HTTPStatusError(
            f"{provider} - Gateway {last_resp.status_code} after {MAX_RETRIES} attempts",
            request=last_resp.request, response=last_resp,
        )

    def stream(self, prompt: str = None, *, messages=None, system=None,
               provider: str = None, model: str = None,
               max_tokens: int = 2048, temperature: float = 0.7,
               tools=None, tool_choice=None,
               cache_system=None, reasoning=None, response_format=None):
        body = {
            "prompt": prompt, "messages": messages, "system": system,
            "provider": provider, "model": model,
            "max_tokens": max_tokens, "temperature": temperature, "stream": True,
            "tools": tools, "tool_choice": tool_choice,
            "cache_system": cache_system, "reasoning": reasoning,
            "response_format": response_format,
        }
        body = {k: v for k, v in body.items() if v is not None}
        with httpx.stream("POST", f"{self.base_url}/v1/chat", json=body, timeout=self.timeout) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                d = json.loads(line[6:])
                if "delta" in d:
                    yield d["delta"]
                if d.get("done") or d.get("error"):
                    return

    def capabilities(self):
        return httpx.get(f"{self.base_url}/v1/capabilities", timeout=30).json()

    def embed(self, text: str,
              task_type: str = "retrieval_document",
              provider: Optional[str] = None) -> dict:
        """Returns {provider, model, embedding, dim, latency_ms, attempted}."""
        body = {"text": text, "task_type": task_type}
        if provider:
            body["provider"] = provider
        r = httpx.post(f"{self.base_url}/v1/embed", json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()


def ask(prompt: str, provider: str = None, **kw) -> str:
    return LLM().chat(prompt, provider=provider, **kw)["text"]


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else None
    print(ask("Say hello in one short line.", provider=p))
