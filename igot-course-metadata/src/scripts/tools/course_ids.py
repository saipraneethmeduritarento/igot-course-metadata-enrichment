"""
Read a course-id list back out of an artefact the pipeline produced.

Its own module, and stdlib-only on purpose: both review_report.py (which needs a
database) and regen_review_subset.py (which only shells out) import it, and the
second should not have to pull in a DB driver to parse a file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List


def course_ids_from(path: Path) -> List[str]:
    """
    Course ids from a previous review HTML, a JSON export index, or a text file
    of one id per line.

    Reading them back out of the HTML is the point: a subset stays defined by the
    file the reviewers actually have, so regenerating it cannot silently drift
    from the set they were looking at.
    """
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()

    if suffix in (".html", ".htm"):
        m = re.search(
            r'<script type="application/json" id="data">(.*?)</script>', text, re.S
        )
        if not m:
            raise SystemExit(f"{path}: no embedded review data found")
        courses = (json.loads(m.group(1)) or {}).get("courses") or []
        return [c["id"] for c in courses if c.get("id")]

    if suffix in (".json", ".jsonl"):
        if suffix == ".jsonl":
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            blob = json.loads(text)
            rows = (
                (blob.get("courses") or blob.get("ids") or [])
                if isinstance(blob, dict) else blob
            )
        out: List[str] = []
        for r in rows:
            if isinstance(r, dict):
                # Do_ID is how a generated record names itself, so a
                # records.jsonl from meta_gen --run-dir reads back directly.
                cid = r.get("course_id") or r.get("id") or r.get("Do_ID")
                if not cid and isinstance(r.get("_meta"), dict):
                    cid = r["_meta"].get("course_id")
                if cid:
                    out.append(str(cid))
            elif r:
                out.append(str(r))
        return out

    return [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def dedupe(ids: List[str]) -> List[str]:
    """Drop repeats, keep first-seen order -- the order reviewers saw."""
    seen: dict = {}
    for cid in ids:
        if cid:
            seen.setdefault(cid, None)
    return list(seen)
