"""
Locations shared by the tools in src/scripts/tools/.

Importing this module also puts two folders on sys.path:
  - src/scripts/                -> `from tools.course_ids import ...`
  - src/scripts/<ENGINE_VERSION>/ -> `import course_io`, `designations`, `language`

ENGINE_VERSION is the engine the tools borrow those modules from and that
regen_review_subset.py runs. It defaults to v3.6, the engine a bare
`python meta_gen.py` ran before the code moved into version folders; override
it with the ENGINE_VERSION environment variable (e.g. ENGINE_VERSION=v3.7).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent  # src/scripts
PROJECT_ROOT = SCRIPTS_DIR.parent.parent  # igot-course-metadata/ (.env, data/, output/)
ENGINE_VERSION = os.environ.get("ENGINE_VERSION", "v3.6")
ENGINE_DIR = SCRIPTS_DIR / ENGINE_VERSION

if not (ENGINE_DIR / "meta_gen.py").is_file():
    raise SystemExit(f"ENGINE_VERSION={ENGINE_VERSION!r}: no engine at {ENGINE_DIR}")

for _p in (ENGINE_DIR, SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
