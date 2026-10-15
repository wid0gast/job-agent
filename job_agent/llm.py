"""OpenRouter client: tool-calling turns for agents, JSON answers for pipeline steps.

Every response's `usage.cost` is added up so a run stops at the spending cap.
Free models are rate limited (about 20 requests a minute), so 429s are retried.
"""

import json
import re
import time


class SpendCapReached(RuntimeError):
    pass


class OpenRouter:
    URL = "https://openrouter.ai/api/v1/chat/completions"
    # Reasoning models can spend the whole budget thinking, and some ignore
    # "effort"; a token cap on thinking is honored, leaving room to answer.
    REASONING = {"max_tokens": 1024}

    def __init__(self, api_key: str, model: str, spend_cap_usd: float = 1.0,
                 data_collection: str = "deny"):
        import httpx  # Local import: the hosted build uses the stdlib broker client.

        self.api_key, self.model = api_key, model
        self.spend_cap_usd, self.data_collection = spend_cap_usd, data_collection
        self.spent_usd = 0.0
        self.calls = 0
        self._json_format = 0  # Index into _post_json's formats that this model accepts.
        self.client = httpx.Client(timeout=httpx.Timeout(180, connect=15))

    def post(self, payload: dict) -> dict:
        if self.spent_usd >= self.spend_cap_usd:
            raise SpendCapReached(
                f"Spending cap of ${self.spend_cap_usd:.2f} reached (spent ${self.spent_usd:.4f}).")
        # "a,b,c" in OPENROUTER_MODEL: OpenRouter tries them in order (free models
        # are often rate-limited upstream).
        models = [m.strip() for m in self.model.split(",") if m.strip()]
        payload = {
            **({"models": models} if len(models) > 1 else {"model": models[0]}),
            "max_tokens": 4096,
            "provider": {"require_parameters": True, "data_collection": self.data_collection},
            **payload,
        }
        for attempt in range(5):
            response = self.client.post(
                self.URL, headers={"Authorization": f"Bearer {self.api_key}"}, json=payload)
            if response.status_code in (429, 502, 503) and attempt < 4:
                wait = float(response.headers.get("retry-after") or 0) or 8 * (attempt + 1)
                time.sleep(min(wait, 60))
                continue
            break
        if response.status_code != 200:
            raise RuntimeError(f"OpenRouter HTTP {response.status_code}: {response.text[:300]}")
        body = response.json()
        if "error" in body:
            raise RuntimeError(f"OpenRouter error: {body['error']}")
        self.calls += 1
        self.spent_usd += float((body.get("usage") or {}).get("cost") or 0)
        return body

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        """One agent turn with tool declarations (used by harness.AgentRun)."""
        body = self.post({"messages": messages, "tools": tools})
        choice = body["choices"][0]
        if choice.get("finish_reason") not in ("stop", "tool_calls", "end_turn"):
            raise RuntimeError(f"Incomplete response: {choice.get('finish_reason')}")
        return choice["message"]

    def json(self, system: str, user: str, schema: dict, name: str = "answer") -> dict:
        """A JSON object matching `schema`, retried once if the model gets it wrong.

        Asks for strict structured output; models that don't support it still
        get the schema in the prompt, and the reply is checked here either way.
        """
        messages = [
            {"role": "system", "content": f"{system}\n\nReply with only a JSON object "
             f"matching this JSON Schema:\n{json.dumps(schema)}"},
            {"role": "user", "content": user},
        ]
        error = ""
        for _ in range(2):
            body = self._post_json(messages, schema, name)
            choice = body["choices"][0]
            text = choice["message"].get("content") or ""
            if not text and choice.get("finish_reason") == "length":
                raise RuntimeError("The model ran out of output tokens before answering "
                                   f"({body.get('model')}). Try another model.")
            try:
                value = parse_json(text)
                check(value, schema)
                return value
            except ValueError as problem:
                error = str(problem)
                messages += [{"role": "assistant", "content": text},
                             {"role": "user", "content": f"That was not valid: {error}. "
                              "Reply again with only the corrected JSON object."}]
        raise ValueError(f"Model did not return valid JSON: {error}")

    def _post_json(self, messages: list[dict], schema: dict, name: str) -> dict:
        """Strict schema if the model's providers support it, then plain JSON mode, then none."""
        formats = [{"type": "json_schema",
                    "json_schema": {"name": name, "strict": True, "schema": schema}},
                   {"type": "json_object"}, None]
        for index in range(self._json_format, len(formats)):
            payload = {"messages": messages, "max_tokens": 16000}
            if self.REASONING:
                payload["reasoning"] = self.REASONING
            if formats[index]:
                payload["response_format"] = formats[index]
            try:
                body = self.post(payload)
            except RuntimeError as failure:
                # 400/404: no provider for this model accepts that response_format.
                if index + 1 < len(formats) and re.search(r"HTTP (400|404)", str(failure)):
                    continue
                raise
            self._json_format = index  # Remember what worked for the rest of the run.
            return body
        raise RuntimeError("unreachable")


