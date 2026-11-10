"""Our own message format and its adapters (pipeline/llm.py), without a network."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import llm  # noqa: E402

TOOLS = [llm.Tool("get_frame", "A frame", {"type": "object", "properties": {"t": {"type": "number"}}})]
PICTURE = llm.Image(b"\xff\xd8jpeg", "Frame at 12 s")


class Wire:
    """Stands in for the network: records each request and answers from a list."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    def __call__(self, url, body, headers, timeout):
        self.requests.append((url, json.loads(json.dumps(body)), headers))
        return self.answers.pop(0)


def responses_answer(n, calls=(), text="", cached=0):
    out = [{"type": "function_call", "call_id": f"c{n}{i}", "name": name, "arguments": json.dumps(args)}
           for i, (name, args) in enumerate(calls)]
    if text:
        out.append({"type": "message", "content": [{"type": "output_text", "text": text}]})
    return {"id": f"r{n}", "output": out,
            "usage": {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": cached},
                      "output_tokens": 50, "output_tokens_details": {"reasoning_tokens": 30}}}


def test_responses_sends_only_what_is_new_and_pictures_after_the_outputs():
    wire = Wire(responses_answer(1, [("get_frame", {"t": 12})]), responses_answer(2, text="Fertig.", cached=900))
    client = llm.OpenAIResponses("secret", "m", "high", post=wire)
    messages = [llm.Message("user", [llm.Text("Hallo")])]
    first = client.complete("Sei knapp.", messages, TOOLS)
    assert [c.name for c in first.calls] == ["get_frame"] and first.calls[0].arguments == {"t": 12}
    assert first.usage.as_dict() == {"input_tokens": 1000, "cached_tokens": 0, "output_tokens": 50,
                                     "reasoning_tokens": 30}
    url, body, headers = wire.requests[0]
    assert url.endswith("/responses") and body["model"] == "m" and body["reasoning"] == {"effort": "high"}
    assert body["instructions"] == "Sei knapp." and body["tools"][0]["name"] == "get_frame"
    assert body["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "Hallo"}]}]
    assert "previous_response_id" not in body and headers["Authorization"] == "Bearer secret"
    messages.append(first.message())
    messages.append(llm.Message("user", [llm.ToolResult("c10", "the picture follows", [PICTURE])]))
    second = client.complete("Sei knapp.", messages, TOOLS)
    assert second.text == "Fertig." and not second.calls and second.usage.cached_tokens == 900
    body = wire.requests[1][1]
    assert body["previous_response_id"] == "r1"                       # the history stays with the provider
    assert [x.get("type") or x["role"] for x in body["input"]] == ["function_call_output", "user"]
    picture = body["input"][1]["content"]
    assert picture[0]["text"] == "Frame at 12 s" and picture[1]["image_url"].startswith("data:image/jpeg;base64,")
    assert "secret" not in repr(client)


def test_responses_sends_everything_for_a_new_conversation():
    wire = Wire(responses_answer(1), responses_answer(2))
    client = llm.OpenAIResponses("k", "m", post=wire)
    client.complete("i", [llm.Message("user", [llm.Text("a")])], [])
    other = [llm.Message("user", [llm.Text("b")]),
             llm.Message("assistant", [llm.Text("ok"), llm.ToolCall("x", "get_frame", {"t": 1})]),
             llm.Message("user", [llm.ToolResult("x", "done")])]
    client.complete("i", other, [])
    body = wire.requests[1][1]
    assert "previous_response_id" not in body
    assert [x.get("type") or x["role"] for x in body["input"]] == ["user", "assistant", "function_call",
                                                                    "function_call_output"]


def test_chat_completions_maps_tools_and_pictures():
    wire = Wire({"choices": [{"message": {"content": None, "tool_calls": [
                    {"id": "t1", "type": "function", "function": {"name": "get_frame", "arguments": "{\"t\": 3}"}}]}}],
                 "usage": {"prompt_tokens": 400, "prompt_tokens_details": {"cached_tokens": 100},
                           "completion_tokens": 20}})
    client = llm.OpenAIChat(None, "local-model", "http://127.0.0.1:8000/v1/", post=wire)
    messages = [llm.Message("user", [llm.Text("Hallo")]),
                llm.Message("assistant", [llm.ToolCall("t0", "get_frame", {"t": 1})]),
                llm.Message("user", [llm.ToolResult("t0", "the picture follows", [PICTURE])])]
    reply = client.complete("Sei knapp.", messages, TOOLS)
    assert reply.calls == [llm.ToolCall("t1", "get_frame", {"t": 3})] and reply.text == ""
    assert reply.usage.input_tokens == 400 and reply.usage.cached_tokens == 100 and reply.usage.output_tokens == 20
    url, body, headers = wire.requests[0]
    assert url == "http://127.0.0.1:8000/v1/chat/completions" and headers == {}       # a local server needs no key
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]           # the picture follows the tool's answer
    assert body["messages"][2]["tool_calls"][0]["function"]["name"] == "get_frame"
    assert body["messages"][4]["content"][1]["type"] == "image_url"
    assert body["tools"][0]["function"]["name"] == "get_frame" and "reasoning_effort" not in body


def test_a_passing_server_error_is_tried_again(monkeypatch):
    import io
    import urllib.error
    answers = [urllib.error.HTTPError("u", 520, "unknown", {}, io.BytesIO(b"error code: 520")),
               io.BytesIO(b'{"id": "r1"}')]

    class Answer:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self.body

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return Answer(a)
    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    assert llm._post("https://x", {}, {}, 1) == {"id": "r1"}
    answers[:] = [urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b"bad request"))]
    with pytest.raises(llm.ModelError, match="400"):
        llm._post("https://x", {}, {}, 1)                                     # a wrong request is not repeated


def test_the_server_chooses_the_model_of_a_stage():
    assert llm.stage_config("cut") == llm.DEFAULTS["cut"]
    chosen = llm.stage_config("cut", {"cut": {"model": "gpt-6.1-mini"}, "summary": {"model": "x"}})
    assert chosen == {**llm.DEFAULTS["cut"], "model": "gpt-6.1-mini"}
    assert isinstance(llm.make(chosen, lambda s: "k"), llm.OpenAIResponses)
    local = llm.make({"adapter": "openai-chat", "model": "qwen", "base_url": "http://127.0.0.1:11434/v1"},
                     lambda s: pytest.fail("a local server needs no key"))
    assert isinstance(local, llm.OpenAIChat) and local.base_url == "http://127.0.0.1:11434/v1"
    with pytest.raises(llm.ModelError):
        llm.make(chosen, lambda s: None)                                        # no key
    with pytest.raises(llm.ModelError):
        llm.make({"adapter": "carrier-pigeon", "model": "x"}, lambda s: "k")
