"""
Review report for the transcription-based course language (action item 1:
"look at samples before deciding the rule").

For every course folder it resolves the language the way meta_gen.py v3.6 does
(first language per video, majority across the course's videos, portal
fallback) and compares it with the language in previous outputs.

Usage (from the repo root):
    python igot-course-metadata/tools/language_report.py \
        --courses OneDrive_2026-11-06/courses_in_excel \
        --previous all_courses/courses \
        --out logs/language_report.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import language as L  # noqa: E402

DEFAULT_CSV = (
    Path(__file__).resolve().parents[2] / "OneDrive_2026-11-06" / "transcription_soruce_lang.csv"
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--courses", type=Path, required=True, help="folder of do_* course folders")
    ap.add_argument("--language-csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--previous", type=Path, help="folder of previous <id>.json outputs")
    ap.add_argument("--out", type=Path, default=Path("logs/language_report.csv"))
    args = ap.parse_args()

    lang_map = L.load_language_csv(args.language_csv)
    raw_rows = {}
    with args.language_csv.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            raw_rows[r["resource_id"].strip()] = r["source_language"]

    rows = []
    stats = Counter()
    for course_dir in sorted(p for p in args.courses.iterdir() if p.is_dir()):
        cid = course_dir.name
        meta = {}
        try:
            meta = json.loads((course_dir / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        module_ids = sorted({p.name for p in course_dir.rglob("do_*") if p.is_dir()})
        found = [lang_map[m] for m in module_ids if m in lang_map]
        multi = [raw_rows[m] for m in module_ids if m in raw_rows and "," in raw_rows[m]]
        language, distinct, source = L.resolve_course_language(
            module_ids, lang_map, portal_language=meta.get("language")
        )
        previous = None
        if args.previous:
            prev_file = args.previous / f"{cid}.json"
            if prev_file.is_file():
                try:
                    previous = json.loads(prev_file.read_text(encoding="utf-8")).get("Language")
                except (OSError, ValueError):
                    previous = None
        prev_norm = L.normalize_language(previous) if previous else None
        changed = bool(prev_norm and language and prev_norm != language)
        stats["courses"] += 1
        stats[f"source:{source}"] += 1
        stats["scorm" if meta.get("scorm") else "non_scorm"] += 1
        if source.startswith("transcription"):
            stats["scorm_resolved" if meta.get("scorm") else "non_scorm_resolved"] += 1
        if changed:
            stats[f"change:{prev_norm}->{language}"] += 1
        rows.append({
            "course_id": cid,
            "course_name": meta.get("name"),
            "scorm": bool(meta.get("scorm")),
            "videos": len(module_ids),
            "videos_with_language": len(found),
            "video_languages": "; ".join(f"{k} x{v}" for k, v in Counter(found).most_common()),
            "multi_language_rows": " | ".join(multi[:5]),
            "resolved_language": language,
            "source": source,
            "previous_language": previous,
            "changed": changed,
        })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["course_id"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"courses: {stats['courses']}  -> {args.out}")
    print(f"non-SCORM resolved from transcription: {stats['non_scorm_resolved']} / {stats['non_scorm']}")
    print(f"SCORM resolved from transcription:     {stats['scorm_resolved']} / {stats['scorm']}")
    for key, value in sorted(stats.items()):
        if key.startswith("source:"):
            print(f"  {key[7:]:<24} {value}")
    changes = sorted(((k[7:], v) for k, v in stats.items() if k.startswith("change:")),
                     key=lambda kv: -kv[1])
    print(f"language changes vs previous output: {sum(v for _, v in changes)}")
    for k, v in changes[:12]:
        print(f"  {k:<28} {v}")


if __name__ == "__main__":
    main()
