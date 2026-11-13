"""
Bundle a run's generated metadata into a folder (and zip) to hand to someone
else. Handles both course sets; each gets its own folder.

Scoped deliberately to the courses run_scorm.ps1 / run_non_scorm.ps1 process,
and nothing else. Two conditions must both hold for a record to be included:

  * content_set matches the set. 'scorm' and 'non-scorm' are reserved for those
    two scripts, which export CONTENT_SET themselves; .env pins everything else
    to 'adhoc', so a bare `python meta_gen.py` cannot land in either set.
  * course_id appears in that set's manifest -- the ids the script actually
    queues, in both local and --api mode.

Records written before .env was pinned to 'adhoc' (2026-09-08) predate that
guarantee: non-scorm rows from then may be hand-runs. --since excludes them.

Either alone would be too loose: the manifest says what *should* have run, the
content_set says what *did*, and only the intersection is that run's output.

    python tools/bundle_courses.py                    # both sets, separate folders
    python tools/bundle_courses.py --set scorm        # just one
    python tools/bundle_courses.py --dry-run          # report only, write nothing
    python tools/bundle_courses.py --no-zip           # leave the folders, skip zips
    python tools/bundle_courses.py --out dist         # parent for the set folders
    python tools/bundle_courses.py --version v3.4-advanced-extended

Partial runs are fine -- it bundles whatever is done at the time and reports how
many of each manifest's courses are still missing.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import glob
import json
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

import asyncpg  # noqa: E402

# Reuse rather than restate: the bundle must not drift from what export.py
# produces, and course_ids_from already parses the manifest's .jsonl shape.
from tools.course_ids import course_ids_from, dedupe  # noqa: E402
from tools.export import FETCH_SQL, _j, flat_row  # noqa: E402

SETS = ("scorm", "non-scorm")

LOG_DIR = ROOT / os.environ.get("LOG_DIR", "logs")

MANIFEST_DIR = Path(
    "C:/Users/RithikaSubrahmanian/OneDrive - Tarento/Desktop/igot/course_metadata"
    "/input/manifests"
)

README = """# iGOT {set} course metadata -- {version}

{count} courses, generated {stamp} by the v3.5 metadata pipeline
(model {model}).

## Files

| File | What it is |
|---|---|
| `{version}.jsonl` | One course per line. The main integration artefact -- stream it, no need to load the whole file. |
| `courses/<course_id>.json` | The same records, one file each, pretty-printed. |
| `index.json` | Course id -> filename, with name, primary competency area, evidence tier and issue count. |
| `{version}.csv` | Flat one-row-per-course summary; competencies and roles collapsed into columns. |

## Record shape

Each JSONL line is the generated record with one added key:

    {{
      "CourseName": ..., "Language": ..., "Duration": ...,
      "PrimaryCompetencyArea": {{"name": ...}},
      "FunctionalCompetencies": [{{"Theme": ..., "SubTheme": ...}}],
      "BehaviouralCompetencies": [...], "DomainCompetencies": [...],
      "Targetroles": [{{"Name": ..., "DesignationId": ...}}],
      "explain": {{...}},              # audit trail: why each choice was made
      "_meta": {{                      # added by the exporter, not the model
        "evidence_tier": ...,          # transcript | thin_transcript | pdf_only
                                       # | links_only | metadata_only
        "declared_competency_area": ...,
        "validation_issues": [...],    # rule violations found post-generation
        "content_set": "{set}",
        "model": ..., "generated_at": ...
      }}
    }}

`GeneratedOn` inside each record is UTC (ISO-8601 with a `+00:00` suffix).

## Caveats

- `validation_issues` is advisory: a record with issues was still written, with
  the offending field corrected where the rule allowed it.
