"""
Review workbook: previous outputs (v3.5) vs a new run (v3.6), laid out for a
human reviewer rather than for a program.

Sheets
  Read me     what the workbook is and how to review it
  Courses     one row per course: title, what content Gemini had, result,
              how many key fields changed, and two columns for the reviewer
  Compare     one block per course, read top to bottom: each key field with a
              plain name, where the new value comes from, before, now, changed?
  All fields  every field of every course (before / now), for digging deeper

Usage (from the repo root):
    python igot-course-metadata/src/scripts/tools/compare_runs.py \
        --new runs/sample10/courses --old all_courses/courses \
        --ids runs/sample10/sample_ids.txt --out runs/sample10/review_sample10.xlsx
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.datavalidation import DataValidation

from _paths import ENGINE_DIR  # noqa: E402

MAX_CELL = 32000
FONT = "Arial"
BLUE = "1F4E78"
CHANGED = PatternFill("solid", fgColor="FFF2CC")   # light yellow
COURSE_BAR = PatternFill("solid", fgColor="D9E1F2")  # light blue
REVIEW = PatternFill("solid", fgColor="E2EFDA")     # light green
THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")

TIER_TEXT = {
    "transcript": "Video transcript (+ PDFs if any)",
    "thin_transcript": "Very short video transcript (+ PDFs if any)",
    "pdf_only": "PDFs only - no video",
    "links_only": "Nothing usable - only web links",
    "metadata_only": "Nothing usable - no video, no PDF",
}
LANG_SOURCE_TEXT = {
    "transcription_api": "Spoken language of the course videos (iGOT transcription service)",
    "transcription_csv": "Spoken language of the course videos (BigQuery transcription file)",
    "transcription_api+csv": "Spoken language of the course videos (iGOT transcription service + BigQuery file)",
    "portal": "Portal language tag - none of the videos had a transcription language",
    "portal_db": "Language stored in the course database - no transcription language found",
}

GEMINI = "Gemini (from transcript / PDF summaries)"
PORTAL = "Portal - copied as-is"
CODE = "Calculated by code"


# ---------------------------------------------------------------------- values
def get(rec: Dict[str, Any], path: str) -> Any:
    cur: Any = rec
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def names(path: str, key: str) -> Callable[[Dict[str, Any]], Any]:
    def f(rec: Dict[str, Any]) -> Any:
        items = get(rec, path)
        if not isinstance(items, list):
            return items
        return [x.get(key) if isinstance(x, dict) else x for x in items]
    return f


def subthemes(path: str) -> Callable[[Dict[str, Any]], Any]:
    def f(rec: Dict[str, Any]) -> Any:
        items = get(rec, path)
        if not isinstance(items, list):
            return items
        out = []
        for x in items:
            if isinstance(x, dict):
                theme, sub = x.get("Theme"), x.get("SubTheme")
                out.append(f"{theme} > {sub}" if theme and sub else (sub or theme))
            else:
                out.append(x)
        return out
    return f


def show(v: Any) -> str:
    """Lists become one item per line; empty becomes a dash."""
    if v is None or v == "" or v == []:
        return "-"
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, list):
        items = [show(x) for x in v]
        return "\n".join(f"• {x}" for x in items) if len(items) > 1 else items[0]
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    s = str(v).strip()
    return s[:MAX_CELL] + " ...[cut]" if len(s) > MAX_CELL else s


def same(a: str, b: str) -> bool:
    norm = lambda s: " ".join(s.replace("•", "").split()).lower()
    return norm(a) == norm(b)


# (plain name, what it means, where the new value comes from, how to read it)
KEY_FIELDS: List[tuple] = [
    ("Title", "Course name shown on the portal", PORTAL, lambda r: get(r, "CourseName")),
    ("Description", "Course description shown on the portal", PORTAL,
     lambda r: get(r, "CourseDescription")),
    ("Duration", "How long the course is", PORTAL, lambda r: get(r, "Duration")),
    ("Language", "Language the course is taught in", None, lambda r: get(r, "Language")),
    ("Summary", "Short write-up of what the course covers", GEMINI,
     lambda r: get(r, "CourseSummary")),
    ("Learning outcomes", "What a learner can do after the course", GEMINI,
     lambda r: get(r, "LearningOutcomes")),
    ("Main competency type", "Domain (subject knowledge), Functional (how-to skills) or "
     "Behavioural (soft skills)", GEMINI, lambda r: get(r, "PrimaryCompetencyArea.name")),
    ("Sector", "Government sector the course belongs to (SGOS list)", GEMINI,
     lambda r: get(r, "Sector")),
    ("Sub-sector", "Ministry / department area (SGOS list)", GEMINI,
     lambda r: get(r, "SubSector")),
    ("Theme", "Specific topic within the sub-sector (SGOS list)", GEMINI,
     lambda r: get(r, "SubSectorTheme")),
    ("Domain competencies", "Subject-knowledge competencies (copy of sub-sector > theme)", CODE,
     subthemes("DomainCompetencies")),
    ("Functional competencies", "How-to skills taught (KCM list)", GEMINI,
     subthemes("FunctionalCompetencies")),
    ("Behavioural competencies", "Soft skills taught (KCM list)", GEMINI,
     subthemes("BehaviouralCompetencies")),
    ("Job level (STO)", "Strategic (senior), Tactical (middle) or Operational (front-line)",
     GEMINI, lambda r: get(r, "STOLevel.Level")),
    ("Difficulty", "Beginner / Intermediate / Advanced - follows from the score below", CODE,
     lambda r: get(r, "LearningLevel")),
    ("Difficulty score", "0-100, sum of the rubric scores Gemini gives", CODE,
     lambda r: get(r, "RubricScoring.TotalScore")),
    ("Target employee groups", "Government employee groups A / B / C / D", GEMINI,
     lambda r: get(r, "TargetEmployeeGroups")),
    ("Target roles", "Job titles the course suits (from the designation list)", GEMINI,
     names("Targetroles", "Name")),
    ("Tags", "Search keywords", GEMINI, lambda r: get(r, "Tags")),
    ("Prior knowledge suggested", "Topics a learner should know before starting", GEMINI,
     lambda r: get(r, "SuggestivePriorKnowledge.SuggestedTopics")),
    ("Learning mode", "Self-paced, instructor-led, ...", GEMINI,
     lambda r: get(r, "LearningMode")),
]
HEADLINE_FOR_COUNT = {f[0] for f in KEY_FIELDS}
# Fields whose source can be switched in field_config.json (label -> field path).
CONFIGURABLE = {"Learning outcomes": "LearningOutcomes", "Learning mode": "LearningMode",
                "Tags": "Tags", "Summary": "CourseSummary"}
FIELD_CONFIG = ENGINE_DIR / "field_config.json"


def configured_source(label: str, default: str) -> str:
    path = CONFIGURABLE.get(label)
    if not path or not FIELD_CONFIG.is_file():
        return default
    src = json.loads(FIELD_CONFIG.read_text(encoding="utf-8")).get("fields", {}).get(path)
    return {"portal": PORTAL, "code": CODE, "off": "Not produced"}.get(src, default)


def flatten(o: Dict[str, Any], p: str = "", row: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    row = {} if row is None else row
    for k, v in o.items():
        q = f"{p}.{k}" if p else k
        if isinstance(v, dict):
            flatten(v, q, row)
        else:
            row[q] = v
    return row


def load(folder: Path, cid: str) -> Optional[Dict[str, Any]]:
    for name in (cid, cid.replace("_rc", "")):
        f = folder / f"{name}.json"
        if f.is_file():
            return json.loads(f.read_text(encoding="utf-8"))
    return None


# ---------------------------------------------------------------------- styling
def header_row(ws, row: int, values: List[str]) -> None:
    for i, v in enumerate(values, start=1):
        c = ws.cell(row=row, column=i, value=v)
        c.font = Font(name=FONT, bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=BLUE)
        c.alignment = WRAP
        c.border = BOX


def body_cell(c, bold: bool = False) -> None:
    c.font = Font(name=FONT, size=10, bold=bold)
    c.alignment = WRAP
    c.border = BOX


def widths(ws, values: List[int]) -> None:
    for i, w in enumerate(values):
        ws.column_dimensions[chr(ord("A") + i)].width = w


# ---------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--new", type=Path, required=True, help="folder of new <id>.json records")
    ap.add_argument("--old", type=Path, required=True, help="folder of previous <id>.json records")
    ap.add_argument("--ids", type=Path, help="course ids file; default: every file in --new")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--old-label", default="v3.5")
    ap.add_argument("--new-label", default="v3.6")
    args = ap.parse_args()
    before, now = f"Before ({args.old_label})", f"Now ({args.new_label})"

    ids = (
        [l.strip() for l in args.ids.read_text(encoding="utf-8").splitlines() if l.strip()]
        if args.ids else sorted(p.stem for p in args.new.glob("*.json"))
    )
    new = {cid: load(args.new, cid) or {} for cid in ids}
    old = {cid: load(args.old, cid) or {} for cid in ids}

    wb = Workbook()

    # ---- Read me ------------------------------------------------------------
    rm = wb.active
    rm.title = "Read me"
    lines = [
        ("What is this?", True),
        (f"A test run of the new metadata generator ({args.new_label}) on {len(ids)} sample "
         f"courses, compared with the metadata the old version ({args.old_label}) produced for "
         "the same courses.", False),
        ("", False),
        ("What changed in the new version", True),
        ("1. Title, Description and Duration are copied from the iGOT portal exactly. Gemini no "
         "longer writes or rewrites them.", False),
        ("2. Language is the spoken language of the course videos (from the transcription "
         "data). If a course has no videos with a known language, the portal's language tag is "
         "used.", False),
        ("3. Gemini only sees the course content - the video transcript and short summaries of "
         "the PDFs. It no longer sees the old title, description, keywords or other metadata.", False),
        ("4. Courses with no video transcript and no PDF are skipped (no Gemini call). They keep "
         "only the portal fields.", False),
        ("", False),
        ("How to review", True),
        ("Step 1 - Go to the 'Courses' sheet. There is one row per course.", False),
        ("Step 2 - Go to the 'Compare' sheet. Each course is a block, read top to bottom. Every "
         "row is one field: what it means, where the new value comes from, the value before, the "
         "value now. Yellow = the value changed.", False),
        ("Step 3 - Back on 'Courses', pick 'OK' or 'Needs fix' in the green 'Your review' column "
         "and write what is wrong in 'Your comments'.", False),
        ("", False),
        ("Things to look at", True),
        ("• Title / Description / Duration should match what you see on the portal.", False),
        ("• Language should be the language the videos are actually spoken in.", False),
        ("• Summary, learning outcomes, competencies, job level and target roles should fit "
         "what the course really teaches.", False),
        ("", False),
        ("The 'All fields' sheet has every field (including Gemini's explanations) if you need "
         "more detail. You can ignore it for a normal review.", False),
    ]
    for i, (text, bold) in enumerate(lines, start=1):
        c = rm.cell(row=i, column=1, value=text)
        c.font = Font(name=FONT, size=12 if bold else 11, bold=bold,
                      color=BLUE if bold else "000000")
        c.alignment = Alignment(wrap_text=True, vertical="top")
    rm.column_dimensions["A"].width = 120

    # ---- Courses ----------------------------------------------------------------
    cs = wb.create_sheet("Courses")
    header_row(cs, 1, ["#", "Course ID", "Title (portal)", "Content Gemini had",
                       "Result", "Key fields changed", "Your review", "Your comments"])
    review_dv = DataValidation(type="list", formula1='"OK,Needs fix"', allow_blank=True)
    cs.add_data_validation(review_dv)

    # ---- Compare ----------------------------------------------------------------
    cmp_ = wb.create_sheet("Compare")
    cmp_cols = ["Field", "What it means", "Where the new value comes from", before, now,
                "Changed?"]
    header_row(cmp_, 1, cmp_cols)
    r = 2

    for n, cid in enumerate(ids, start=1):
        rec_new, rec_old = new[cid], old[cid]
        tier = get(rec_new, "explain.evidence_tier_used") or get(rec_old, "explain.evidence_tier_used")
        skipped = rec_new.get("status") == "insufficient_content"
        if not rec_new:
            result = "Not run"
        elif skipped:
            result = "Skipped - no video transcript or PDF; only portal fields filled"
        else:
            result = "Generated"
        title = rec_new.get("CourseName") or rec_old.get("CourseName") or ""

        # course bar
        bar = cmp_.cell(row=r, column=1,
                        value=f"Course {n} of {len(ids)}:  {title}   ({cid})   -   "
                              f"Content: {TIER_TEXT.get(tier, tier or '?')}   -   {result}")
        bar.font = Font(name=FONT, bold=True, size=11)
        cmp_.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(cmp_cols))
        for col in range(1, len(cmp_cols) + 1):
            cmp_.cell(row=r, column=col).fill = COURSE_BAR
        r += 1

        changed = 0
        for label, meaning, source, read in KEY_FIELDS:
            source = configured_source(label, source)
            a = show(read(rec_old)) if rec_old else "(no old output)"
            if skipped and source != PORTAL and label != "Language":
                b = "(not generated - course skipped)"
            else:
                b = show(read(rec_new)) if rec_new else "(not run)"
            if label == "Language":
                src = get(rec_new, "explain.language_source")
                source = LANG_SOURCE_TEXT.get(src, src or "-")
            not_generated = b.startswith("(not generated")
            is_changed = not not_generated and not same(a, b)
            changed += is_changed
            values = [label, meaning, source, a, b,
                      "Skipped" if not_generated else ("Yes" if is_changed else "")]
            for col, v in enumerate(values, start=1):
                c = cmp_.cell(row=r, column=col, value=v)
                body_cell(c, bold=(col == 1))
                if is_changed and col in (5, 6):
                    c.fill = CHANGED
            r += 1
        r += 1  # blank line between courses

        row = [n, cid, title, TIER_TEXT.get(tier, tier or "?"), result,
               f"{changed} of {len(KEY_FIELDS)}" if not skipped else f"{changed} (portal fields only)",
               "", ""]
        cs.append(row)
        rr = cs.max_row
        for col in range(1, len(row) + 1):
            body_cell(cs.cell(row=rr, column=col))
        for col in (7, 8):
            cs.cell(row=rr, column=col).fill = REVIEW
        review_dv.add(f"G{rr}")

    widths(cs, [4, 30, 45, 34, 34, 14, 14, 60])
    cs.freeze_panes = "C2"
    widths(cmp_, [24, 40, 34, 60, 60, 10])
    cmp_.freeze_panes = "A2"

    # ---- All fields ---------------------------------------------------------------
    af = wb.create_sheet("All fields")
    header_row(af, 1, ["Course ID", "Field (technical name)", before, now, "Changed?"])
    for cid in ids:
        fo, fn = flatten(old[cid]), flatten(new[cid])
        for f in sorted(dict.fromkeys(list(fn) + list(fo))):
            a, b = show(fo.get(f)), show(fn.get(f))
            is_changed = not same(a, b)
            af.append([cid, f, a, b, "Yes" if is_changed else ""])
            rr = af.max_row
            for col in range(1, 6):
                body_cell(af.cell(row=rr, column=col))
            if is_changed:
                af.cell(row=rr, column=4).fill = CHANGED
    widths(af, [30, 44, 70, 70, 10])
    af.freeze_panes = "C2"
    af.auto_filter.ref = af.dimensions

    wb.active = 1  # open on "Courses"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(args.out)
    print(f"{len(ids)} course(s) -> {args.out}")


if __name__ == "__main__":
    main()
