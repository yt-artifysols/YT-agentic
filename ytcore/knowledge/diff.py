"""Line diff between two knowledge versions, for the approval hub's diff view."""

from __future__ import annotations

import difflib
from typing import TypedDict


class DiffLine(TypedDict):
    op: str  # equal | add | remove
    text: str


def line_diff(old: str, new: str) -> list[DiffLine]:
    a, b = old.splitlines(), new.splitlines()
    out: list[DiffLine] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            out.extend({"op": "equal", "text": line} for line in a[i1:i2])
            continue
        out.extend({"op": "remove", "text": line} for line in a[i1:i2])
        out.extend({"op": "add", "text": line} for line in b[j1:j2])
    return out
