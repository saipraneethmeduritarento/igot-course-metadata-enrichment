"""
Append newly-generated courses into an existing review HTML page without
touching anything already in it.

review_report.py always regenerates a page from scratch with a --limit, which
only works if every course you care about is still within that limit and still
present in course_metadata_regenerated. This instead reads the page's own
embedded <script id="data"> JSON, adds only the courses in the DB that aren't
already in the file (built via review_report.build_records, so the shape is
identical to what review_report.py itself would produce), and writes the
result back. Existing course cards, reviewer verdicts already saved in that
data (none are, they live in the browser's localStorage), and the rest of the
page (layout, CSS, JS) are untouched.

    python tools/append_to_review.py                       # review.html, v3.5-advanced
    python tools/append_to_review.py --file qa.html
    python tools/append_to_review.py --version v3.4-advanced-extended
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import asyncpg  # noqa: E402
import review_report as rr  # noqa: E402

TAG_RE = re.compile(
    r'(<script type="application/json" id="data">)(.*?)(</script>)',
    re.DOTALL,
)

FETCH_ALL_SQL = """
SELECT course_id, regenerated_json, original_metadata_json, validation_issues,
       evidence_tier, declared_competency_area, llm_model, llm_prompt_version,
       regenerated_at, processing_duration_seconds
  FROM course_metadata_regenerated
 WHERE llm_prompt_version = $1
 ORDER BY regenerated_at DESC
"""


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=Path("review.html"),
                    help="existing review page to append into (default review.html)")
    ap.add_argument("--version", default=os.environ.get("PROMPT_VERSION", "v3.5-advanced"))
    args = ap.parse_args()

    if not args.file.is_file():
        print(f"{args.file} does not exist; run tools/review_report.py first to create it")
        return 1

    original = args.file.read_text(encoding="utf-8")
    m = TAG_RE.search(original)
    if not m:
        print(f"could not find the data script tag in {args.file}; no changes made")
        return 1

    existing_payload = json.loads(m.group(2))
    existing_courses = existing_payload["courses"]
    existing_ids = {c["id"] for c in existing_courses}
    print(f"existing courses in {args.file}: {len(existing_courses)}")

    conn = await asyncpg.connect(dsn=os.environ["DB_DSN"])
    try:
        rows = await conn.fetch(FETCH_ALL_SQL, args.version)
    finally:
        await conn.close()

    new_rows = [r for r in rows if r["course_id"] not in existing_ids]
    print(f"courses in DB ({args.version}) not yet in {args.file}: {len(new_rows)}")
    if not new_rows:
        print("nothing to add; file left untouched")
        return 0

    new_records = rr.build_records(new_rows)
    merged_payload = dict(existing_payload)
    merged_payload["courses"] = existing_courses + new_records
    data = json.dumps(merged_payload, ensure_ascii=False).replace("<", "\\u003c")

    new_content = original[: m.start()] + m.group(1) + data + m.group(3) + original[m.end():]
    args.file.write_text(new_content, encoding="utf-8")

    verify = json.loads(TAG_RE.search(args.file.read_text(encoding="utf-8")).group(2))
    print(f"wrote {args.file}  ({len(verify['courses'])} courses total, "
          f"{args.file.stat().st_size / 1024:.0f} KB)")
    print("added:", [r["course_id"] for r in new_rows])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
