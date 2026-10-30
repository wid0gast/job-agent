"""Opening a page: a passing network error is tried again; anything else fails at once."""

import pytest
from playwright.sync_api import Error as PlaywrightError

from job_agent.pageview import goto


class FlakyPage:
    def __init__(self, errors):
        self.errors, self.opened, self.waits = list(errors), [], []

    def goto(self, url, **options):
        self.opened.append((url, options))
        if self.errors:
            raise PlaywrightError(self.errors.pop(0))
        return "response"

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


def test_a_dropped_socket_is_tried_again():
    page = FlakyPage(["Page.goto: net::ERR_SOCKET_NOT_CONNECTED at https://example.com/job"])
    assert goto(page, "https://example.com/job", timeout=30000) == "response"
    assert page.opened[-1][1] == {"timeout": 30000, "wait_until": "domcontentloaded"} and len(page.opened) == 2


def test_after_three_drops_the_message_says_retrying_is_safe():
    page = FlakyPage(["Page.goto: net::ERR_CONNECTION_RESET at https://example.com/job"] * 3)
    with pytest.raises(PlaywrightError, match="network problem.*trying again later is safe"):
        goto(page, "https://example.com/job")
    assert len(page.opened) == 3 and page.waits == [1000, 2000]


def test_other_errors_are_not_retried():
    page = FlakyPage(["Timeout 30000ms exceeded."])
    with pytest.raises(PlaywrightError, match="Timeout"):
        goto(page, "https://example.com/job")
    assert len(page.opened) == 1
