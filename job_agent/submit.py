"""SUBMIT: send exactly what you approved, in code, then save proof.

No model runs here. The live form is compared with the packet first; any
difference (a new required question, changed options) sends the packet back
to you instead of submitting. The job is marked `submitting` before the click
and is never retried automatically after that.
"""

from datetime import UTC, datetime
from pathlib import Path

from .pageview import goto
from . import answers, companies, forms, packets, systems, trace, web


def submit(job_id: int, ctx, ask, log=print) -> str:
    with trace.run("submit", job_id) as outcome:
        status = _submit(job_id, ctx, ask, log)
        if outcome is not None:
            job = ctx.tracker.get(job_id)
            outcome.update(status=status, detail=job["reason"] or "")
        return status


def _submit(job_id: int, ctx, ask, log) -> str:
    tracker, settings = ctx.tracker, ctx.settings
    job = tracker.get(job_id)
    packet = packets.load(job)

    def back_to_you(reason: str) -> str:
        tracker.move(job_id, "approved", "needs_you", reason=reason)
        log(f"  needs you: {reason}")
        return "needs_you"

    if job["status"] != "approved" or packet is None:
        raise ValueError(f"Job {job_id} is {job['status']}, not approved.")
    with trace.step("Checks before submitting") as step:
        same = packet.get("approved_hash") == packets.content_hash(packet)
        trace.check("Answers and documents are exactly what you approved", same,
                    "" if same else "Changed after approval.")
        if not same:
            return back_to_you("The packet changed after approval. Review and approve it again.")
        problem = companies.cap_problem(tracker, packet["job"]["company_id"], settings, job_id)
        trace.check("The company limit has room", not problem, problem or "")
        if problem:
            return back_to_you(problem)
        full = tracker.submitted_today() >= settings.daily_submit_limit
        trace.check(f"Under today's limit of {settings.daily_submit_limit}", not full,
                    f"{tracker.submitted_today()} submitted today.")
        if full:
            return back_to_you(f"Daily limit of {settings.daily_submit_limit} reached; try tomorrow.")
        step.summary = "Passed"

    browser = ctx.browser()
    page = browser.page
    # Explored applications replay their approved recording (web.walk).
    system = web if packet.get("recording") else systems.adapter(packet["job"]["ats"])
    if hasattr(system, "walk"):
        with trace.step("Filling the application page by page") as step:
            step.summary = walk_submit(job_id, ctx, ask, log, system, packet, page, back_to_you)
            return step.summary
    with trace.step("Opening the form and comparing it") as step:
        log("  opening the form")
        goto(page, packet["job"]["apply_url"])
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(1000)
        fields = system.page_fields(page)
        differences = forms.unexpected(fields, with_known_answers(fields, packet))
        trace.check("The live form is the one you approved answers for", not differences,
                    "; ".join(differences) if differences else f"{len(fields)} questions, as approved.")
        if differences:
            return back_to_you("The form changed: " + "; ".join(differences))
        step.summary = f"{len(fields)} questions, unchanged"
    with trace.step("Typing your approved answers (code, no model)") as step:
        try:
            log("  filling approved answers")
            system.fill_form(page, packet, packets.files(packet))
        except forms.FillError as error:
            trace.check("Every approved answer could be entered", False, f"{error.label}: {error.problem}")
            what = error.label if error.label.endswith(" fields") else f"“{error.label}”"
            return back_to_you(f"Couldn't fill {what}: {error.problem}. The rest was filled; nothing was sent.")
        step.summary = f"{sum(a.get('value') not in (None, '', []) for a in packet['answers'])} answers and " \
                       f"{len(packets.files(packet))} document(s)"

    with trace.step("Submitting") as step:
        tracker.move(job_id, "approved", "submitting")
        log("  submitting")
        try:
            result = system.submit(page, ask)
        except Exception as error:  # noqa: BLE001 - the click may have happened; you decide.
            result = {"status": "needs_you", "detail": f"Error after clicking submit: {error}"}
        step.summary = result.get("detail", result["status"])
        return finish(job_id, ctx, packet, page, result, log)


