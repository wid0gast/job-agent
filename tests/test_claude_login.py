"""Connect Claude from the dashboard, against a fake `claude` that signs in like the real one."""

import stat

import pytest

from job_agent import claude_login

FAKE = """#!/usr/bin/env python3
import json, os, sys
state = os.environ["FAKE_CLAUDE_STATE"]
if sys.argv[1:3] == ["auth", "status"]:
    signed_in = os.path.exists(state)
    print(json.dumps({"loggedIn": signed_in, "authMethod": "claude.ai" if signed_in else "none",
                      **({"email": "jane@example.com", "subscriptionType": "pro"} if signed_in else {})}))
elif sys.argv[1:3] == ["auth", "login"]:
    print("Opening browser to sign in…")
    print("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&state=x")
    sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
    code = sys.stdin.readline().strip()
    if code != "good-code":
        print("Invalid code"); sys.exit(1)
    open(state, "w").close(); print("Login successful.")
elif sys.argv[1:3] == ["auth", "logout"]:
    os.path.exists(state) and os.remove(state)
"""


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(FAKE)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(tmp_path / "signed-in"))
    monkeypatch.setattr(claude_login, "_status", None)
    monkeypatch.setattr(claude_login, "_login", None)


def test_sign_in_with_the_code_from_the_browser(fake_claude):
    assert claude_login.status(fresh=True) == {"installed": True, "loggedIn": False, "email": "",
                                               "method": "none", "plan": ""}
    login = claude_login.start()
    assert login.url.startswith("https://claude.com/cai/oauth/authorize")
    info = claude_login.finish("good-code")
    assert info["loggedIn"] and claude_login.describe(info) == "Claude Pro · jane@example.com"
    claude_login.logout()
    assert not claude_login.status()["loggedIn"]


def test_a_wrong_code_says_so(fake_claude):
    claude_login.start()
    with pytest.raises(ValueError, match="didn't work"):
        claude_login.finish("typo")
    assert not claude_login.status()["loggedIn"]
