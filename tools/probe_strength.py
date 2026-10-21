#!/usr/bin/env python3
"""How strongly a probe string names the claim it guards. One rule, read by both directions.

A probe is a short literal recorded so that a claim being retracted fails a gate instead of passing
one. It can fail at that in two independent ways, and neither rule sees the other's findings:

  heading-only   The string resolves only to a heading. A section keeps its title while its content
                 is replaced by the opposite finding, so the gate stays green.
  duplicated     The string occurs more than once. Either copy can be reworded with the gate still
                 green, so it guards nothing.

**The two sets do not overlap.** When one repository detected only duplicates and another only
headings, each reported findings the other's rule was silent on — six of each, no row in both. So
holding one rule is indistinguishable from holding none for the class it cannot see.

Length is not the test. `0.18 倍` is eight characters and names a specific measured ratio;
`Cost Explorer` is thirteen and guards nothing. That third weakness -- a string that carries no
claim even when it appears once in body text -- cannot be decided by a rule and is left to a
judgement per row.

This module exists because the rule was about to be written twice. It is imported by
`check_cross_repo.py` for the strings this repository pins elsewhere, and by
`check_inbound_probes.py` for the strings other repositories pin here. A second copy is how the two
directions end up enforcing different rules while both reporting success.
"""

from __future__ import annotations

import re

# Every outcome `verdict` can return. `ok` is the only one that is not a defect.
VERDICTS = ("ok", "absent", "heading-only", "duplicated")


def strip_code(text: str) -> str:
    """Blank out fenced blocks so an example inside one is not read as the real thing.

    Shared with the citation checks for the same reason the verdict is: a document that *shows* a
    probe string as an example must not satisfy a gate by doing so.
    """
    out, fenced = [], False
    for line in text.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
            out.append("")
            continue
        out.append("" if fenced else line)
    return "\n".join(out)


def verdict(probe: str, text: str) -> str:
    """Classify one probe against the document that holds it.

    Pure, so the outcomes can be pinned as a truth table. Three of the four are silent in normal
    use — a passing gate looks identical whether the rule is right or absent — which is the whole
    reason to test them rather than trust them.
    """
    hits = [line for line in strip_code(text).splitlines() if probe in line]
    if not hits:
        return "absent"
    if all(line.lstrip().startswith("#") for line in hits):
        return "heading-only"
    if len(hits) > 1:
        return "duplicated"
    return "ok"
