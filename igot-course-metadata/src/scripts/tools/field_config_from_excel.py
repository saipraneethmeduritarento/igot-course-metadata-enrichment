"""
Regenerate field_config.json from the review workbook's "Fields" sheet, once
stakeholders have edited it.

The sheet needs a "Column" column (dotted field path as exported, e.g.
"explain.sto_reason" or "Targetroles[].Name") and a "Recommended action"
column. Actions map to sources as:
    "Fetch from portal"           -> portal
    "Compute in code"             -> code
    "Remove..."                   -> off
    "Keep - generate with Gemini" -> llm (omitted from the file; llm is the default)
Item-level columns ("X[].Y") and helper "(count)" columns are folded into their
parent field; a parent is only switched away from llm when all its rows agree.

Usage (from the repo root):
    python igot-course-metadata/src/scripts/tools/field_config_from_excel.py all_courses_flattened.xlsx \
        --out igot-course-metadata/src/scripts/v3.7/field_config.json
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from openpyxl import load_workbook

ACTIONS = [
    ("fetch from portal", "portal"),
    ("compute in code", "code"),
    ("remove", "off"),
    ("keep", "llm"),
]
SKIP = {"SourceFile"}


def action_to_source(action: str) -> str:
    low = (action or "").strip().lower()
    for prefix, source in ACTIONS:
        if low.startswith(prefix):
            return source
    return "llm"


def field_path(column: str) -> str:
    """'Targetroles[].Name' -> 'Targetroles'; 'Tags (count)' -> 'Tags'."""
    col = column.replace(" (count)", "")
    return col.split("[]")[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workbook", type=Path)
    ap.add_argument("--sheet", default="Fields")
    ap.add_argument("--out", type=Path, required=True,
                    help="the field_config.json to write, e.g. src/scripts/v3.7/field_config.json")
    args = ap.parse_args()

    ws = load_workbook(args.workbook, read_only=True)[args.sheet]
    rows = ws.iter_rows(values_only=True)
    header = [str(h or "").strip() for h in next(rows)]
    i_col, i_act = header.index("Column"), header.index("Recommended action")

    votes = defaultdict(set)
    for row in rows:
        column = row[i_col]
        if not column or str(column).startswith(("Notes", "'Should")) or column in SKIP:
            continue
        votes[field_path(str(column))].add(action_to_source(str(row[i_act] or "")))

    fields = {}
    for path, sources in sorted(votes.items()):
        if len(sources) == 1:
            source = sources.pop()
            if source != "llm":
                fields[path] = source
        else:
            print(f"mixed actions for {path}: {sorted(sources)} - left as llm")

    existing = {}
    if args.out.is_file():
        existing = json.loads(args.out.read_text(encoding="utf-8"))
    payload = {"_doc": existing.get("_doc", []), "fields": fields}
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(fields)} non-llm field(s) -> {args.out}")


if __name__ == "__main__":
    main()
