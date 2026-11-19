"""Minimal client for a local Ollama server (https://ollama.com).

Only the standard library + requests; talks to the documented REST API:
  GET  /api/tags   list installed models
  POST /api/chat   chat completion (stream=false)
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, model: str, host: str = "http://localhost", port: int = 11434,
                 temperature: float = 0.2, num_ctx: int = 32768, timeout: float = 900,
                 seed: int | None = 42, keep_alive: str = "30m", max_retries: int = 2,
                 session: requests.Session | None = None):
        host = host.rstrip("/")
        if not host.startswith("http"):
            host = f"http://{host}"
        self.base_url = f"{host}:{port}" if port else host
        self.model = model
        self.options: dict[str, Any] = {"temperature": temperature, "num_ctx": num_ctx}
        if seed is not None:
            self.options["seed"] = seed
        self.timeout = timeout
        self.keep_alive = keep_alive
        self.max_retries = max_retries
        self.session = session or requests.Session()

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "OllamaClient":
        return cls(
            model=cfg["model"],
            host=cfg.get("host", "http://localhost"),
            port=cfg.get("port", 11434),
            temperature=float(cfg.get("temperature", 0.2)),
            num_ctx=int(cfg.get("num_ctx", 32768)),
            timeout=float(cfg.get("timeout_seconds", 900)),
            seed=cfg.get("seed", 42),
            keep_alive=str(cfg.get("keep_alive", "30m")),
        )

    def check(self) -> None:
        """Fail early with a helpful message if the server or model is missing."""
        try:
            resp = self.session.get(f"{self.base_url}/api/tags", timeout=10)
            resp.raise_for_status()
        except requests.RequestException as err:
            raise LLMError(
                f"Cannot reach Ollama at {self.base_url} ({err}). Start it with `ollama serve`, "
                f"or fix summarize.ollama.host/port in the config."
            ) from err
        names = {m.get("name") for m in resp.json().get("models", [])}
        wanted = {self.model, f"{self.model}:latest"}
        if not names & wanted:
            raise LLMError(
                f"Model '{self.model}' is not installed in Ollama at {self.base_url}. "
                f"Run `ollama pull {self.model}`. Installed: {', '.join(sorted(n for n in names if n)) or 'none'}"
            )

    def chat(self, system: str, user: str, json_mode: bool = False) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "options": self.options,
            "keep_alive": self.keep_alive,
        }
        if json_mode:
            payload["format"] = "json"
        last: Exception | None = None
        for attempt in range(1, self.max_retries + 2):
            try:
                resp = self.session.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
                if resp.status_code != 200:
                    raise LLMError(f"Ollama HTTP {resp.status_code}: {resp.text[:300]}")
                content = resp.json().get("message", {}).get("content", "")
                return THINK_RE.sub("", content).strip()
            except (requests.RequestException, LLMError, ValueError) as err:
                last = err
                log.warning("Ollama call failed (attempt %d): %s", attempt, err)
                time.sleep(min(30, 2 ** attempt))
        raise LLMError(f"Ollama call failed after retries: {last}")

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        """Chat in JSON mode; one retry with a reminder if the reply does not parse."""
        reply = self.chat(system, user, json_mode=True)
        try:
            return parse_json_reply(reply)
        except ValueError:
            reply = self.chat(system, user + "\n\nReply with ONE valid JSON object only.", json_mode=True)
            return parse_json_reply(reply)


def parse_json_reply(text: str) -> dict[str, Any]:
    text = THINK_RE.sub("", text).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"No JSON object in model reply: {text[:200]}")
        obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("Model reply is JSON but not an object")
    return obj
