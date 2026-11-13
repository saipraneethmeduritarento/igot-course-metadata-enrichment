"""
Regenerate an existing review HTML so it reflects the current output contract.

The course ids are read back out of the old HTML, so the subset stays exactly the
one the reviewers were looking at -- no hardcoded list to drift out of date (which
is what tools/filter_review.py did, and why its 25 ids live in the source).

Two things have to happen, in order, and only the second is cheap:

  1. Regenerate the records. A field added to the contract does not exist in
     rows written before it was added. The HTML renders what is
     in the database, so without this step the new field shows as empty. This
     makes one Vertex call per course and rewrites those rows.

  2. Rebuild the HTML from the refreshed rows.

    # both steps: recall the 25 courses, regenerate them, rewrite the page
    python tools/regen_review_subset.py pdf_courses_review.html

    # see what it would do, and what a run would cost, without calling anything
    python tools/regen_review_subset.py pdf_courses_review.html --dry-run

    # step 2 only -- the rows are already current, just redraw the page
    python tools/regen_review_subset.py pdf_courses_review.html --html-only

By default the page is written back over the file it read the ids from, and the
version it replaces is kept alongside as <name>.<timestamp>.bak.html.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.course_ids import course_ids_from, dedupe  # noqa: E402


def run(cmd: List[str], *, dry_run: bool) -> int:
    print("\n$ " + subprocess.list2cmdline(cmd), flush=True)
    if dry_run:
        print("  [dry-run] not executed")
        return 0
    return subprocess.call(cmd, cwd=str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Regenerate a review HTML and the records behind it."
    )
    ap.add_argument("report", type=Path,
                    help="the existing review .html to take the course ids from")
    ap.add_argument("--out", type=Path, default=None,
                    help="where to write the new page (default: over the input)")
    ap.add_argument("--version", default=os.environ.get("PROMPT_VERSION", "v3.5-advanced"),
                    help="prompt version to regenerate and report on")
    ap.add_argument("--html-only", action="store_true",
                    help="skip regeneration; redraw the page from the rows already stored")
    ap.add_argument("--dry-run", action="store_true",
                    help="print both commands and run neither. meta_gen's own dry run "
                         "still builds every prompt, so use it to check prompt size and cost")
    ap.add_argument("--batch-size", type=int, default=4,
                    help="concurrent courses during regeneration (default 4)")
    ap.add_argument("--no-backup", action="store_true",
                    help="overwrite the input page without keeping a .bak.html copy")
    args = ap.parse_args()

    if not args.report.is_file():
        raise SystemExit(f"no such report: {args.report}")

    ids = dedupe(course_ids_from(args.report))
    if not ids:
        raise SystemExit(f"{args.report}: no course ids found")
    print(f"{args.report.name}: {len(ids)} course id(s)")

    out = args.out or args.report

    # ---- 1. regenerate the records -----------------------------------------
    if not args.html_only:
        cmd = [sys.executable, "meta_gen.py", "--force", "--batch-size", str(args.batch_size)]
        for cid in ids:
            cmd += ["--course-id", cid]
        # --force alone still needs --execute; without it meta_gen is a dry run
        # that builds every prompt and writes nothing. So under --dry-run we do
        # run meta_gen -- just without --execute, which is how you see the real
        # prompt sizes and the cost estimate before committing to the calls.
        if not args.dry_run:
            cmd.append("--execute")
        rc = run(cmd, dry_run=False)
        if rc != 0:
            print(f"\nmeta_gen exited {rc}; not rebuilding the page from a partial run.")
            return rc
        if args.dry_run:
            print("\n[dry-run] records untouched; the page below would be rebuilt next.")

    # ---- 2. rebuild the page -----------------------------------------------
    if out.exists() and out == args.report and not args.no_backup and not args.dry_run:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = out.with_suffix(f".{stamp}.bak.html")
        shutil.copy2(out, backup)
        print(f"\nkept the previous page as {backup.name}")

    cmd = [
        sys.executable, "tools/review_report.py",
        "--version", args.version,
        "--ids-from", str(args.report),
        "--out", str(out),
    ]
    return run(cmd, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
