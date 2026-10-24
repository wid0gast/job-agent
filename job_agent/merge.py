"""Merging resume versions into one master resume (docs/merge-guide.md, docs/merge-eval.md).

Code does the structure: contact details, which role is which, where each section goes,
skills item by item, exact repeats, and which wording of a repeat to keep. The model only
judges what code can't see, two wordings of one accomplishment, by grouping line ids; it
never writes a word, so nothing can be invented. Without a model, code still catches the
repeats that share their numbers and keeps everything else (a repeat beats a loss).
"""

import copy
import re

DATE = (r"(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+)?(?:19|20)\d{2}")
DATES = re.compile(rf"{DATE}(?:\s*(?:–|—|-|to)\s*(?:present|current|now|{DATE}))?", re.I)
# Numbers that identify a result: 84%, $3M, 1.2M, 2M+, 6,000, 397. Years and small counts don't.
DISTINCT = re.compile(r"\$?\d[\d,.]*\d(?:%|[kmb]\+?|\+)?|\$?\d(?:%|[kmb]\+?|\+)", re.I)
KINDS = [("summary", r"summary|profile|objective|focus|about"), ("education", r"education|degree"),
         ("skills", r"skill|technolog|tools"), ("publications", r"publication|papers"),
         ("experience", r"experience|employment|work history")]
MERGE_PROMPT = """
You compare lines from several versions of one person's resume. Lines come in units (one
role, or one section). In each unit, find lines that state the same accomplishment, fact or
item in different words: one is a rewrite, a shorter or a longer version of the other.
Lines about different work stay apart even when they share many words (teaching algebra
and teaching geometry; two different systems built in the same job). Reply with groups of
ids that say the same thing, each group from a single unit; leave out lines with no match.
""".strip()
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["groups"],
          "properties": {"groups": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}}}


def words(text: str) -> set[str]:
    from .resumes import words as resume_words

    return resume_words(text)


def kind_of(name: str) -> str:
    return next((kind for kind, pattern in KINDS if re.search(pattern, name, re.I)), norm(name))


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def numbers(text: str) -> set[str]:
    found = set()
    for match in DISTINCT.findall(text or ""):
        key = match.lower().replace(",", "").rstrip(".")
        digits = re.sub(r"\D", "", key)
        if re.fullmatch(r"(19|20)\d\d", key) or (key.isdigit() and len(digits) < 3):
            continue  # A year, or a small count ("5 sections").
        found.add(key)
    return found


def info(text: str) -> int:
    """How much a wording says: its numbers count most, then its words."""
    return 3 * len(numbers(text)) + len(words(text))


def start_of(text: str) -> str:
    match = DATES.search(text or "")
    return norm(match.group(0).split("–")[0].split("—")[0].split(" to ")[0]) if match else ""


def role_words(entry: dict, under: bool = False) -> set[str]:
    """A role's or school's words, without dates; with `under`, the line under the heading too.
    Versions split them differently ("UW — Seattle, WA / B.Des., 2022" or "UW — B.Des. / 2022")."""
    return words(DATES.sub(" ", " ".join([entry["heading"] or "", *(entry["meta"][:1] if under else [])])))


def likeness(wa: set[str], wb: set[str]) -> float:
    if not wa or not wb or len(wa & wb) / min(len(wa), len(wb)) < 0.8:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def role_score(a: dict, b: dict) -> float:
    """How surely two role headings are one role: 0 when they aren't. A promotion ("Software
    Engineer II") at the same company is a different role from "Software Engineer"."""
    score = max(likeness(role_words(a), role_words(b)), likeness(role_words(a, True), role_words(b, True)))
    if not score:
        return 0.0
    sa, sb = start_of(" ".join([a["heading"] or "", *a["meta"]])), start_of(" ".join([b["heading"] or "", *b["meta"]]))
    if sa and sb:
        score += 0.5 if sa == sb else -0.5
    return score


def items_of(entry: dict, kind: str) -> list[str]:
    """The lines merged as facts. A degree line under its school is one (the fuller wins);
    a role's dates and place are not (the newest version's win)."""
    return (entry["meta"] if kind == "education" else []) + entry["items"]


