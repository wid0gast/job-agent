"""The class broker client: streamed answers, and answers the class proxy cuts off."""

import httpx
import pytest

from job_agent import llm

DONE = (b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"cost":0.001}}\n\n'
        b"data: [DONE]\n\n")
CUT = b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'  # No finish_reason, no [DONE].


def broker(monkeypatch, body: bytes) -> tuple[llm.Broker, list]:
    monkeypatch.setenv("WORKSHOP_BROKER_URL", "http://broker.test")
    monkeypatch.setenv("WORKSHOP_RUN_TOKEN", "run-token")
    calls = []

    def reply(request):
        calls.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    model = llm.Broker("deepseek/deepseek-v4.1-flash")
    model.client = httpx.Client(transport=httpx.MockTransport(reply))
    return model, calls


def test_streamed_answer_is_reassembled(monkeypatch):
    model, calls = broker(monkeypatch, DONE)
    body = model.post({"messages": [{"role": "user", "content": "hi"}]})
    assert body["choices"][0]["message"]["content"] == "Hello" and len(calls) == 1


def test_cut_off_stream_is_never_used_as_an_answer(monkeypatch):
    model, calls = broker(monkeypatch, CUT)
    with pytest.raises(RuntimeError, match="dropped the request"):
        model.post({"messages": [{"role": "user", "content": "hi"}]})
    assert len(calls) == 4  # A quick drop is retried.


def test_cut_at_the_platform_deadline_is_not_retried(monkeypatch):
    model, calls = broker(monkeypatch, CUT)
    monkeypatch.setattr(llm, "REQUEST_LIMIT_SECONDS", 0)  # As if the drop came at 90 s.
    with pytest.raises(RuntimeError, match="90-second limit"):
        model.post({"messages": [{"role": "user", "content": "hi"}]})
    assert len(calls) == 1


def test_an_empty_reply_in_strict_mode_is_asked_again_without_it(monkeypatch):
    # DeepSeek V4.1 Flash answered every fit check with the schema's bare minimum in strict mode.
    import json

    monkeypatch.setenv("WORKSHOP_BROKER_URL", "http://broker.test")
    monkeypatch.setenv("WORKSHOP_RUN_TOKEN", "run-token")
    sent = []

    def reply(request):
        payload = json.loads(request.content)
        sent.append((payload.get("response_format") or {}).get("type"))
        answer = ({"score": 0, "summary": "", "requirements": []} if sent[-1] == "json_schema" else
                  {"score": 72, "summary": "Reasonable fit.", "requirements": [{"requirement": "Python", "met": "yes"}]})
        chunk = json.dumps({"choices": [{"delta": {"content": json.dumps(answer)}, "finish_reason": "stop"}]})
        return httpx.Response(200, content=f"data: {chunk}\n\ndata: [DONE]\n\n".encode(),
                              headers={"content-type": "text/event-stream"})

    model = llm.Broker("deepseek/deepseek-v4.1-flash")
    model.client = httpx.Client(transport=httpx.MockTransport(reply))
    schema = {"type": "object", "required": ["score", "summary", "requirements"],
              "properties": {"score": {"type": "integer"}, "summary": {"type": "string"},
                             "requirements": {"type": "array", "items": {"type": "object"}}}}
    assert model.json("Judge fit.", "{}", schema, "fit")["score"] == 72
    assert sent == ["json_schema", "json_object"]
    # An empty list of results is a real answer when nothing requires text.
    assert not llm.says_something({"type": "object", "required": ["results"],
                                   "properties": {"results": {"type": "array"}}})


def test_broker_errors_are_said_plainly():
    # As the live platform sent it (trimmed): the raw form once filled a job's page.
    raw = {"code": "UPSTREAM_UNCERTAIN", "message": "The model request was stopped; its provider outcome and "
           "charge remain unconfirmed", "correlationId": "1e08414a-2c1c-40ac-8e8f-31b60e8fec04", "retryable": False,
           "details": {"providerName": "OpenInference", "partialResponse": {"body": "data: {...}"}}}
    said = llm.broker_problem(None, raw)
    assert "OpenInference didn't finish" in said and "ref 1e08414a" in said
    assert "partialResponse" not in said and len(said) < 260
    http = llm.broker_problem(402, '{"error": {"code": "ALLOWANCE_EXHAUSTED", "message": "needs $0.22", '
                                   '"correlationId": "43186022-x"}}')
    assert "reserves room" in http and "ALLOWANCE_EXHAUSTED" in http
    assert "HTTP 500" in llm.broker_problem(500, "upstream exploded")


def test_a_request_the_service_stopped_is_asked_once_more(monkeypatch):
    # About a third of live prepares failed this way, on two models.
    import json

    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    stopped = ('data: ' + json.dumps({"error": {"code": "UPSTREAM_UNCERTAIN", "message": "stopped",
                                                "correlationId": "abc12345", "retryable": False}}) + "\n\n").encode()
    for replies, ok in (([stopped, DONE], True), ([stopped, stopped, DONE], False)):
        queue, calls = list(replies), []
        model, _ = broker(monkeypatch, DONE)
        model.client = httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(1) or httpx.Response(
            200, content=queue.pop(0), headers={"content-type": "text/event-stream"})))
        if ok:
            assert model.post({"messages": []})["choices"][0]["message"]["content"] == "Hello" and len(calls) == 2
        else:
            with pytest.raises(RuntimeError, match="stopped this request partway"):
                model.post({"messages": []})
            assert len(calls) == 2, "only one more try"
