"""Checks in code for what the writing guide (docs/writing-guide.md) bans in prose sent to
employers: stock words, stock openers, shapes that read as machine-written, em dashes.

A draft with problems goes back to the model once with the list; whatever is left is
shown to you in the packet. Single words are signals only in clusters, so the list is
kept to what is rarely natural in an application.
"""

import re

WORDS = ["delve", "pivotal", "crucial", "robust", "seamless", "tapestry", "testament", "realm",
         "intricate", "underscore", "showcase", "foster", "leverage", "leveraging", "leveraged",
         "spearhead", "spearheaded", "synergy", "cutting-edge", "innovative", "passionate", "thrilled",
         "honed", "invaluable", "elevate", "empower", "resonates deeply"]
PHRASES = ["i am confident", "i'm confident that", "valuable asset", "unique blend", "proven track record",
           "hit the ground running", "meaningful impact", "at the intersection of", "i hope this finds you",
           "look forward to the opportunity", "excited to apply", "eager to contribute",
           "make a difference", "fast-paced environment"]
OPENERS = re.compile(r"^\W*(i am|i'm) (writing|applying|excited|thrilled|reaching out)|^\W*i would like to apply|"
                     r"express my (strong )?interest", re.I)
SHAPES = {
    "a \"not just X, but Y\" contrast": r"\bnot (just|only|merely)\b[^.?!]{0,80}\bbut\b",
    "an \"it's not X, it's Y\" contrast": r"\b(it'?s|it is|this is|that'?s) not\b[^.?!]{0,60}[,;]\s*(it'?s|it is)\b",
    "a sentence opening with Additionally/Moreover/Furthermore": r"(^|[.!?]\s+)(Additionally|Moreover|Furthermore),",
    "a trailing \"-ing\" clause claiming significance": r", (highlighting|underscoring|showcasing|demonstrating|"
                                                      r"reflecting|fostering|ensuring) (my|the|a|how|that|its)\b",
    "\"serves as\" or \"stands as\" for \"is\"": r"\b(serves|stands) as\b",
}


def problems(text: str, letter: bool = False) -> list[str]:
    """What to fix, as short instructions; [] when clean."""
    if not text:
        return []
    low = text.lower()
    found = [f"the word \"{w}\"" for w in WORDS if re.search(rf"\b{re.escape(w)}\b", low)]
    found += [f"the phrase \"{p}\"" for p in PHRASES if p in low]
    found += [name for name, pattern in SHAPES.items() if re.search(pattern, text, re.M)]
    body = text.split("\n", 2)[-1] if letter and text.lstrip().lower().startswith("dear") else text
    if OPENERS.search(body.strip().split("\n")[0][:200]):
        found.append("a stock opening (\"I'm applying\", \"I am writing\", \"I'm excited\")")
    if "—" in text:
        found.append(f"{text.count('—')} em dash(es): use commas, periods or parentheses")
    if "!" in text:
        found.append("exclamation marks")
    if "**" in text or re.search(r"(?m)^#", text):
        found.append("Markdown formatting")
    return found
