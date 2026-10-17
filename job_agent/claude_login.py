"""Signing in to Claude Code from the dashboard.

Claude Code's own sign-in (`claude auth login --claudeai`) opens a Claude page in the
browser; after you approve, that page shows a code, which goes back to the waiting
command. This runs that command, shows its link, passes your code on, and reads
`claude auth status` to see who's signed in. Your credentials stay with Claude Code.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time

_status: tuple[float, dict] | None = None
_login: "Login | None" = None
_lock = threading.Lock()


def claude_env() -> dict:
    # An API key in the environment would take precedence over the Claude sign-in.
    return {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}


def status(fresh: bool = False) -> dict:
    """{"installed", "loggedIn", "email", "plan", ...}, cached for a few seconds."""
    global _status
    with _lock:
        if not fresh and _status and time.time() - _status[0] < 5:
            return _status[1]
        executable = shutil.which("claude")
        result = {"installed": bool(executable), "loggedIn": False}
        if executable:
            try:
                done = subprocess.run([executable, "auth", "status", "--json"], capture_output=True,
                                      text=True, timeout=20, env=claude_env())
                data = json.loads(done.stdout or "{}")
                result.update(loggedIn=bool(data.get("loggedIn")), email=data.get("email") or "",
                              method=data.get("authMethod") or "", plan=data.get("subscriptionType") or "")
            except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as error:
                result["error"] = f"{type(error).__name__}: {error}"[:200]
        _status = (time.time(), result)
        return result


def describe(info: dict) -> str:
    """'Claude Pro · you@example.com' and the like."""
    plan = {"pro": "Claude Pro", "max": "Claude Max", "team": "Claude Team",
            "enterprise": "Claude Enterprise"}.get(info.get("plan", ""), "Claude")
    if info.get("method") == "console":
        plan = "Anthropic Console"
    return f"{plan} · {info['email']}" if info.get("email") else plan


class Login:
    """One running `claude auth login`, waiting for the code from the browser."""

    def __init__(self):
        self.url, self.output, self.done = "", [], False
        self.process = subprocess.Popen(
            [shutil.which("claude"), "auth", "login", "--claudeai"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=claude_env())
        threading.Thread(target=self.read, daemon=True).start()

    def read(self) -> None:
        buffer = ""
        while chunk := self.process.stdout.read(1):  # The prompt for the code has no newline.
            buffer += chunk
            if chunk == "\n" or buffer.endswith("> "):
                self.output.append(buffer.strip())
                found = re.search(r"https://\S+", buffer)
                if found and not self.url:
                    self.url = found.group(0)
                buffer = ""
        self.done = True

    def send_code(self, code: str) -> None:
        self.process.stdin.write(code.strip() + "\n")
        self.process.stdin.flush()

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> None:
        if self.running:
            self.process.terminate()


def start() -> Login:
    global _login
    if _login and _login.running:
        return _login
    _login = Login()
    deadline = time.time() + 10  # Until it prints the sign-in link.
    while not _login.url and _login.running and time.time() < deadline:
        time.sleep(0.1)
    return _login


def current() -> Login | None:
    return _login


def finish(code: str) -> dict:
    """Pass the browser's code to the waiting sign-in; returns the new status."""
    if not (_login and _login.running):
        raise ValueError("The sign-in had stopped. Start it again.")
    _login.send_code(code)
    deadline = time.time() + 30
    while _login.running and time.time() < deadline:
        time.sleep(0.2)
    info = status(fresh=True)
    if not info["loggedIn"]:
        last = next((line for line in reversed(_login.output) if line), "")
        _login.stop()
        raise ValueError("That code didn't work" + (f" ({last[:160]})" if last else "") + ". Start again.")
    return info


def logout() -> None:
    executable = shutil.which("claude")
    if executable:
        subprocess.run([executable, "auth", "logout"], capture_output=True, timeout=20, env=claude_env())
    status(fresh=True)
