"""Model clients, without calling a model: a fake `claude` command stands in for Claude Code."""

import json
import stat
from dataclasses import replace

import pytest

from job_agent import config, llm

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
record = os.environ["FAKE_CLAUDE_RECORD"]
with open(record, "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read(),
                        "api_key": os.environ.get("ANTHROPIC_API_KEY")}) + "\\n")
print(os.environ["FAKE_CLAUDE_REPLY"])
"""


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(FAKE_CLAUDE)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-reach-claude-code")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")

    def reply(**body):
        monkeypatch.setenv("FAKE_CLAUDE_REPLY", json.dumps({
            "type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01, **body}))

    def calls():
        return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]

    return reply, calls


SCHEMA = {"type": "object", "properties": {"score": {"type": "integer"}}, "required": ["score"]}


def test_claude_code_answers_json_with_your_login(fake_claude):
    reply, calls = fake_claude
    reply(structured_output={"score": 87}, result='{"score":87}')
    model = llm.ClaudeCode()
    assert model.json("Rate job titles.", "ML Intern", SCHEMA, "match_scores") == {"score": 87}
    call = calls()[0]
    assert call["stdin"] == "ML Intern"
    assert call["api_key"] is None  # Your Claude login is used, not an API key in the environment.
    argv = call["argv"]
    assert argv[:3] == ["-p", "--output-format", "json"]
    assert argv[argv.index("--tools") + 1] == ""  # Claude Code's own tools are off.
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--system-prompt") + 1] == "Rate job titles."
    assert json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMA
    # Bulk title ranking goes to Haiku at low effort, named by full ID (the alias is an older model).
    assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    assert argv[argv.index("--effort") + 1] == "low"
    assert model.calls == 1


def test_claude_code_agent_turn_becomes_tool_calls(fake_claude):
    reply, calls = fake_claude
    reply(structured_output={"thought": "Opening the posting.",
                             "tool_calls": [{"name": "open_page", "arguments": {"url": "https://x.test/job"}}]})
    tools = [{"type": "function", "function": {"name": "open_page", "description": "Open a URL.",
                                               "parameters": {"type": "object"}}},
             {"type": "function", "function": {"name": "finish", "description": "End.",
                                               "parameters": {"type": "object"}}}]
    messages = [{"role": "system", "content": "You apply to jobs."},
                {"role": "user", "content": "Apply to job 7."},
                {"role": "assistant", "content": "Reading it.", "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "open_page", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call_1", "name": "open_page", "content": '{"ok": true}'}]
    message = llm.ClaudeCode().complete(messages, tools)
    assert message["content"] == "Opening the posting."
    [call] = message["tool_calls"]
    assert call["function"]["name"] == "open_page"
    assert json.loads(call["function"]["arguments"]) == {"url": "https://x.test/job"}
    sent = calls()[0]
    assert "Apply to job 7." in sent["stdin"] and "Result of open_page [call_1]" in sent["stdin"]
    schema = json.loads(sent["argv"][sent["argv"].index("--json-schema") + 1])
    assert schema["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"] == ["open_page", "finish"]


def test_claude_code_not_signed_in_says_how_to_fix(fake_claude):
    reply, _ = fake_claude
    reply(subtype="error", is_error=True, result="Invalid API key · Please run /login")
    with pytest.raises(RuntimeError, match="sign in with your Claude account"):
        llm.ClaudeCode().json("s", "u", SCHEMA)


def test_tool_results_become_one_user_turn():
    turns = llm.to_anthropic([
        {"role": "user", "content": "Go."},
        {"role": "assistant", "content": "x", "provider_content": [{"type": "thinking", "thinking": "", "signature": "s"},
                                                                  {"type": "tool_use", "id": "a", "name": "t", "input": {}},
                                                                  {"type": "tool_use", "id": "b", "name": "t", "input": {}}]},
        {"role": "tool", "tool_call_id": "a", "name": "t", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "name": "t", "content": "2"},
    ])
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    assert turns[1]["content"][0]["type"] == "thinking"  # Replayed unchanged.
    assert [b["tool_use_id"] for b in turns[2]["content"]] == ["a", "b"]


def test_choosing_a_backend(tmp_path, monkeypatch):
    settings = replace(config.Settings.from_env(), backend="auto")
    monkeypatch.setenv("PATH", str(tmp_path))  # No claude command.
    monkeypatch.setenv("HOME", str(tmp_path))  # No `ant auth login` profile.
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    assert llm.client(settings) is None
    assert llm.client(replace(settings, backend="off")) is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    model = llm.client(settings)
    assert isinstance(model, llm.AnthropicAPI) and model.choose("fit") == ("claude-opus-5-5", "medium")


class Block:
    def __init__(self, **fields):
        self.__dict__.update(fields)

    def model_dump(self, exclude_none=True):
        return dict(self.__dict__)


def test_anthropic_api_keeps_thinking_and_counts_cost(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    model = llm.AnthropicAPI(spend_cap_usd=1.0)
    sent = []

    def create(**kwargs):
        sent.append(kwargs)
        return Block(stop_reason="tool_use", usage=Block(input_tokens=1000, output_tokens=100),
                     content=[Block(type="thinking", thinking="", signature="sig"),
                              Block(type="text", text="Checking."),
                              Block(type="tool_use", id="toolu_1", name="finish", input={"report": "done"})])

    monkeypatch.setattr(model.client.beta.messages, "create", create)
    tools = [{"type": "function", "function": {"name": "finish", "description": "End.",
                                               "parameters": {"type": "object"}}}]
    message = model.complete([{"role": "system", "content": "S"}, {"role": "user", "content": "Go."}], tools,
                             task="explore")
    assert message["tool_calls"][0]["function"] == {"name": "finish", "arguments": '{"report": "done"}'}
    assert message["provider_content"][0] == {"type": "thinking", "thinking": "", "signature": "sig"}
    request = sent[0]
    assert request["model"] == "claude-opus-5-5" and request["system"] == "S"
    assert request["output_config"] == {"effort": "medium"}
    assert request["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert request["context_management"] == {"edits": [{"type": "clear_tool_uses_20250919"}]}
    assert request["tools"][0]["input_schema"] == {"type": "object"}
    assert model.spent_usd == pytest.approx((1000 * 4 + 100 * 20) / 1e6)
    model.spent_usd = 2.0
    with pytest.raises(llm.SpendCapReached):
        model.complete([{"role": "system", "content": "S"}, {"role": "user", "content": "Go."}], tools)


def test_each_task_gets_a_fitting_model():
    model = llm.Model()
    assert model.choose("match_scores") == ("claude-haiku-5-5", "low")
    assert model.choose("resume_facts") == ("claude-sonnet-5-5", "low")
    assert model.choose("research") == ("claude-sonnet-5-5", "medium")
    assert model.choose("fit") == ("claude-opus-5-5", "medium")
    assert model.choose("tailored_resume") == ("claude-opus-5-5", "high")
    assert model.choose("form_answers") == ("claude-opus-5-5", "medium")
    assert model.choose("something new") == ("claude-sonnet-5-5", "medium")
    assert set(model.plan()) == set(llm.TASKS)
    # CLAUDE_MODEL forces one model everywhere (effort still follows the task); a tier can be swapped.
    assert llm.Model("claude-opus-5-5").choose("match_scores") == ("claude-opus-5-5", "low")
    swapped = llm.Model(tiers={"light": "claude-sonnet-5-5", "best": ""})
    assert swapped.choose("match_scores")[0] == "claude-sonnet-5-5"
    assert swapped.choose("fit")[0] == "claude-opus-5-5"  # An empty override keeps the default.


def test_settings_read_tier_overrides(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL_LIGHT", "claude-sonnet-5-5")
    assert config.Settings.from_env().tier_models["light"] == "claude-sonnet-5-5"
