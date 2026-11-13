"""
Run meta_gen.py with the v3.7 prompt (prompts_v37.py) without editing it.

meta_gen.py and validation.py still `import prompts_v36 as P`. This launcher
registers prompts_v37 under that module name first, so both pick up v3.7, then
runs meta_gen.py with the same command-line arguments.

    python run_v37.py --api --course-id do_123 --execute --run-dir runs/x
"""

import os
import runpy
import sys
from pathlib import Path

# .env sets PROMPT_VERSION=v3.5-advanced; v3.7 runs must be tagged v3.7 so the
# DB "already done" check and log names are scoped to this prompt.
os.environ["PROMPT_VERSION"] = os.environ.get("PROMPT_VERSION_V37", "v3.7")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import prompts_v37  # noqa: E402

sys.modules["prompts_v36"] = prompts_v37
sys.argv[0] = str(HERE / "meta_gen.py")
runpy.run_path(str(HERE / "meta_gen.py"), run_name="__main__")
