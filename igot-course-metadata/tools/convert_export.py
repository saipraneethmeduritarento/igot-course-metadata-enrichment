"""
Convert an exported .jsonl into another format.

The JSONL that run_scorm.ps1 / run_non_scorm.ps1 write (and that
bundle_courses.py packages) is the canonical file: one course per line, the full
record plus a `_meta` block. Everything else is a view of it, so convert from
the JSONL rather than re-querying the database -- what you convert is then
exactly what you exported.

    python tools/convert_export.py --set non-scorm --to pkl
    python tools/convert_export.py --set scorm --to xlsx --flat
    python tools/convert_export.py path/to/v3.5-advanced.jsonl --to parquet

pkl / parquet keep the nested competency and role lists as real Python objects.
xlsx and csv cannot hold nested cells, so they need --flat, which JSON-encodes
any list/dict column. Round-trip fidelity therefore only holds for pkl/parquet.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SETS = ("scorm", "non-scorm")
FORMATS = ("pkl", "xlsx", "csv", "parquet", "json")


def default_jsonl(content_set: str) -> Path:
    """The stable per-set export the run scripts refresh on every run."""
    folder = ROOT / "output" / "runs" / content_set
    hits = sorted(folder.glob("*.jsonl"))
    if not hits:
        raise SystemExit(
            f"no .jsonl in {folder} -- run .\\run_{content_set.replace('-', '_')}.ps1 "
            "-Execute first, or pass a path explicitly"
        )
    # One per prompt version; newest wins if a version was bumped mid-flight.
    return max(hits, key=lambda p: p.stat().st_mtime)


def flatten(df: pd.DataFrame) -> pd.DataFrame:
    """JSON-encode nested cells so a spreadsheet writer can hold them."""
    out = df.copy()
    for col in out.columns:
        if out[col].map(lambda v: isinstance(v, (list, dict))).any():
            out[col] = out[col].map(
                lambda v: json.dumps(v, ensure_ascii=False)
                if isinstance(v, (list, dict)) else v
            )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("jsonl", nargs="?", type=Path, default=None,
                    help="source .jsonl (default: the --set export under output/runs/)")
    ap.add_argument("--set", dest="content_set", default="non-scorm", choices=list(SETS),
                    help="which set's export to convert when no path is given")
    ap.add_argument("--to", dest="fmt", default="pkl", choices=list(FORMATS))
    ap.add_argument("--out", type=Path, default=None,
                    help="output file (default: the source path with a new suffix)")
    ap.add_argument("--flat", action="store_true",
                    help="JSON-encode nested columns (required for xlsx/csv)")
    args = ap.parse_args()

    src = args.jsonl or default_jsonl(args.content_set)
    df = pd.read_json(src, lines=True)
    print(f"read {len(df):,} courses x {len(df.columns)} columns <- {src}")

    if args.fmt in ("xlsx", "csv") and not args.flat:
        nested = [c for c in df.columns
                  if df[c].map(lambda v: isinstance(v, (list, dict))).any()]
        if nested:
            ap.error(
                f"{args.fmt} cannot store nested columns ({', '.join(nested[:4])}"
                f"{'...' if len(nested) > 4 else ''}); re-run with --flat"
            )
    if args.flat:
        df = flatten(df)

    out = args.out or src.with_suffix(f".{args.fmt}")
    if args.fmt == "pkl":
        df.to_pickle(out)
    elif args.fmt == "parquet":
        try:
            df.to_parquet(out)
        except ImportError:
            raise SystemExit("parquet needs pyarrow: .venv\\Scripts\\pip install pyarrow")
    elif args.fmt == "xlsx":
        df.to_excel(out, index=False)
    elif args.fmt == "csv":
        df.to_csv(out, index=False, encoding="utf-8-sig")
    else:
        out.write_text(df.to_json(orient="records", indent=2, force_ascii=False),
                       encoding="utf-8")

    print(f"wrote {out}  ({out.stat().st_size/1024/1024:.1f} MB)")
    if args.fmt == "pkl":
        print(f"  read it back with: pd.read_pickle(r\"{out}\")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