def merge(versions: list[tuple[str, str]], llm=None, log=print) -> dict:
    """(label, markdown) versions, oldest first; the last is the base (the caller orders ties
    so the fuller cut is last). Returns markdown, changes, kept (from older versions only) and
    combined (repeats, with the wording kept and the ones left out)."""
    from .resumes import render, structure

    docs = [structure(md) for _, md in versions]
    linky = re.compile(r"(?i)\W*(links?|contact( info(rmation)?| details)?|profiles?)\W*")
    for doc in docs:  # A "Links" or "Contact" section or entry is the contact line, written lower down.
        for section in list(doc["sections"]):
            whole = bool(linky.fullmatch(section["name"]))
            for entry in [e for e in section["entries"] if whole or (e["heading"] and linky.fullmatch(e["heading"]))]:
                parts = [p for l in [entry["heading"] or "", *entry["meta"], *entry["items"]]
                         for p in re.split(r"\s+[·|•]\s+|\s+", strip_bullet(l)) if p and not linky.fullmatch(p)]
                doc["header"].append(" · ".join(parts))
                section["entries"].remove(entry)
            if not section["entries"]:
                doc["sections"].remove(section)
    labels = [label for label, _ in versions]
    base = copy.deepcopy(docs[-1])
    rank = len(versions) - 1  # Newest = highest.
    changes = [f"Base: {labels[-1]} (newest, fullest)."]
    kept: list[tuple[str, str]] = []
    pools: dict[int, list[dict]] = {}  # id(entry) -> candidate lines
    owners: dict[int, tuple[dict, dict]] = {}  # id(entry) -> (section, entry)

    def pool(section: dict, entry: dict) -> list[dict]:
        if id(entry) not in pools:
            kind = kind_of(section["name"])
            pools[id(entry)] = [{"text": t, "rank": rank, "order": i, "label": labels[-1]}
                                for i, t in enumerate(items_of(entry, kind))]
            owners[id(entry)] = (section, entry)
        return pools[id(entry)]

    for section in base["sections"]:
        for entry in section["entries"]:
            pool(section, entry)

    early: list[dict] = []  # Older lines already said in their section, before any pooling.
    from .resumes import render as _render

    header = merge_header([d["header"] for d in docs], changes, _render(docs[-1]))
    for index in range(len(docs) - 2, -1, -1):  # Next-newest first: newer wins over older.
        doc, label = docs[index], labels[index]
        for old_section in doc["sections"]:
            kind = kind_of(old_section["name"])
            placed: dict[int, int] = {}  # base section id -> entries of this section matched there
            pending = []
            for entry in old_section["entries"]:
                if entry["heading"]:
                    target = best_role(base, entry, kind)
                    if target:
                        section, match = target
                        placed[id(section)] = placed.get(id(section), 0) + 1
                        lines = pool(section, match)
                        lines += [{"text": t, "rank": index, "order": 1000 + i, "label": label}
                                  for i, t in enumerate(items_of(entry, kind_of(section["name"])))]
                        continue
                pending.append(entry)
            for entry in pending:
                section = find_section(base, old_section, placed)
                if section is None and not entry["heading"]:
                    # A section no version shares ("Awards & Leadership" beside "Awards & Honors"
                    # and "Activities & Leadership"): each line joins the section it best fits,
                    # where repeats are found; lines that fit nowhere keep a section of their own.
                    rest = []
                    for text in entry["items"]:
                        home = best_fit(base, text)
                        if home is None:
                            rest.append(text)
                            continue
                        loose = next((e for e in home["entries"] if not e["heading"]), None)
                        if loose is None:
                            loose = {"heading": None, "meta": [], "items": []}
                            home["entries"].append(loose)
                        if not said_in(home, text, early):
                            pool(home, loose).append({"text": text, "rank": index,
                                                      "order": 1000 + len(pool(home, loose)), "label": label})
                    if not rest:
                        continue
                    entry = {**entry, "items": rest}
                if section is None:
                    section = {"name": old_section["name"], "entries": []}
                    base["sections"].append(section)
                    changes.append(f"Added section “{old_section['name']}” from {label}.")
                said: list = []  # Already there in another shape ("School, City" as a plain line).
                if entry["heading"] and section["entries"] and all(
                        said_in(section, t, said) for t in [entry["heading"], *entry["meta"], *entry["items"]]):
                    early += said
                    continue
                if entry["heading"]:
                    new = copy.deepcopy(entry)
                    insert_entry(section, new)
                    changes.append(f"Added “{entry['heading']}” from {label}.")
                    kept.append((label, entry["heading"]))
                    pools[id(new)] = [{"text": t, "rank": index, "order": i, "label": label, "new": True}
                                      for i, t in enumerate(items_of(new, kind_of(section["name"])))]
                    owners[id(new)] = (section, new)
                else:
                    loose = next((e for e in section["entries"] if not e["heading"]), None)
                    if loose is None:
                        loose = {"heading": None, "meta": [], "items": []}
                        section["entries"].append(loose)
                    lines = pool(section, loose)
                    lines += [{"text": t, "rank": index, "order": 1000 + i, "label": label}
                              for i, t in enumerate(entry["items"]) if not said_in(section, t, early)]

    combined, possible = dedupe(pools, owners, llm, log)
    combined = early + combined
    for key, lines in pools.items():
        section, entry = owners[key]
        kind = kind_of(section["name"])
        texts = [line["text"] for line in sorted(lines, key=lambda l: (l["order"] >= 1000, l["order"]))
                 if not line.get("dropped")]
        kept += [(line["label"], line["text"]) for line in lines
                 if line["rank"] < rank and not line.get("dropped") and not line.get("new")]
        if kind == "education":
            entry["meta"], entry["items"] = [t for t in texts if not BULLETED(t)], [t for t in texts if BULLETED(t)]
        else:
            entry["items"] = texts
    for section in base["sections"]:
        if kind_of(section["name"]) == "skills":
            merge_skills(section, [s for d in docs[:-1] for s in d["sections"] if kind_of(s["name"]) == "skills"])
        section["entries"] = [e for e in section["entries"] if e["heading"] or e["items"] or e["meta"]]
    base["sections"] = [s for s in base["sections"] if s["entries"]]
    base["sections"].sort(key=lambda s: kind_of(s["name"]) != "summary")  # Summaries first; stable.
    base["header"] = header
    if len(changes) == 1 and len(versions) > 1 and not kept:
        changes.append("Everything in the older versions is already in the newest one.")
    return {"markdown": render(base), "changes": changes,
            "kept": [{"from": label, "line": line} for label, line in kept], "combined": combined,
            "possible": possible}


