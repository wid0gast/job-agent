"""Stopping a running task when you ask (the Stop button, or the assistant).

The task is marked "stopping"; the work notices at its next step boundary (each pipeline
step, each agent turn) and ends there, so nothing is left half-written. A model request
already under way finishes first. Submitting can't be stopped: once the click may have
happened, stopping halfway is how double applications happen.
"""

import contextvars

# The long tasks (minutes, many steps) that only read pages and call the model, so stopping
# them between steps is safe. Short ones (a single request) simply finish.
STOPPABLE = ("prepare", "people")


class Stopped(BaseException):
    """Raised at a step boundary after you asked to stop. A BaseException, like
    KeyboardInterrupt, so the handlers that turn errors into results don't swallow it."""


_asked: contextvars.ContextVar = contextvars.ContextVar("stop_asked", default=None)


def watch(asked):
    """Check `asked()` at each step boundary of the work run in this thread from now on."""
    return _asked.set(asked)


def check() -> None:
    asked = _asked.get()
    if asked is not None and asked():
        raise Stopped("Stopped by you.")
