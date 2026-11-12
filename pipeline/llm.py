"""Language models behind one small interface of our own.

Decision of 2026-11-10: no agent framework and no SDK of a provider. A conversation is a list
of Message objects made of a few kinds of parts (text, picture, tool call, tool result); an
adapter turns it into the request of one kind of API and the answer back into a Reply. Which
adapter and model a stage of the pipeline uses is configuration (stage_config), so models can
be swapped without touching the agent.

Adapters:
- OpenAIResponses: OpenAI's Responses API. It continues a conversation with
  previous_response_id, so pictures already sent are not uploaded again.
- OpenAIChat: the chat completions API that OpenAI and most other providers speak, and the
  servers for open models running locally (vLLM, Ollama); for later stages and local models.
An adapter for Anthropic's Messages API follows when the project has a key for it.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

# The models of the stages, unless the server's configuration says otherwise (models.json in its
# data folder): {"cut": {"adapter": ..., "model": ..., "effort": ...}, ...}
DEFAULTS = {
    "cut": {"adapter": "openai-responses", "model": "gpt-6.1-sol", "effort": "high"},
    "outline": {"adapter": "openai-responses", "model": "gpt-6.1-sol", "effort": "high"},
    "section": {"adapter": "openai-responses", "model": "gpt-6.1-sol", "effort": "high"},     # version 2 of the cut
    "section_light": {"adapter": "openai-responses", "model": "gpt-6-luna", "effort": "medium"},
    "closing": {"adapter": "openai-responses", "model": "gpt-6.1-sol", "effort": "medium"},
    "review": {"adapter": "openai-responses", "model": "gpt-6.1-sol", "effort": "medium"},     # transitions, cut_review.py
    "summary": {"adapter": "openai-responses", "model": "gpt-6.1-sol", "effort": "medium"},
    "topics": {"adapter": "openai-responses", "model": "gpt-6.1-sol", "effort": "medium"},   # feedback, products.py
}


class ModelError(RuntimeError):
    pass


# ---------------------------------------------------------------- the conversation

@dataclass
class Text:
    text: str


@dataclass
class Image:
    data: bytes
    label: str = ""
    mime: str = "image/jpeg"

    def url(self) -> str:
        return f"data:{self.mime};base64," + base64.b64encode(self.data).decode()


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ToolResult:
    call_id: str
    output: str
    images: list[Image] = field(default_factory=list)


@dataclass
class Message:
    role: str                                   # "user" or "assistant"
    parts: list


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict


@dataclass
class Usage:
    input_tokens: int = 0
    cached_tokens: int = 0                      # part of input_tokens the provider had cached
    output_tokens: int = 0
    reasoning_tokens: int = 0                   # part of output_tokens

    def add(self, other: "Usage") -> None:
        for k in vars(self):
            setattr(self, k, getattr(self, k) + getattr(other, k))

    def as_dict(self) -> dict:
        return dict(vars(self))


@dataclass
class Reply:
    text: str
    calls: list[ToolCall]
    usage: Usage

    def message(self) -> Message:
        """The reply as the assistant's turn, to append to the conversation."""
        return Message("assistant", ([Text(self.text)] if self.text else []) + list(self.calls))


# ---------------------------------------------------------------- HTTP

