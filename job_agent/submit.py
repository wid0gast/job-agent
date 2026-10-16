"""SUBMIT: send exactly what you approved, in code, then save proof.

No model runs here. The live form is compared with the packet first; any
difference (a new required question, changed options) sends the packet back
to you instead of submitting. The job is marked `submitting` before the click
and is never retried automatically after that.
"""

from datetime import UTC, datetime
from pathlib import Path

from . import answers, companies, forms, packets, systems, web


def submit(job_id: int, ctx, ask, log=print) -> str:
    tracker, settings = ctx.tracker, ctx.settings
    job = tracker.get(job_id)
    packet = packets.load(job)

    def back_to_you(reason: str) -> str:
        tracker.move(job_id, "approved", "needs_you", reason=reason)
        log(f"  needs you: {reason}")
        return "needs_you"

    if job["status"] != "approved" or packet is None:
        raise ValueError(f"Job {job_id} is {job['status']}, not approved.")
    if packet.get("approved_hash") != packets.content_hash(packet):
        return back_to_you("The packet changed after approval. Review and approve it again.")
    problem = companies.cap_problem(tracker, packet["job"]["company_id"], settings, job_id)
    if problem:
        return back_to_you(problem)
    if tracker.submitted_today() >= settings.daily_submit_limit:
        return back_to_you(f"Daily limit of {settings.daily_submit_limit} reached; try tomorrow.")

    browser = ctx.browser()
    page = browser.page
    # Explored applications replay their approved recording (web.walk).
    system = web if packet.get("recording") else systems.adapter(packet["job"]["ats"])
    if hasattr(system, "walk"):
        return walk_submit(job_id, ctx, ask, log, system, packet, page, back_to_you)
    log("  opening the form")
    page.goto(packet["job"]["apply_url"], wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle", timeout=20000)
    page.wait_for_timeout(1000)

    fields = system.page_fields(page)
    differences = forms.unexpected(fields, with_known_answers(fields, packet))
    if differences:
        return back_to_you("The form changed: " + "; ".join(differences))
    try:
        log("  filling approved answers")
        system.fill_form(page, packet, packets.files(packet))
    except forms.FillError as error:
        return back_to_you(f"Couldn't fill “{error.label}”: {error.problem}.")

    tracker.move(job_id, "approved", "submitting")
    log("  submitting")
    try:
        result = system.submit(page, ask)
    except Exception as error:  # noqa: BLE001 - the click may have happened; you decide.
        result = {"status": "needs_you", "detail": f"Error after clicking submit: {error}"}
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
        drafted, _ = answers.draft(result["fields"], profile, job, saved)
        for answer in drafted:
            answer["note"] = answer.get("note") or f"New on “{answer['section']}”."
        have = {a["key"] for a in packet["answers"]}
        packet["answers"] += [a for a in drafted if a["key"] not in have]
        packet["approved_hash"] = None
        packets.store(packet)
        return back_to_you(result["detail"] + " Answer them (or have the model draft them), "
                           "then approve again; the Chrome window shows where it stopped.")
    packets.store(packet)
    return back_to_you(result["detail"])
