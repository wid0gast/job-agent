"""Application systems the agent can prepare and submit for, by tracker `ats` name.

Each module offers the same functions: resolve, fetch, apply_url, read_form,
page_fields, fill_form, submit (see forms.py for the field format). Multi-page
systems (Workday) offer walk() in place of fill_form and submit.
"""

from . import ashby, greenhouse, lever, workday
from .forms import Closed, FillError  # noqa: F401

ADAPTERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workday": workday}
SUPPORTED = tuple(ADAPTERS)


def adapter(ats: str):
    return ADAPTERS.get(ats)
