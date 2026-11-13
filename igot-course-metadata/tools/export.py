"""
Export generated metadata out of Postgres into files.

The worker writes to `course_metadata_regenerated`, which is the right home for
it -- it survives restarts, supports the resume logic, and lets several prompt
versions coexist. But the framework's section 13 also calls for per-course
artefacts, and anyone integrating downstream generally wants files, so this
produces them on demand.

    python tools/export.py                        # one JSON per course + index
    python tools/export.py --format jsonl         # single file, one course/line
    python tools/export.py --format csv           # flat summary for a spreadsheet
    python tools/export.py --split-explain        # also <id>_explain.json
    python tools/export.py --version v3.4-advanced-extended --out out/v34

Every format reads the same rows, so exports of different shapes stay consistent.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

import asyncpg  # noqa: E402

from tools.course_ids import course_ids_from, dedupe  # noqa: E402

FETCH_SQL = """
SELECT course_id, regenerated_json, validation_issues, evidence_tier,
       declared_competency_area, content_set, llm_model, llm_prompt_version,
       processing_duration_seconds, llm_usage_json, regenerated_at, status
  FROM course_metadata_regenerated
 WHERE llm_prompt_version = $1
   AND ($2::text IS NULL OR content_set = $2)
 ORDER BY course_id
"""


def _j(v: Any) -> Any:
    if v is None or isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def flat_row(rec: Dict[str, Any]) -> Dict[str, Any]:
    """One line per course for the CSV: the fields a reviewer scans, not the whole record."""
    g = rec["record"]
    rubric = g.get("RubricScoring") or {}
    pairs = lambda k: "; ".join(
        f"{x.get('Theme')} > {x.get('SubTheme')}" for x in (g.get(k) or [])
    )
    row: Dict[str, Any] = {
        "course_id": rec["course_id"],
        "course_name": g.get("CourseName"),
        "language": g.get("Language"),
        "duration": g.get("Duration"),
        "learning_level": g.get("LearningLevel"),
        "sto_level": (g.get("STOLevel") or {}).get("Level"),
    }
    row.update({
        "total_score": rubric.get("TotalScore"),
        "primary_area": (g.get("PrimaryCompetencyArea") or {}).get("name"),
        "declared_area": rec["declared_competency_area"],
        "agrees_with_declared": (g.get("explain") or {}).get("agrees_with_declared_area"),
        "sector": g.get("Sector"),
        "sub_sector": g.get("SubSector"),
        "sub_sector_theme": g.get("SubSectorTheme"),
        "functional": pairs("FunctionalCompetencies"),
        "behavioural": pairs("BehaviouralCompetencies") or pairs("BehavioralCompetencies"),
        "domain": pairs("DomainCompetencies"),
        "role_bands": "; ".join(g.get("TargetEmployeeGroups") or []),
        "target_roles": "; ".join(
            f"{r.get('Name')} ({r.get('DesignationId')})" for r in (g.get("Targetroles") or [])
        ),
        "tags": "; ".join(g.get("Tags") or []),
        "evidence_tier": rec["evidence_tier"],
        "issue_count": len(rec["validation_issues"] or []),
        "issues": " | ".join(rec["validation_issues"] or []),
        "model": rec["llm_model"],
        "version": rec["llm_prompt_version"],
        "generated_at": rec["regenerated_at"].isoformat() if rec["regenerated_at"] else None,
    })
    return row


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=os.environ.get("PROMPT_VERSION", "v3.5-advanced"))
    ap.add_argument("--content-set", default=None,
                    help="restrict to one set, e.g. non-scorm")
    ap.add_argument("--format", choices=["json", "jsonl", "csv"], default="json")
    ap.add_argument("--split-explain", action="store_true",
                    help="also write <id>_explain.json alongside each record")
    ap.add_argument("--ids-from", type=Path, default=None,
                    help="restrict to the course ids in this file (a records.jsonl "
                         "from meta_gen --run-dir, a previous review HTML, a JSON "
                         "index, or one id per line)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    conn = await asyncpg.connect(dsn=os.environ["DB_DSN"])
    try:
        rows = await conn.fetch(FETCH_SQL, args.version, args.content_set)
    finally:
        await conn.close()

    if args.ids_from:
        # Scopes an export to one run's output. course_ids_from also reads the
        # records.jsonl that --run-dir appends, so the two line up exactly.
        wanted = set(dedupe(course_ids_from(args.ids_from)))
        rows = [r for r in rows if r["course_id"] in wanted]
        missing = len(wanted) - len(rows)
        if missing:
            print(f"note: {missing} of {len(wanted)} requested ids have no "
                  f"{args.version} record")

    if not rows:
        print(f"No rows for {args.version}"
              + (f" / {args.content_set}" if args.content_set else ""))
        return 1

    recs: List[Dict[str, Any]] = []
    for r in rows:
        recs.append({
            "course_id": r["course_id"],
            "record": _j(r["regenerated_json"]) or {},
            "validation_issues": _j(r["validation_issues"]) or [],
            "evidence_tier": r["evidence_tier"],
            "declared_competency_area": r["declared_competency_area"],
            "content_set": r["content_set"],
            "llm_model": r["llm_model"],
            "llm_prompt_version": r["llm_prompt_version"],
            "llm_usage": _j(r["llm_usage_json"]) or {},
            "processing_duration_seconds": float(r["processing_duration_seconds"] or 0),
            "regenerated_at": r["regenerated_at"],
            "status": r["status"],
        })

    out = args.out or ROOT / "output" / args.version
    out.mkdir(parents=True, exist_ok=True)

    if args.format == "json":
        for rec in recs:
            record = rec["record"]
            if args.split_explain:
                # Section 13 of the framework asks for the audit trail as its own
                # artefact; keep it in the main record too so a single file stays
                # self-contained.
                (out / f"{rec['course_id']}_explain.json").write_text(
                    json.dumps(record.get("explain") or {}, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            (out / f"{rec['course_id']}.json").write_text(
                json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
        index = [
            {
                "course_id": r["course_id"],
                "course_name": r["record"].get("CourseName"),
                "file": f"{r['course_id']}.json",
                "primary_area": (r["record"].get("PrimaryCompetencyArea") or {}).get("name"),
                "evidence_tier": r["evidence_tier"],
                "issue_count": len(r["validation_issues"]),
            }
            for r in recs
        ]
        (out / "index.json").write_text(
            json.dumps({"version": args.version, "count": len(recs), "courses": index},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        extra = f" (+{len(recs)} _explain.json)" if args.split_explain else ""
        print(f"wrote {len(recs)} JSON files{extra} + index.json -> {out}")

    elif args.format == "jsonl":
        path = out / f"{args.version}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
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
        print(f"wrote {len(recs)} records -> {path} ({path.stat().st_size/1024:.0f} KB)")

    else:  # csv
        path = out / f"{args.version}.csv"
        flat = [flat_row(r) for r in recs]
        with path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(flat[0]), extrasaction="ignore")
            w.writeheader()
            w.writerows(flat)
        print(f"wrote {len(flat)} rows -> {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