def _post(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if (e.code in (408, 429) or e.code >= 500) and attempt < 2:    # 520 and the like: Cloudflare in front
                time.sleep(10 * (attempt + 1))
                continue
            raise ModelError(f"Das Sprachmodell antwortet mit Fehler {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:     # also a socket reset mid-reply
            if attempt < 2:
                time.sleep(10 * (attempt + 1))
                continue
            raise ModelError(f"Das Sprachmodell ist nicht erreichbar: {e}") from None
    raise ModelError("Das Sprachmodell antwortet nicht.")


# ---------------------------------------------------------------- OpenAI Responses

class OpenAIResponses:
    URL = "https://api.openai.com/v1/responses"

    def __init__(self, key: str, model: str, effort: str = "high", timeout: float = 600,
                 post: Callable[[str, dict, dict, float], dict] = _post):
        self._key, self.model, self.effort, self.timeout, self._post = key, model, effort, timeout, post
        self._history: list | None = None       # the conversation of the last call, and how much of it
        self._sent = 0                          # the provider already holds (with the reply)
        self._last_id = ""

    def __repr__(self) -> str:
        return f"OpenAIResponses({self.model})"   # never the key

    def create(self, body: dict) -> dict:
        """A raw request, for callers that need the API's own features (e.g. the summary's schema)."""
        body = {"model": self.model, "reasoning": {"effort": self.effort}, **body}
        return self._post(self.URL, body, {"Authorization": f"Bearer {self._key}"}, self.timeout)

    def complete(self, instructions: str, messages: list[Message], tools: list[Tool]) -> Reply:
        body = {"instructions": instructions, "tools": [self._tool(t) for t in tools]}
        if messages is self._history and self._last_id and len(messages) >= self._sent:
            body["previous_response_id"] = self._last_id      # only what is new since the last reply
            body["input"] = self._input(messages[self._sent:])
        else:
            body["input"] = self._input(messages)
        r = self.create(body)
        self._history, self._sent, self._last_id = messages, len(messages) + 1, r.get("id", "")
        out = r.get("output", [])
        calls = [ToolCall(o["call_id"], o["name"], json.loads(o.get("arguments") or "{}"))
                 for o in out if o.get("type") == "function_call"]
        text = " ".join(c.get("text", "") for o in out if o.get("type") == "message"
                        for c in o.get("content", []) if c.get("type") == "output_text").strip()
        u = r.get("usage") or {}
        return Reply(text, calls, Usage(u.get("input_tokens", 0), (u.get("input_tokens_details") or {}).get("cached_tokens", 0),
                                        u.get("output_tokens", 0),
                                        (u.get("output_tokens_details") or {}).get("reasoning_tokens", 0)))

    @staticmethod
    def _tool(t: Tool) -> dict:
        return {"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters,
                "strict": False}

    @staticmethod
    def _input(messages: list[Message]) -> list[dict]:
        items = []
        for m in messages:
            if m.role == "assistant":
                text = "".join(p.text for p in m.parts if isinstance(p, Text))
                if text:
                    items.append({"role": "assistant", "content": [{"type": "output_text", "text": text}]})
                items += [{"type": "function_call", "call_id": p.id, "name": p.name,
                           "arguments": json.dumps(p.arguments, ensure_ascii=False)}
                          for p in m.parts if isinstance(p, ToolCall)]
                continue
            results = [p for p in m.parts if isinstance(p, ToolResult)]
            items += [{"type": "function_call_output", "call_id": p.call_id, "output": p.output} for p in results]
            content = []
            for p in m.parts:
                if isinstance(p, Text):
                    content.append({"type": "input_text", "text": p.text})
                elif isinstance(p, Image):
                    content.append({"type": "input_image", "image_url": p.url(), "detail": "high"})
            if content:
                items.append({"role": "user", "content": content})
            for p in results:                       # a tool's pictures follow its output as the user's
                for img in p.images:
                    items.append({"role": "user", "content": [{"type": "input_text", "text": img.label},
                                                              {"type": "input_image", "image_url": img.url(),
                                                               "detail": "high"}]})
        return items


# ---------------------------------------------------------------- OpenAI-compatible chat completions

class OpenAIChat:
    def __init__(self, key: str | None, model: str, base_url: str = "https://api.openai.com/v1",
                 effort: str | None = None, timeout: float = 600,
                 post: Callable[[str, dict, dict, float], dict] = _post):
        self._key, self.model, self.base_url, self.effort = key, model, base_url.rstrip("/"), effort
        self.timeout, self._post = timeout, post

    def __repr__(self) -> str:
        return f"OpenAIChat({self.model} at {self.base_url})"

    def complete(self, instructions: str, messages: list[Message], tools: list[Tool]) -> Reply:
        body = {"model": self.model, "messages": [{"role": "system", "content": instructions}] + self._messages(messages)}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": t.parameters}} for t in tools]
        if self.effort:
            body["reasoning_effort"] = self.effort
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        r = self._post(self.base_url + "/chat/completions", body, headers, self.timeout)
        msg = ((r.get("choices") or [{}])[0]).get("message") or {}
        calls = [ToolCall(c["id"], c["function"]["name"], json.loads(c["function"].get("arguments") or "{}"))
                 for c in msg.get("tool_calls") or []]
        u = r.get("usage") or {}
        return Reply((msg.get("content") or "").strip(), calls,
                     Usage(u.get("prompt_tokens", 0), (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
                           u.get("completion_tokens", 0),
                           (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)))

    @staticmethod
    def _messages(messages: list[Message]) -> list[dict]:
        out = []
        for m in messages:
            if m.role == "assistant":
                calls = [p for p in m.parts if isinstance(p, ToolCall)]
                entry = {"role": "assistant", "content": "".join(p.text for p in m.parts if isinstance(p, Text)) or None}
                if calls:
                    entry["tool_calls"] = [{"id": c.id, "type": "function",
                                            "function": {"name": c.name,
                                                         "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                                           for c in calls]
                out.append(entry)
                continue
            results = [p for p in m.parts if isinstance(p, ToolResult)]
            out += [{"role": "tool", "tool_call_id": p.call_id, "content": p.output} for p in results]
            content = []
            for p in m.parts:
                if isinstance(p, Text):
                    content.append({"type": "text", "text": p.text})
                elif isinstance(p, Image):
                    content.append({"type": "image_url", "image_url": {"url": p.url()}})
            for p in results:                      # tool messages carry no pictures: they follow as the user's
                for img in p.images:
                    content += [{"type": "text", "text": img.label}, {"type": "image_url", "image_url": {"url": img.url()}}]
            if content:
                out.append({"role": "user", "content": content})
        return out


# ---------------------------------------------------------------- configuration

def stage_config(stage: str, overrides: dict | None = None) -> dict:
    """The model of a stage: the default, with what the server's configuration sets for it."""
    return {**DEFAULTS.get(stage, DEFAULTS["cut"]), **((overrides or {}).get(stage) or {})}


def make(config: dict, key: Callable[[str], str | None]):
    """An adapter for a stage's configuration; key gives the key of a service ("openai")."""
    adapter = config.get("adapter", "openai-responses")
    if adapter == "openai-responses":
        k = key("openai")
        if not k:
            raise ModelError("Kein OpenAI-Schlüssel")
        return OpenAIResponses(k, config["model"], config.get("effort", "high"))
    if adapter == "openai-chat":
        service = config.get("key")              # a local server needs none
        return OpenAIChat(key(service) if service else None, config["model"],
                          config.get("base_url", "https://api.openai.com/v1"), config.get("effort"))
    raise ModelError(f"Unbekannter Adapter {adapter}")
