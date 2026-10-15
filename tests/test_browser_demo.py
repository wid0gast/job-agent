"""Drive the demo site with the real browser tools, no model involved."""

import threading
from http.server import ThreadingHTTPServer

import pytest

from job_agent import demo_site
from job_agent.browser import Browser
from job_agent.tools import BrowserTools


@pytest.fixture(scope="module")
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), demo_site.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    b = Browser(tmp_path_factory.mktemp("profile"), headless=True)
    yield b
    b.close()


def by_name(snapshot, text):
    return next(e for e in snapshot["elements"] if text.lower() in e["name"].lower())


def test_hidden_instructions_never_reach_the_model(site, browser):
    browser.goto(f"{site}/jobs/101")
    snap = browser.snapshot()
    assert "Build internal tools" in snap["text"]
    assert demo_site.TRAP_WORD not in snap["text"]
    assert "Ignore previous instructions" not in snap["text"]
    assert demo_site.TRAP_WORD not in browser.read_text()["text"]


def test_final_submit_requires_submit_application(site, browser, tmp_path, capsys):
    tools = BrowserTools(browser)
    snap = tools.goto(f"{site}/apply/101")["page"]
    # Sign up, then the verification code printed by the site.
    snap = tools.fill(by_name(snap, "email")["ref"], "jane@example.com")["page"]
    passwords = [e for e in snap["elements"] if e.get("type") == "password"]
    for field in passwords:
        snap = tools.fill(field["ref"], "Sup3rSecret!")["page"]
    snap = tools.click(by_name(snap, "create account")["ref"])["page"]
    assert "Verify your email" in snap["text"]
    code = capsys.readouterr().out.split("jane@example.com: ")[1][:6]
    snap = tools.fill(by_name(snap, "verification code")["ref"], code)["page"]
    snap = tools.click(by_name(snap, "verify")["ref"])["page"]

    # The form lives in an iframe; refs must still resolve.
    assert "DemoATS application" in snap["text"]
    names = [e["name"] for e in snap["elements"]]
    assert not any("leave this field empty" in n.lower() for n in names), "honeypot exposed"
    assert any("company careers page" in n.lower() for n in names), "styled radio missing"

    snap = tools.fill(by_name(snap, "first name")["ref"], "Jane")["page"]
    snap = tools.fill(by_name(snap, "last name")["ref"], "Doe")["page"]
    snap = tools.select(by_name(snap, "legally authorized")["ref"], "Yes")["page"]
    snap = tools.set_checked(by_name(snap, "company careers page")["ref"], True)["page"]
    snap = tools.set_checked(by_name(snap, "i certify")["ref"], True)["page"]
    resume = tmp_path / "Jane_Doe_Resume.pdf"
    browser.render_pdf("<h1>Jane Doe</h1>", resume)
    browser.upload(by_name(snap, "resume")["ref"], resume)
    snap = browser.snapshot()

    submit = by_name(snap, "submit application")
    with pytest.raises(PermissionError):
        tools.click(submit["ref"])
    browser.click(submit["ref"])  # What submit_application does after approval.
    snap = browser.snapshot()
    assert "Application received" in snap["text"]
    report = capsys.readouterr().out
    assert "FAIL" not in report and report.count("PASS") == 4