def finish(job_id: int, ctx, packet: dict, page, result: dict, log) -> str:
    """After the final click: proof, learned limits, and the job's outcome."""
    tracker = ctx.tracker
    folder = Path(packet["_dir"])
    shot = folder / "submitted.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
    except Exception:  # noqa: BLE001 - proof is best effort.
        shot = None
    trace.note(f"Proof saved: a screenshot of the page after submitting ({page.url}).")
    packet["submission"] = {"at": datetime.now(UTC).isoformat(timespec="seconds"),
                            "result": result, "screenshot": str(shot) if shot else None,
                            "url": page.url}
    packets.store(packet)
    if result.get("limit"):
        companies.learn_rule(packet["job"]["company_id"], result["detail"], page.url)
    if result["status"] == "submitted":
        tracker.move(job_id, "submitting", "submitted", reason=result["detail"])
        log("  submitted")
    else:
        tracker.move(job_id, "submitting", "needs_you", reason=result["detail"])
        log(f"  needs you: {result['detail']}")
    return result["status"]


def with_known_answers(fields: list[dict], packet: dict) -> dict:
    """Fill new questions you've already answered elsewhere (same wording and options)."""
    bank = answers.load_bank()
    have = {a["key"] for a in packet["answers"]}
    for field in fields:
        if field["key"] in have:
            continue
        known = bank.get(answers.fingerprint(field))
        if known:
            packet["answers"].append({**field, "fingerprint": answers.fingerprint(field),
                                      "value": known["value"], "source": "approved earlier",
                                      "note": "Added at submit time."})
    return packet


def walk_submit(job_id, ctx, ask, log, system, packet, page, back_to_you) -> str:
    """Multi-page systems (Workday): fill page by page; new questions come back to you."""
    from . import config

    tracker = ctx.tracker
    profile, saved = config.load_profile(), config.load_answers()
    job = tracker.get(job_id)

    def known(field):
        """Your profile, saved or earlier-approved answers, recorded in the packet."""
        drafted, _ = answers.draft([field], profile, job, saved)
        answer = drafted[0]
        if answer["value"] in (None, "", []):
            return None
        answer["note"] = "Filled while applying, from your " + answer["source"] + "."
        packet["answers"].append(answer)
        return answer["value"]

    def before_final():
        tracker.move(job_id, "approved", "submitting")
        log("  submitting")

    email = profile.get("contact", {}).get("email")
    if not email:
        return back_to_you("Your profile has no email to sign in with.")
    try:
        result = system.walk(page, packet, packets.files(packet), ask, email=email, tracker=tracker,
                             before_final=before_final, known=known, log=log)
    except forms.FillError as error:
        result = {"status": "needs_you", "detail": f"{error.label}: {error.problem}"}
    except Exception as error:  # noqa: BLE001 - before or after the click; the status says which.
        result = {"status": "needs_you", "detail": f"{type(error).__name__}: {error}"[:500]}
    if tracker.get(job_id)["status"] == "submitting":
        return finish(job_id, ctx, packet, page, result, log)
    if result["status"] == "new_questions":
        by_key = {a["key"]: a for a in packet["answers"]}
        fresh = []
        for field in result["fields"]:
            answer = by_key.get(field["key"])
            if answer is not None and "rejected" in field:
                # An answer you have, which this site won't take (e.g. not one of its choices):
                # clear it and show why, with the site's real choices to pick from.
                answer.update(value=None, note=field["note"], source="blank", section=field["section"],
                              **({"options": field["options"], "kind": "select"} if field.get("options") else {}))
            elif answer is None:
                fresh.append(field)
        drafted, _ = answers.draft(fresh, profile, job, saved)
        for answer in drafted:
            answer["note"] = answer.get("note") or f"New on “{answer['section']}”."
        packet["answers"] += drafted
        packet["approved_hash"] = None
        packets.store(packet)
        return back_to_you(result["detail"] + " Answer them (or have the model draft them), "
                           "then approve again; the Chrome window shows where it stopped.")
    packets.store(packet)
    return back_to_you(result["detail"])