class Broker(OpenRouter):
    """The class app store's model broker. Same chat API, but one model per visit (no
    fallback list), its own reasoning settings, and a budget it enforces itself."""

    REASONING = None

    def __init__(self, model: str, spend_cap_usd: float = 0.5, data_collection: str = "deny"):
        import os

        import httpx

        super().__init__("", model, spend_cap_usd, data_collection)
        # The broker is a class service, reached directly rather than through the web proxy.
        self.client = httpx.Client(timeout=httpx.Timeout(180, connect=15), trust_env=False)
        self.URL = os.environ["WORKSHOP_BROKER_URL"].rstrip("/") + "/v1/llm/chat/completions"
        self.token = os.environ["WORKSHOP_RUN_TOKEN"]
        self.profile_id = os.environ.get("WORKSHOP_PROFILE_ID", "")

    stream = True  # Long answers arrive piece by piece, so the connection never sits idle.

    def post(self, payload: dict) -> dict:
        import uuid

        import httpx

        if self.spent_usd >= self.spend_cap_usd:
            raise SpendCapReached(f"This visit's model budget (${self.spend_cap_usd:.2f}) is used up.")
        payload = {"model": self.model.split(",")[0].strip(), "max_tokens": 4096,
                   "provider": {"data_collection": self.data_collection}, **payload}
        for attempt in range(4):
            headers = {"Authorization": f"Bearer {self.token}", "X-Workshop-Contract-Version": "workshop.app.v1",
                       "X-Workshop-Operation-ID": str(uuid.uuid4()), "X-Workshop-Expected-Profile": self.profile_id,
                       "X-Workshop-Attempt-Limit": "1"}
            started = time.time()
            try:
                if self.stream:
                    body = self._streamed(payload, headers)
                    if body is None:  # The broker refused a streamed request: use plain ones.
                        Broker.stream = False
                        continue
                    if body == "busy" and attempt < 3:
                        time.sleep(5 * (attempt + 1))
                        continue
                    if body == "busy":
                        raise RuntimeError("The model service stayed busy; try again shortly.")
                else:
                    response = self.client.post(self.URL, headers=headers, json=payload)
                    if response.status_code in (429, 502, 503) and attempt < 3:
                        time.sleep(5 * (attempt + 1))
                        continue
                    if response.status_code != 200:
                        raise RuntimeError(f"Model broker HTTP {response.status_code}: {response.text[:300]}")
                    body = response.json()
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout) as error:
                if attempt < 3:
                    continue
                raise RuntimeError(f"The model service dropped the request after {time.time() - started:.0f}s "
                                   f"({type(error).__name__}, {'streamed' if self.stream else 'plain'}).") from None
            if "error" in body:
                raise RuntimeError(f"Model broker error: {body['error']}")
            self.calls += 1
            self.spent_usd += float((body.get("usage") or {}).get("cost") or 0)
            return body
        raise RuntimeError("The model service kept refusing the request.")

    def _streamed(self, payload: dict, headers: dict) -> dict | str | None:
        """One streamed chat completion, reassembled into the usual response shape."""
        import json as _json

        with self.client.stream("POST", self.URL, headers=headers, json={**payload, "stream": True}) as response:
            if response.status_code != 200:
                text = response.read().decode(errors="replace")
                if response.status_code == 400 and "INVALID_REQUEST" in text:
                    return None
                if response.status_code in (429, 502, 503):
                    return "busy"
                raise RuntimeError(f"Model broker HTTP {response.status_code}: {text[:300]}")
            content, calls, finish, usage, model = [], {}, None, {}, None
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = _json.loads(data)
                if "error" in chunk:
                    return {"error": chunk["error"]}
                usage, model = chunk.get("usage") or usage, chunk.get("model") or model
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        content.append(delta["content"])
                    for call in delta.get("tool_calls") or []:
                        slot = calls.setdefault(call.get("index", 0), {
                            "id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                        slot["id"] = call.get("id") or slot["id"]
                        function = call.get("function") or {}
                        slot["function"]["name"] += function.get("name") or ""
                        slot["function"]["arguments"] += function.get("arguments") or ""
                    finish = choice.get("finish_reason") or finish
        message = {"role": "assistant", "content": "".join(content) or None}
        if calls:
            message["tool_calls"] = [calls[i] for i in sorted(calls)]
        return {"model": model, "choices": [{"message": message, "finish_reason": finish}], "usage": usage}


def client(settings):
    """The model for this run: the class broker when hosted, else OpenRouter (None if unset)."""
    from . import config

    if config.HOSTED:
        import os

        if not (settings.model and os.environ.get("WORKSHOP_BROKER_URL")):
            return None  # No class models this visit; the dashboard says why.
        return Broker(settings.model, settings.spend_cap_usd, settings.data_collection)
    if settings.api_key and settings.model:
        return OpenRouter(settings.api_key, settings.model, settings.spend_cap_usd, settings.data_collection)
    return None


def parse_json(text: str):
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    if start > 0:
        text = text[start:]
    try:
        return json.JSONDecoder().raw_decode(text)[0]
    except json.JSONDecodeError as error:
        raise ValueError(f"not JSON ({error})") from None


def check(value, schema: dict, path: str = "$") -> None:
    """A small JSON Schema check: types, required keys, enums, nested objects and arrays."""
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "number": (int, float), "boolean": bool}
    if kind in types and not isinstance(value, types[kind]):
        raise ValueError(f"{path} should be {kind}")
    if kind in ("integer", "number") and isinstance(value, bool):
        raise ValueError(f"{path} should be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']}")
    if kind == "object":
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{path}.{key} is missing")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                check(value[key], sub, f"{path}.{key}")
    if kind == "array" and "items" in schema:
        for index, item in enumerate(value):
            check(item, schema["items"], f"{path}[{index}]")
