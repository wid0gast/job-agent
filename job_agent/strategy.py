"""HOW TO APPLY: the route that gives each job its best odds, decided in code from signals you can check.

Most tools only make applying faster. What changes the odds is how you apply:
- through a person: in Lever's 2016 study of about 4 million candidates, 1 in 16 referred candidates
  was hired, against 1 in 152 who applied through a careers page or posting;
- to a posting that's really hiring: about 1 in 5 postings show no hiring activity, and roles
  open 45+ days or posted again are the clearest sign (JobsPipe's 2026 ghost-job index);
- without competing with yourself: a second application at a company where you've applied for
  the same role mostly reaches the same people.

So each job you haven't applied to gets one route, with the reasons it rests on:
  reach_out    someone found at the company hasn't heard from you yet, replied, or is due a few days
  apply        nothing in the way
  think_twice  the posting may not be hiring, you've applied here for this role, or the company limit is full
No model call: every reason names the data it came from (posting dates, your applications,
the people found and what you've sent them).
"""

from datetime import UTC, datetime, timedelta

from . import companies
from .filters import posted_date
from .tracker import normalize

STALE_DAYS = 45       # Open this long, a posting is more often not hiring...
INTERN_STALE_DAYS = 90  # ...but internship postings normally stay open for the whole recruiting season.
REPOST_GAP_DAYS = 21  # The same role listed again this much later is a repost, not another office.
FRESH_DAYS = 7
WARM = ("background", "peer")  # People worth writing to before you apply (people.KINDS).
ROUTES = {"reach_out": "Reach out first", "apply": "Apply", "think_twice": "Think twice"}
OPEN = ("found", "preparing", "prepared", "approved", "needs_you")  # Not yet applied.


def stale_days(job: dict) -> int:
    return INTERN_STALE_DAYS if job.get("job_type") == "internship" else STALE_DAYS


def same_place(a: dict, b: dict) -> bool:
    places = [normalize(x.get("location")) for x in (a, b)]
    return not all(places) or places[0] == places[1]


def posting_signals(job: dict, listings: list[dict], now: datetime) -> tuple[list[str], list[str]]:
    """(warnings, good signs) from when this role was listed: its own date and its other listings'."""
    bad, good = [], []
    mine = posted_date(job.get("posted_at"))
    # Only this place's listings count: another office opening later is a new opening.
    here = [d for o in listings if same_place(o, job) and (d := posted_date(o.get("posted_at")))]
    first = min(here, default=None)
    earliest = min([d for d in (mine, first) if d], default=None)
    if mine and first and (mine - first).days >= REPOST_GAP_DAYS:
        bad.append(f"Posted again: this role was first listed {first:%b %-d}, {(now - first).days} days ago. "
                   "Reposted roles are more often not really hiring.")
    elif earliest and (now - earliest).days >= (limit := stale_days(job)):
        bad.append(f"Open {(now - earliest).days} days (posted {earliest:%b %-d}). "
                   f"{'Internship postings' if limit == INTERN_STALE_DAYS else 'Postings'} open {limit}+ days "
                   "are more often not really hiring.")
    elif mine and (now - mine).days <= FRESH_DAYS:
        age = (now.astimezone().date() - mine.astimezone().date()).days  # Calendar days where you are.
        good.append(f"Posted {'today' if age == 0 else 'yesterday' if age == 1 else f'{age} days ago'}: "
                    "early applications are read first.")
    return bad, good