- {tiers}
"""


async def fetch(version: str, content_set: str) -> List[Dict[str, Any]]:
    conn = await asyncpg.connect(dsn=os.environ["DB_DSN"])
    try:
        rows = await conn.fetch(FETCH_SQL, version, content_set)
    finally:
        await conn.close()
    return [
        {
            "course_id": r["course_id"],
            "record": _j(r["regenerated_json"]) or {},
            "validation_issues": _j(r["validation_issues"]) or [],
            "evidence_tier": r["evidence_tier"],
            "declared_competency_area": r["declared_competency_area"],
            "content_set": r["content_set"],
            "llm_model": r["llm_model"],
            "llm_prompt_version": r["llm_prompt_version"],
            "regenerated_at": r["regenerated_at"],
        }
        for r in rows
    ]


def write_bundle(
    recs: List[Dict[str, Any]], out: Path, version: str, content_set: str
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    courses = out / "courses"
    courses.mkdir(exist_ok=True)

    for rec in recs:
        (courses / f"{rec['course_id']}.json").write_text(
            json.dumps(rec["record"], indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    with (out / f"{version}.jsonl").open("w", encoding="utf-8") as fh:
        for rec in recs:
            fh.write(json.dumps({
                **rec["record"],
                "_meta": {
                    "evidence_tier": rec["evidence_tier"],
                    "declared_competency_area": rec["declared_competency_area"],
                    "validation_issues": rec["validation_issues"],
                    "content_set": rec["content_set"],
                    "model": rec["llm_model"],
                    "generated_at": rec["regenerated_at"].isoformat()
                    if rec["regenerated_at"] else None,
                },
            }, ensure_ascii=False) + "\n")

    (out / "index.json").write_text(
        json.dumps({
            "version": version,
            "content_set": content_set,
            "count": len(recs),
            "generated": datetime.now().astimezone().isoformat(),
            "courses": [
                {
                    "course_id": r["course_id"],
                    "course_name": r["record"].get("CourseName"),
                    "file": f"courses/{r['course_id']}.json",
                    "primary_area": (r["record"].get("PrimaryCompetencyArea") or {}).get("name"),
                    "evidence_tier": r["evidence_tier"],
                    "issue_count": len(r["validation_issues"]),
                }
                for r in recs
            ],
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    flat = [flat_row(r) for r in recs]
    with (out / f"{version}.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(flat[0]), extrasaction="ignore")
        w.writeheader()
        w.writerows(flat)


def tier_line(recs: List[Dict[str, Any]]) -> str:
    counts: Dict[str, int] = {}
    for r in recs:
        counts[r["evidence_tier"]] = counts.get(r["evidence_tier"], 0) + 1
    parts = ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
    return (
        f"Evidence tiers in this bundle: {parts}. SCORM packages mostly expose no "
        "transcript or PDF text, so `metadata_only` dominating that set is "
        "expected, not a fault."
    )


def outcome_ids(content_set: str, pattern: Optional[str]) -> Tuple[Set[str], List[str]]:
    """
    Course ids that run_scorm.ps1 / run_non_scorm.ps1 actually processed, read
    from the per-run outcome CSVs they write.

    This is the one artefact that identifies a scripted run. Only those scripts
    pass --out logs/<set>-outcomes-<stamp>.csv; a bare `python meta_gen.py`
    falls back to meta_gen_<version>_<stamp>.csv, so the filename itself is the
    provenance marker and hand-runs are excluded by not matching the glob.

    Rows are filtered to courses that actually produced a record -- a dry_run or
    failed row names a course the run touched but never wrote.
    """
    glob_pat = pattern or str(LOG_DIR / f"{content_set}-outcomes-*.csv")
    files = sorted(glob.glob(glob_pat))
    ids: Set[str] = set()
    for path in files:
        # utf-8-sig: an early run's id list carried a BOM that ended up in the
        # first course_id, and the same BOM reaches this CSV.
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                if (row.get("status") or "").startswith("success"):
                    ids.add((row.get("course_id") or "").strip())
    ids.discard("")
    return ids, files


def zip_dir(folder: Path) -> Path:
    archive = folder.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(folder.parent))
    return archive


async def bundle_one(args: argparse.Namespace, content_set: str, parent: Path) -> bool:
    """One set. Returns True if anything was bundled (or would be, when dry)."""
    manifest = args.manifest or MANIFEST_DIR / f"{content_set}.win.jsonl"
    print(f"\n=== {content_set} ===")
    if not manifest.is_file():
        print(f"missing manifest: {manifest}")
        return False
    wanted = set(dedupe(course_ids_from(manifest)))

    recs = await fetch(args.version, content_set)
    in_scope = [r for r in recs if r["course_id"] in wanted]
    stray = len(recs) - len(in_scope)
    missing = len(wanted) - len(in_scope)
    script = "run_scorm.ps1" if content_set == "scorm" else "run_non_scorm.ps1"

    print(f"manifest              : {len(wanted)} courses ({manifest.name})")
    print(f"content_set           : {len(recs)} records for {args.version}")
    if missing:
        print(f"not yet generated     : {missing} (run .\\{script} -Execute to finish)")
    if stray:
        # Records carrying this content_set that the manifest does not list --
        # e.g. a course run by hand with CONTENT_SET exported in the shell.
        print(f"excluded, off-manifest: {stray}")

    # Optional narrowing, on top of the manifest + content_set scoping above.
    if args.from_outcomes is not None:
        ran, files = outcome_ids(content_set, args.from_outcomes or None)
        before = len(in_scope)
        in_scope = [r for r in in_scope if r["course_id"] in ran]
        print(f"scripted-run filter   : {len(files)} outcome CSV(s), "
              f"{len(ran)} ids -> dropped {before - len(in_scope)}")
        if not files:
            print("  (no outcome CSVs matched; nothing was run by the scripts yet)")

    if args.since:
        cutoff = datetime.fromisoformat(args.since).astimezone()
        before = len(in_scope)
        in_scope = [
            r for r in in_scope
            if r["regenerated_at"] and r["regenerated_at"].astimezone() >= cutoff
        ]
        print(f"since {cutoff.date()}          : dropped {before - len(in_scope)}")

    print(f"in bundle             : {len(in_scope)}")

    if not in_scope:
        print("nothing to bundle")
        return False
    if args.dry_run:
        print(tier_line(in_scope))
        return True

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = parent / f"{content_set}-bundle-{stamp}"
    write_bundle(in_scope, out, args.version, content_set)
    (out / "README.md").write_text(
        README.format(
            set=content_set,
            version=args.version,
            count=len(in_scope),
            stamp=datetime.now().strftime("%Y-%m-%d"),
            model=in_scope[0]["llm_model"],
            tiers=tier_line(in_scope),
        ),
        encoding="utf-8",
    )

    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"bundle -> {out}  ({size/1024/1024:.1f} MB)")
    if not args.no_zip:
        archive = zip_dir(out)
        print(f"zip    -> {archive}  ({archive.stat().st_size/1024/1024:.1f} MB)")
    return True


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--version", default=os.environ.get("PROMPT_VERSION", "v3.5-advanced"))
    ap.add_argument("--set", dest="content_set", default=None, choices=list(SETS),
                    help="bundle just one set (default: both, into separate folders)")
    ap.add_argument("--manifest", type=Path, default=None,
                    help="override the manifest (only valid with --set)")
    ap.add_argument("--out", type=Path, default=None,
                    help="parent folder for the set folders (default output/)")
    ap.add_argument("--from-outcomes", nargs="?", const="", default=None, metavar="GLOB",
                    help="restrict to courses a scripted run actually wrote, read from "
                         "logs/<set>-outcomes-*.csv. Pass a glob to pick specific runs, "
                         "e.g. --from-outcomes \"logs/scorm-outcomes-202609*.csv\"")
    ap.add_argument("--since", metavar="YYYY-MM-DD",
                    help="only records generated on or after this date (local time)")
    ap.add_argument("--no-zip", action="store_true", help="leave the folders, skip the zips")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be bundled and write nothing")
    args = ap.parse_args()

    if args.manifest and not args.content_set:
        ap.error("--manifest applies to a single set; pass --set too")

    targets = [args.content_set] if args.content_set else list(SETS)
    parent = args.out or ROOT / "output"

    any_written = False
    for content_set in targets:
        if await bundle_one(args, content_set, parent):
            any_written = True
    return 0 if any_written else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