def BULLETED(text: str) -> bool:  # noqa: N802 - reads like a test.
    from .resumes import BULLET

    return bool(BULLET.match(text))


def best_role(base: dict, entry: dict, kind: str | None = None):
    """The base role or school this entry is. From a section of another kind, only a close match:
    "BITS Pilani" (an activities heading) is inside "Teaching Assistant | BITS Pilani" but isn't it."""
    def fits(section: dict, other: dict) -> bool:
        if kind is None or kind_of(section["name"]) == kind:
            return True
        wa, wb = role_words(entry), role_words(other)
        return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.6

    scored = [(role_score(entry, e), s, e) for s in base["sections"] for e in s["entries"] if e["heading"] and fits(s, e)]
    scored = [x for x in scored if x[0] > 0]
    if not scored:
        return None
    _, section, match = max(scored, key=lambda x: x[0])
    return section, match


def said_in(section: dict, text: str, record: list) -> bool:
    """Whether a section's roles and lines already say this older line (a degree on its own
    line, already under its school heading)."""
    lines = [l for e in section["entries"] for l in [e["heading"] or "", *e["meta"], *e["items"]] if l]
    for line in lines:
        if same_text(text, line) or (inside(text, line) and not adds(text, line)):
            record.append({"kept": [line], "left_out": [text]})
            return True
    joined = " ".join(lines)
    # All its words (and digits) among the section's lines: the same things, said as one line or several.
    covered = stems(text) <= stems(joined) and set(re.findall(r"\d+", text)) <= set(re.findall(r"\d+", joined))
    if lines and covered and not adds(text, joined) and len(words(strip_bullet(text))) >= 3:
        record.append({"kept": [l for l in lines if words(l) & words(text)][:3], "left_out": [text]})
        return True
    return False