def person_signal(packet: dict | None, now: datetime) -> dict | None:
    """Where you stand with the people found for this job: {"state", "name", "headline", "reason"},
    or None when there's no one to write to."""
    from .people import SECOND_PERSON_DAYS

    listed = [p for p in ((packet or {}).get("people") or {}).get("people", []) if not p.get("removed")]
    for p in listed:
        if p.get("replied_at"):
            first = p["name"].split()[0]
            return {"state": "replied", "name": first,
                    "headline": f"{first} replied: ask whether they'd refer you or pass your name on, then apply.",
                    "reason": f"{p['name']} ({p['title']}) replied on {p['replied_at'][:10]}."}
    for p in listed:
        sent = posted_date(p.get("sent_at"))
        if sent and now - sent < timedelta(days=SECOND_PERSON_DAYS):
            first, until = p["name"].split()[0], sent + timedelta(days=SECOND_PERSON_DAYS)
            return {"state": "waiting", "name": first,
                    "headline": f"You wrote to {first} on {sent:%b %-d}: give it until {until:%b %-d}, then apply.",
                    "reason": f"Advisers give a first message {SECOND_PERSON_DAYS} days before trying someone else."}
    if any(p.get("sent_at") for p in listed):
        return None  # Written to, no reply in time: apply (follow-ups are on Today).
    warm = [p for p in listed if p["kind"] in WARM]
    if warm:
        first = warm[0]["name"].split()[0]
        why = "share your background" if warm[0]["kind"] == "background" else "work in this role or team"
        return {"state": "fresh", "name": first,
                "headline": f"Write to {warm[0]['name']} before you apply: they {why}, and a referral counts most "
                            "before you apply. The draft is ready under People to contact.",
                "reason": (f"{len(warm)} people found worth writing to first" if len(warm) > 1 else
                           f"{warm[0]['name']} ({warm[0]['title']}) found worth writing to first") + "; not contacted yet."}
    return None


def advise(job: dict, *, listings: list[dict], applied: dict | None = None, cap: str | None = None,
           packet: dict | None = None, now: datetime | None = None) -> dict | None:
    """The route for one job: {"route", "label", "headline", "reasons": [{"text", "good"}]}, or None
    once you've applied. `listings`: this role's other rows (tracker.listings); `applied`: an
    application you sent there for what looks like the same role (tracker.likely_applied);
    `cap`: why the company limit is full (companies.cap_problem)."""
    if job.get("status") not in OPEN:
        return None
    now = now or datetime.now(UTC)
    stale, fresh = posting_signals(job, listings, now)
    person = person_signal(packet, now)
    blocked = []
    if applied:
        blocked.append(f"You applied for {applied['title']} here on {(applied['submitted_at'] or '')[:10]}: "
                       "a second application mostly reaches the same people.")
    if cap:
        blocked.append(cap)
    reasons = [{"text": t, "good": False} for t in blocked + stale]
    reasons += [{"text": t, "good": True} for t in fresh + ([person["reason"]] if person else [])]
    if job.get("fit_score") is not None:
        reasons.append({"text": f"Fit {job['fit_score']} from reading the whole posting.", "good": job["fit_score"] >= 70})
    if blocked:
        route, headline = "think_twice", blocked[0]
    elif stale and person and person["state"] != "waiting":
        route = "reach_out"  # Someone there can tell you whether it's really open.
        headline = f"Ask {person['name']} whether this role is still open before you spend time on it."
    elif stale:
        route, headline = "think_twice", stale[0]
    elif person:
        route, headline = "reach_out", person["headline"]
    else:
        route = "apply"
        headline = "Apply: " + ("the posting is new and nothing is in the way." if fresh else "nothing is in the way.")
    return {"route": route, "label": ROUTES[route], "headline": headline, "reasons": reasons}


class Advisor:
    """Routes for many jobs at once (a page or a list): what they share is read once."""

    def __init__(self, tracker, settings):
        self.tracker, self.settings = tracker, settings
        self.index = tracker.applied_index()
        self.rules = companies.load_rules()
        self.now = datetime.now(UTC)

    def advise(self, job: dict, packet: dict | None = None, listings: list[dict] | None = None) -> dict | None:
        if job.get("status") not in OPEN:
            return None
        cap = (companies.cap_problem(self.tracker, job["company_id"], self.settings, job["id"], self.rules)
               if job.get("company_id") else None)
        return advise(job, listings=self.tracker.listings(job) if listings is None else listings,
                      applied=self.tracker.likely_applied(job, self.index), cap=cap, packet=packet, now=self.now)