def best_fit(base: dict, text: str):
    """The section whose lines already hold most of this line's words (half at least)."""
    mine = words(strip_bullet(text))
    if len(mine) < 3:
        return None
    scored = []
    for section in base["sections"]:
        if kind_of(section["name"]) in ("summary", "skills"):
            continue
        theirs = words(" ".join(l for e in section["entries"] for l in [e["heading"] or "", *e["meta"], *e["items"]]))
        scored.append((len(mine & theirs) / len(mine), section))
    share, section = max(scored, key=lambda x: x[0], default=(0, None))
    return section if share >= 0.5 else None


def find_section(base: dict, old: dict, placed: dict):
    """Where an older section's unmatched entries go: the section its other roles were found
    in, else one with the same name or kind; None if no section is like it."""
    if placed:
        top = max(placed, key=placed.get)
        return next(s for s in base["sections"] if id(s) == top)
    same = [s for s in base["sections"] if norm(s["name"]) == norm(old["name"])]
    kind = kind_of(old["name"])
    # Summaries are framings, not facts: a "Research Focus" and a "Professional Summary"
    # are both kept, each as written.
    alike = same or ([] if kind == "summary" else [s for s in base["sections"] if kind_of(s["name"]) == kind])
    return alike[0] if alike else None


def insert_entry(section: dict, entry: dict) -> None:
    """In date order when both sides have dates (newest first), else at the end."""
    year = lambda e: max((int(y) for y in re.findall(r"(?:19|20)\d{2}", " ".join(  # noqa: E731
        [e["heading"] or "", *e["meta"]]))), default=0)
    mine = year(entry)
    for i, other in enumerate(section["entries"]):
        if mine and other["heading"] and year(other) and year(other) < mine:
            section["entries"].insert(i, entry)
            return
    section["entries"].append(entry)


def merge_header(headers: list[list[str]], changes: list, said: str = "") -> list[str]:
    """Contact details from every version; the newest wins for one of a kind (a phone, an
    email, a LinkedIn); other lines (work authorization) come from the newest that has one."""
    newest_first = list(reversed(headers))
    items, seen_kinds, lines, labeled = [], set(), [], {}
    for n, header in enumerate(newest_first):
        for line in header:
            if re.search(r"@|https?://|linkedin|github|\+?\(?\d{3}\)?[\s.-]\d{3}", line):
                for part in re.split(r"\s+[·|•]\s+|\s{2,}", line.strip()):
                    part = part.strip(" ·|")
                    if not part:
                        continue
                    if re.match(r"[A-Za-z][A-Za-z -]{2,30}:\s", part):  # "Work Authorization: …" is a line of its own.
                        labeled.setdefault(n, []).append(part)
                        continue
                    kind = contact_kind(part)
                    if kind in seen_kinds and kind != "other":
                        continue
                    if kind == "other" and n and norm(part) and norm(part) in norm(said):
                        continue  # An older version's "New York, USA": the newest already says it (under Education).
                    if kind == "other" and any(norm(part) == norm(p) for p in items):
                        continue
                    seen_kinds.add(kind)
                    items.append(part)
    for n, header in enumerate(newest_first):
        for line in labeled.get(n, []) + header:
            if not re.search(r"@|https?://|linkedin|github|\+?\(?\d{3}\)?[\s.-]\d{3}", line) and \
                    not any(norm(line) == norm(l) for l in lines) and kind_of_line(line) not in \
                    {kind_of_line(l) for l in lines}:
                lines.append(line)
    # A paper's link isn't a contact detail; the resume lists the paper itself.
    papers = [i for i in items if re.search(r"\.pdf\b|doi\.org|/doi/|/abs/|/article/|arxiv\.org|aclanthology", i.lower())]
    if papers:
        items = [i for i in items if i not in papers]
        changes.append(f"Left {len(papers)} paper link(s) out of the contact line: {', '.join(papers)}.")
    # A link's label left beside its address ("Scholar" next to scholar.google.com/…).
    links = " ".join(i.lower() for i in items if contact_kind(i) != "other")
    flat = re.sub(r"[^a-z0-9]", "", links)  # "Scholar", or a handle: "marcuslee", "a mlee" (an icon read as "a").
    items = [i for i in items if contact_kind(i) != "other" or len(i.split()) > 2 or not flat or
             not (norm(i).replace(" ", "") in flat or (len(norm(i).split()[-1]) >= 3 and norm(i).split()[-1] in flat))]
    # One order whatever the version wrote, so a fresh reading of the same PDF changes nothing.
    place = {"email": 0, "phone": 1, "other": 2, "linkedin": 3, "github": 4, "scholar.google": 5}
    items.sort(key=lambda i: place.get(contact_kind(i), 6))
    return ([" · ".join(items)] if items else []) + lines


def contact_kind(part: str) -> str:
    lower = part.lower()
    if "@" in lower and "." in lower and " " not in lower:
        return "email"
    if re.fullmatch(r"\+?[\d\s().-]{9,}", part):
        return "phone"
    for site in ("linkedin", "github", "scholar.google"):
        if site in lower:
            return site
    if re.match(r"(https?://)?[\w-]+(\.[\w-]+)+(/\S*)?$", lower):
        return "site:" + re.sub(r"^https?://(www\.)?", "", lower).split("/")[0]
    return "other"


def kind_of_line(line: str) -> str:
    match = re.match(r"\s*([A-Za-z ]{3,30}):", line)
    return norm(match.group(1)) if match else norm(line)


def dedupe(pools: dict, owners: dict, llm, log) -> tuple[list[dict], list[dict]]:
    """Mark repeats in each pool as dropped, keeping the wording that says most. Returns what
    was combined, and pairs judged alike whose wordings each say something (all kept).

    Each group judged alike is settled on its own: chaining them (A is like B, B is like C)
    once compared a line with the wrong partner and kept a repeat."""
    combined, possible, units = [], [], []
    for key, lines in pools.items():
        section, _ = owners[key]
        if kind_of(section["name"]) == "skills":
            continue  # Merged item by item instead.
        for i, line in enumerate(lines):
            line["id"] = f"u{len(units)}.{i}"
        for i, a in enumerate(lines):  # The same line with a number updated ("F1 by 9%" → "by 12%"): the newest wins.
            for b in lines[:i]:
                if a["rank"] != b["rank"] and not a.get("dropped") and not b.get("dropped") and updated(a["text"], b["text"]):
                    old_line, new_line = sorted((a, b), key=lambda l: l["rank"])
                    old_line["dropped"] = True
                    combined.append({"kept": [new_line["text"]], "left_out": [old_line["text"]]})
        groups = []  # Code's own certain pairs first: the same line, or the same result by its numbers.
        for i in range(len(lines)):
            for j in range(i):
                a, b = lines[i]["text"], lines[j]["text"]
                if same_text(a, b) or len(numbers(a) & numbers(b)) >= 2 or (numbers(a) & numbers(b) and half_alike(a, b)) \
                        or (lines[i]["rank"] != lines[j]["rank"] and (run(a, b) >= 6 or (numbers(a) & numbers(b) and
                                                                         len(content(a) & content(b)) >= 4))):
                    groups.append([j, i])
        units.append({"lines": lines, "groups": groups})
    if llm is not None:
        ask = [{"unit": u, "lines": [{"id": l["id"], "text": strip_bullet(l["text"])} for l in unit["lines"]
                                     if not l.get("dropped")]}
               for u, unit in enumerate(units) if len({l["rank"] for l in unit["lines"]}) > 1]
        ask = [a for a in ask if len(a["lines"]) > 1]
        if ask:
            import json

            try:
                reply = llm.json(MERGE_PROMPT, json.dumps({"units": ask}, ensure_ascii=False), SCHEMA, "resume_merge")
                for group in reply.get("groups") or []:
                    ids = [g for g in group if isinstance(g, str) and re.fullmatch(r"u\d+\.\d+", g)]
                    if len(ids) < 2 or len({g.split(".")[0] for g in ids}) != 1:
                        continue  # Across units, or unknown ids: not trusted.
                    unit = units[int(ids[0].split(".")[0][1:])]
                    index = [int(g.split(".")[1]) for g in ids if int(g.split(".")[1]) < len(unit["lines"])]
                    unit["groups"].append(index)
            except Exception as error:  # noqa: BLE001 - a merge without the model still merges.
                log(f"  repeats judged in code only: {error}")
    for unit in units:
        lines = unit["lines"]
        for group in unit["groups"]:
            members = [lines[i] for i in dict.fromkeys(group) if not lines[i].get("dropped")]
            if len(members) > 1:
                settle(members, combined, possible)
        newest = max((l["rank"] for l in lines), default=0)
        partners: dict[int, set[int]] = {}
        for group in unit["groups"]:
            for i in group:
                partners.setdefault(i, set()).update(j for j in group if j != i)
        for i, line in enumerate(lines):
            # An older line judged alike with several kept lines that together say it, but for a
            # verb ("Applied" for "Used"): one 1-page bullet that condensed two 2-page bullets.
            if line.get("dropped") or line["rank"] == newest:
                continue
            kept = [lines[j] for j in partners.get(i, ()) if not lines[j].get("dropped")]
            joined = " ".join(k["text"] for k in kept)
            missing = stems(line["text"]) - stems(joined)
            if len(kept) > 1 and len(missing) <= max(1, len(stems(line["text"])) // 10) and not adds(line["text"], joined):
                line["dropped"] = True
                combined.append({"kept": [k["text"] for k in kept], "left_out": [line["text"]]})
        for line in lines:  # An older line that only sums up others here ("Reviewer for two conferences").
            if line.get("dropped") or line["rank"] == newest:
                continue
            others = [o for o in lines if o is not line and not o.get("dropped")]
            joined = " ".join(o["text"] for o in others)
            missing = stems(line["text"]) - stems(joined)  # But for a verb: "Applied" for "Used".
            if others and len(stems(line["text"])) >= 2 and len(missing) <= max(1, len(stems(line["text"])) // 10) \
                    and set(re.findall(r"\d+", line["text"])) <= set(re.findall(r"\d+", joined)) and not adds(line["text"], joined):
                line["dropped"] = True
                combined.append({"kept": [o["text"] for o in others if words(strip_bullet(o["text"])) &
                                          words(strip_bullet(line["text"]))], "left_out": [line["text"]]})
        for i, line in enumerate(lines):  # A line wholly inside another (its words and numbers) says nothing more.
            if line.get("dropped"):
                continue
            host = next((o for j, o in enumerate(lines) if j != i and not o.get("dropped") and inside(line["text"], o["text"])), None)
            if host is not None:
                line["dropped"] = True
                combined.append({"kept": [host["text"]], "left_out": [line["text"]]})
    kept_texts = {l["text"] for u in units for l in u["lines"] if not l.get("dropped")}
    unique = {}
    for group in possible:  # Only pairs that both stayed, once each.
        texts = list(dict.fromkeys(t for t in group["lines"] if t in kept_texts))
        if len(texts) > 1:
            unique.setdefault(frozenset(texts), {"lines": texts})
    return combined, list(unique.values())


def settle(members: list[dict], combined: list, possible: list) -> None:
    """One group judged alike: fullest first; a wording stays unless what's kept already says
    all of it. One that looks like a repeat but adds a fact is kept and shown to you; one that
    doesn't look like a repeat (different work, judged alike) just stays."""
    order = sorted(members, key=lambda l: (-info(l["text"]), -l["rank"], l["order"]))
    keepers, dropped = [order[0]], []
    for line in order[1:]:
        kept_text = " ".join(k["text"] for k in keepers)
        alike = any(says(k["text"], line["text"]) for k in keepers) or inside(line["text"], kept_text)
        if alike and not adds(line["text"], kept_text):
            line["dropped"] = True
            dropped.append(line["text"])
            continue
        if alike:
            possible.append({"lines": [k["text"] for k in keepers if says(k["text"], line["text"])] + [line["text"]]})
        keepers.append(line)
    best = keepers[0]
    ranks = [m["rank"] for m in members]
    if best["order"] >= 1000 and best["rank"] < max(ranks):
        best["order"] = min(m["order"] for m in members)  # The fuller, older wording takes the newer one's place.
    if dropped:
        combined.append({"kept": [k["text"] for k in keepers], "left_out": dropped})


DEGREES = [(r"\bM\.?S\.?(?=\s|,|$)|\bM\.Sc\.?", "Master of Science"), (r"\bB\.?S\.?(?=\s|,|$)|\bB\.Sc\.?", "Bachelor of Science"),
           (r"\bB\.?A\.?(?=\s|,|$)", "Bachelor of Arts"), (r"\bM\.?A\.?(?=\s|,|$)", "Master of Arts"),
           (r"\bB\.?E\.?(?=\s|,|$)", "Bachelor of Engineering"), (r"\bB\.?Tech\.?", "Bachelor of Technology"),
           (r"\bM\.?Eng\.?", "Master of Engineering"), (r"\bPh\.?D\.?", "Doctor of Philosophy"),
           (r"\bMBA\b", "Master of Business Administration")]


def stems(text: str) -> set[str]:
    """Words with their endings trimmed, so "showing", "showed" and "shows" are one word; a
    degree's abbreviation as its name ("M.S." is "Master of Science")."""
    out = set()
    for pattern, name in DEGREES:
        text = re.sub(pattern, name, text)
    for word in words(strip_bullet(text)):
        for end in ("ing", "ed", "es", "s"):
            if len(word) > len(end) + 3 and word.endswith(end):
                word = word[: -len(end)]
                break
        out.add(word)
    return out


def says(kept: str, line: str) -> bool:
    """Whether the kept wording plausibly says what `line` does: a number in common, or a third
    of the line's words. A model once grouped two bullets of one project with different findings."""
    if numbers(kept) & numbers(line):
        return True
    mine = stems(line)
    return bool(mine) and len(mine & stems(kept)) / len(mine) >= 0.34


def terms(text: str) -> set[str]:
    """Named things: acronyms, products, tools and names (MCP, LangGraph, Kubernetes)."""
    return {t.lower() for t in re.findall(r"\b[A-Za-z]*[A-Z][A-Za-z0-9+#]*[A-Z0-9][A-Za-z0-9+#]*\b|\b[A-Z]{2,}\b", text)}


def adds(line: str, kept: str) -> bool:
    """Whether `line` states a number or a named thing that `kept` doesn't (a tool's name, a
    count like "across 120 regions")."""
    digits = lambda t: set(re.findall(r"\d+(?:[.,]\d+)?", t))  # noqa: E731 - "2 sections" isn't "5 sections".
    return bool(numbers(line) - numbers(kept)) or bool(digits(line) - digits(kept)) or \
        bool(terms(line) - terms(kept) - {w.lower() for w in words(kept)})


def updated(a: str, b: str) -> bool:
    """One line with a number changed: the same words, different numbers."""
    nums = lambda t: set(re.findall(r"\d+(?:[.,]\d+)?", t))  # noqa: E731
    wa = {w for w in words(strip_bullet(a)) if not w.isdigit()}
    wb = {w for w in words(strip_bullet(b)) if not w.isdigit()}
    return len(wa) >= 4 and wa == wb and nums(a) != nums(b)


def content(text: str) -> set[str]:
    """Stems that carry meaning: "hallucination", "answer", not "by" or "61"."""
    return {w for w in stems(text) if len(w) > 3 and not w.isdigit()}


def run(a: str, b: str) -> int:
    """The longest stretch of words two lines share. One model missed "…used by 4 analytics
    teams through a shared API" in two wordings of one pipeline; a six-word stretch is no accident."""
    wa, wb = norm(strip_bullet(a)).split(), norm(strip_bullet(b)).split()
    best, prev = 0, [0] * (len(wb) + 1)
    for x in wa:
        cur = [0] * (len(wb) + 1)
        for j, y in enumerate(wb, 1):
            if x == y:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def half_alike(a: str, b: str) -> bool:
    wa, wb = words(strip_bullet(a)), words(strip_bullet(b))
    return bool(wa and wb) and len(wa & wb) / len(wa) >= 0.5 and len(wa & wb) / len(wb) >= 0.5


def inside(line: str, other: str) -> bool:
    """`line`'s words and digits are all in `other`, which says more ("July 2024 · Rajasthan"
    within "GPA: 3.71/4.0 · July 2024 · Rajasthan")."""
    mine, theirs = stems(line), stems(other)
    return len(mine) >= 2 and mine < theirs and set(re.findall(r"\d+", line)) <= set(re.findall(r"\d+", other))


def strip_bullet(text: str) -> str:
    from .resumes import BULLET

    return BULLET.sub("", text).strip()


def same_text(a: str, b: str) -> bool:
    """The same line, give or take formatting and a word or two."""
    wa, wb = words(strip_bullet(a)), words(strip_bullet(b))
    if not wa or not wb:
        return False
    if set(re.findall(r"\d+", a)) != set(re.findall(r"\d+", b)):
        return False  # "5 sections" and "2 sections" are different classes.
    shared = len(wa & wb)
    return shared / len(wa) >= 0.8 and shared / len(wb) >= 0.8


SKILL = re.compile(r"^\s*(?:[-*•]\s*)?(?:\*\*)?([^:*]{2,40}?)(?:\*\*)?\s*:\s*(?:\*\*)?\s*(.+)$")


def skill_key(item: str) -> str:
    return re.sub(r"[^a-z0-9+#]", "", item.lower())


def split_items(text: str) -> list[str]:
    return [i.strip(" .") for i in re.split(r",(?![^()]*\))|;", text) if i.strip(" .")]


def merge_skills(section: dict, older: list[dict]) -> None:
    """Each skill once: the newest version's lines and labels, then what only older ones list
    (under their label, joined to a line with the same label), and nothing listed twice."""
    seen: set[str] = set()
    lines: list[tuple[str | None, list[str], str]] = []  # (label, items, bullet prefix)

    def take(text: str, prefix: str):
        match = SKILL.match(text)
        label, body = (match.group(1).strip(), match.group(2)) if match else (None, re.sub(r"^\s*[-*•]\s*", "", text))
        def listed(item: str) -> bool:  # Already there, as itself, its parts or a longer name.
            key = skill_key(item)
            parts = [skill_key(a) for a in re.split(r"[(),/]", item) if skill_key(a)]
            return key in seen or (len(parts) > 1 and all(p in seen for p in parts)) or \
                (len(key) >= 6 and any(k.startswith(key) for k in seen))

        fresh = [i for i in split_items(body) if skill_key(i) and not listed(i)]
        for item in fresh:  # "Orchestration (LangGraph, LangChain)" lists LangGraph too.
            seen.update(skill_key(a) for a in [item, *re.split(r"[(),/]", item)] if skill_key(a))
        if not fresh:
            return
        same = next((l for l in lines if l[0] and label and norm(l[0]) == norm(label)), None)
        if same is None and label is None:
            same = next((l for l in lines if l[0] is None), None)
        if same:
            same[1].extend(fresh)
        else:
            lines.append((label, fresh, prefix))

    entries = section["entries"]
    for entry in entries:
        for text in entry["items"] + entry["meta"]:
            take(text, "- " if re.match(r"^\s*[-*•]", text) else "")
    for old in older:
        for entry in old["entries"]:
            for text in entry["items"] + entry["meta"]:
                take(text, "- " if re.match(r"^\s*[-*•]", text) else "")
    bolded = any("**" in t for e in entries for t in e["items"])
    bulleted = any(re.match(r"^\s*[-*•]", t) for e in entries for t in e["items"])
    out = []
    for label, items, prefix in lines:
        prefix = "- " if bulleted else prefix  # Older lines take the newest version's style.
        body = ", ".join(items)
        if label:
            out.append(f"{prefix}{f'**{label}:**' if bolded else f'{label}:'} {body}")
        else:
            out.append(f"{prefix}{body}")
    section["entries"] = [{"heading": None, "meta": [], "items": out}]
