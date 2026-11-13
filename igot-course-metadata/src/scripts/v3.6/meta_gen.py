"""
iGOT course metadata regeneration worker -- v3.6 (content-only input).

v3.6: the model sees only course content (transcript + PDF chunk summaries) plus
the masters and the output schema -- never the course's platform metadata.
Title, description and duration are copied from the portal, Language comes from
the transcription source language (language.py), and which fields the model
generates is set in field_config.json. Courses with no transcript and no PDF are
not sent to the model; they get a portal-only record (status insufficient_content).
Prompt text: src/prompts/v3.6.yaml; schema and assembly: prompts.py in this
folder (src/scripts/v3.5/prompts.py is the unchanged baseline).

Supersedes the v3.4-advanced-extended implementation. Prompt and output schema
now live in prompts.py (text in src/prompts/); content extraction in course_io.py; output checking
in validation.py; target-role retrieval in designations.py.

Follows the conventions in legacy bulk_scripts/README.md: dry-run by default,
`--execute` to make real calls and writes, `--batch-size` for concurrency, an
outcome CSV plus a log file every run, mandatory env vars enforced up front, and
safe to re-run (finished courses are skipped unless `--force`).

Run from the project root (igot-course-metadata/), where .env and its relative
paths resolve:
    python src/scripts/v3.6/meta_gen.py --limit 10                     # dry run: no AI calls, no writes
    python src/scripts/v3.6/meta_gen.py --limit 10 --execute           # pilot for real
    python src/scripts/v3.6/meta_gen.py --course-id do_... --execute   # single course
    python src/scripts/v3.6/meta_gen.py --execute                      # drain all pending, then exit
    python src/scripts/v3.6/meta_gen.py --execute --serve              # stay resident, poll for work
    python src/scripts/v3.6/meta_gen.py --api --sample 10 --run-dir runs/sample10 --execute
                                                                       # 10-course spread across tiers
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
import httpx
from dotenv import load_dotenv

# src/scripts/<version>/meta_gen.py -> project root (igot-course-metadata/)
PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")

from google import genai
from google.auth import exceptions as google_auth_exceptions
from google.genai import types
from google.genai import errors as genai_errors

import api_fetcher
import course_io
import language as lang_mod
import prompts as P
from designations import DesignationIndex, build_content_retrieval_query
from pdf_summarizer import PdfSummarizer
from validation import MasterData, build_portal_only_record, validate_and_repair

# ---------------------------------------------------------------- configuration
DB_DSN = os.environ.get("DB_DSN", "postgresql://localhost:5432/karmayogi_db")
ENRICHMENT_DSN = os.environ.get("ENRICHMENT_DSN") or None

COURSES_BASE_PATH = Path(os.environ.get("COURSES_BASE_PATH", "course_data"))
COURSE_MANIFEST = os.environ.get("COURSE_MANIFEST") or None
CONTENT_SET = os.environ.get("CONTENT_SET", "non-scorm")

# The competency master: one entry per Theme/SubTheme pair with its definitions.
KCM_PATH = Path(os.environ.get("KCM_PATH", "kcm_competencies.json"))
SGOS_PATH = Path(os.environ.get("SGOS_PATH", "SGOS 1.json"))
DESIGNATIONS_PATH = Path(os.environ.get("DESIGNATIONS_PATH", "igot_designations.csv"))
DESIGNATION_INDEX_PATH = Path(
    os.environ.get("DESIGNATION_INDEX_PATH", "data/designation_index.npz")
)

# META_GEN_* deliberately shadow GENAI_MODEL_NAME / GOOGLE_LOCATION, which in the
# shared .env point at gemini-2.5-flash in asia-south1. No pro model is served
# from asia-south1 for this project, and flash would regress quality against the
# v3.4 baseline, which ran on gemini-2.5-pro.
GENAI_MODEL_NAME = os.environ.get("META_GEN_MODEL") or os.environ.get(
    "GENAI_MODEL_NAME", "gemini-3.1-pro-preview"
)
GOOGLE_LOCATION = os.environ.get("META_GEN_LOCATION") or os.environ.get(
    "GOOGLE_LOCATION", "global"
)
GOOGLE_PROJECT_ID = os.environ.get("GOOGLE_PROJECT_ID")

MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "4"))
WORKER_BATCH_SIZE = int(os.environ.get("WORKER_BATCH_SIZE", "20"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
DESIGNATION_CANDIDATES = int(os.environ.get("DESIGNATION_CANDIDATES", "150"))
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.1"))
LLM_MAX_OUTPUT_TOKENS = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "32768"))
# Without a ceiling a single generate_content call can hang indefinitely and wedge
# the whole run (observed: a worker stuck >10 min with no progress and no error).
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS_META", "300"))

# Courses left 'in_progress' by a killed worker are reclaimed after this long.
STALE_CLAIM_MINUTES = int(os.environ.get("STALE_CLAIM_MINUTES", "30"))

# Published USD per 1M tokens, for run cost reporting. Defaults are
# gemini-3.1-pro-preview at the <=200k-prompt tier ($4/$18 above 200k; our
# prompts run ~55k-140k so the low tier applies). Verify against
# cloud.google.com/vertex-ai/generative-ai/pricing -- these are only as current
# as whoever last edited .env.
PRICE_INPUT_PER_M = float(os.environ.get("PRICE_INPUT_PER_M", "2.00"))
PRICE_OUTPUT_PER_M = float(os.environ.get("PRICE_OUTPUT_PER_M", "12.00"))
PRICE_CACHED_INPUT_PER_M = float(os.environ.get("PRICE_CACHED_INPUT_PER_M", "0.20"))

LLM_PROMPT_VERSION = P.PROMPT_VERSION

# Transcription source language per video (BigQuery export). Fallback for the
# per-video language the Learning API reports in --api mode.
LANGUAGE_CSV = Path(
    os.environ.get("LANGUAGE_CSV")
    or PROJECT_ROOT.parent / "OneDrive_2026-11-06" / "transcription_soruce_lang.csv"
)
# Model used for PDF chunk summaries; defaults to the main model.
PDF_SUMMARY_MODEL = os.environ.get("PDF_SUMMARY_MODEL") or GENAI_MODEL_NAME
# Tiers with no transcript and no PDF: not sent to the model at all.
SKIP_TIERS = {"metadata_only", "links_only"}
# Char limits for the per-course prompt (one truncation layer: course_io's caps
# are larger, so these are the effective ones).
PROMPT_TRANSCRIPT_CHARS = int(os.environ.get("PROMPT_TRANSCRIPT_CHARS", "200000"))
PROMPT_PDF_SUMMARY_CHARS = int(os.environ.get("PROMPT_PDF_SUMMARY_CHARS", "120000"))

# ---------------------------------------------------------------------- logging
LOG_DIR = Path(os.environ.get("LOG_DIR", "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_LEVEL = os.environ.get("META_GEN_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(
            LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}-{P.PROMPT_VERSION}.log",
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
# The shared .env sets LOG_LEVEL=DEBUG for another service; honour it for our own
# loggers only via META_GEN_LOG_LEVEL, and keep the SDK's chatter down.
for noisy in ("google_genai", "google.genai", "httpx", "httpcore", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger("course-regenerator")

_shutdown = asyncio.Event()

# ------------------------------------------------------------------------- SQL
DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS course_processing_checkpoint (
      course_id TEXT PRIMARY KEY,
      source_folder TEXT,
      status TEXT,
      last_updated TIMESTAMP WITH TIME ZONE,
      attempts INTEGER DEFAULT 0,
      last_error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS course_metadata_regenerated (
      course_id TEXT NOT NULL,
      regenerated_json JSONB NOT NULL,
      source_folder TEXT,
      llm_model TEXT,
      llm_prompt_version TEXT,
      processing_duration_seconds NUMERIC,
      llm_usage_json JSONB,
      original_metadata_json JSONB,
      status TEXT,
      regenerated_at TIMESTAMP WITH TIME ZONE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS course_processing_errors (
      id BIGSERIAL PRIMARY KEY,
      course_id TEXT,
      error_message TEXT,
      created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
    )
    """,
    # v3.5 additions. Separate ALTERs so a partially-migrated DB still converges.
    "ALTER TABLE course_processing_checkpoint ADD COLUMN IF NOT EXISTS content_set TEXT",
    "ALTER TABLE course_processing_checkpoint ADD COLUMN IF NOT EXISTS prompt_version TEXT",
    "ALTER TABLE course_metadata_regenerated ADD COLUMN IF NOT EXISTS validation_issues JSONB",
    "ALTER TABLE course_metadata_regenerated ADD COLUMN IF NOT EXISTS evidence_tier TEXT",
    "ALTER TABLE course_metadata_regenerated ADD COLUMN IF NOT EXISTS declared_competency_area TEXT",
    "ALTER TABLE course_metadata_regenerated ADD COLUMN IF NOT EXISTS content_set TEXT",
]

# The v3.4 table was keyed on course_id alone, so re-running would overwrite the
# 368-row v3.4 baseline we want to compare against. Widen the key to include the
# prompt version so both generations coexist.
MIGRATE_PK_SQL = """
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'course_metadata_regenerated_pkey'
      AND conrelid = 'course_metadata_regenerated'::regclass
      AND array_length(conkey, 1) = 1
  ) THEN
    ALTER TABLE course_metadata_regenerated
      DROP CONSTRAINT course_metadata_regenerated_pkey;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'course_metadata_regenerated_pkey'
      AND conrelid = 'course_metadata_regenerated'::regclass
  ) THEN
    UPDATE course_metadata_regenerated
       SET llm_prompt_version = COALESCE(llm_prompt_version, 'unknown')
     WHERE llm_prompt_version IS NULL;
    ALTER TABLE course_metadata_regenerated
      ALTER COLUMN llm_prompt_version SET NOT NULL;
    ALTER TABLE course_metadata_regenerated
      ADD CONSTRAINT course_metadata_regenerated_pkey
      PRIMARY KEY (course_id, llm_prompt_version);
  END IF;
END $$;
"""

UPSERT_CHECKPOINT_SQL = """
INSERT INTO course_processing_checkpoint
  (course_id, source_folder, status, last_updated, attempts, content_set, prompt_version)
VALUES ($1, $2, 'pending', now(), 0, $3, $4)
ON CONFLICT (course_id) DO UPDATE
  SET source_folder = EXCLUDED.source_folder,
      content_set   = EXCLUDED.content_set,
      status        = CASE
                        WHEN course_processing_checkpoint.prompt_version IS DISTINCT FROM EXCLUDED.prompt_version
                        THEN 'pending'
                        ELSE course_processing_checkpoint.status
                      END,
      attempts      = CASE
                        WHEN course_processing_checkpoint.prompt_version IS DISTINCT FROM EXCLUDED.prompt_version
                        THEN 0
                        ELSE course_processing_checkpoint.attempts
                      END,
      prompt_version = EXCLUDED.prompt_version
"""

CLAIM_COURSE_SQL = """
UPDATE course_processing_checkpoint
   SET status = 'in_progress',
       attempts = course_processing_checkpoint.attempts + 1,
       last_updated = now()
 WHERE course_id = $1
   AND status IN ('pending', 'failed')
RETURNING course_id, source_folder
"""

SELECT_PENDING_SQL = """
SELECT course_id, source_folder
  FROM course_processing_checkpoint
 WHERE status IN ('pending', 'failed')
   AND attempts < $2
   AND content_set = $3
   AND ($4::text[] IS NULL OR course_id = ANY($4))
 ORDER BY attempts, last_updated
 LIMIT $1
"""

# Hand back a claim the run never got to process (shutdown, cancellation, a
# crash between claiming and finishing). The attempt is undone as well: the
# course was never actually tried, so burning one of its MAX_ATTEMPTS would
# march it towards 'dead' for reasons that have nothing to do with the course.
# The status guard keeps this from clobbering a row that did finish.
RELEASE_CLAIM_SQL = """
UPDATE course_processing_checkpoint
   SET status = 'pending',
       attempts = GREATEST(0, attempts - 1),
       last_updated = now()
 WHERE course_id = ANY($1)
   AND status = 'in_progress'
RETURNING course_id
"""

# A worker killed mid-flight leaves rows in 'in_progress'. Those are not in the
# pending set, so without this they are stranded permanently and a restart
# silently skips them. RELEASE_CLAIM_SQL covers the orderly exits; this is the
# backstop for a hard kill, where no cleanup code gets to run at all.
RECLAIM_STALE_SQL = """
UPDATE course_processing_checkpoint
   SET status = 'pending', last_updated = now()
 WHERE status = 'in_progress'
   AND content_set = $1
   AND last_updated < now() - ($2 || ' minutes')::interval
RETURNING course_id
"""

SAVE_RESULT_SQL = """
INSERT INTO course_metadata_regenerated
  (course_id, regenerated_json, source_folder, llm_model, llm_prompt_version,
   processing_duration_seconds, llm_usage_json, original_metadata_json, status,
   regenerated_at, validation_issues, evidence_tier, declared_competency_area, content_set)
VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, now(),
        $10::jsonb, $11, $12, $13)
ON CONFLICT (course_id, llm_prompt_version) DO UPDATE
  SET regenerated_json = EXCLUDED.regenerated_json,
      source_folder = EXCLUDED.source_folder,
      llm_model = EXCLUDED.llm_model,
      processing_duration_seconds = EXCLUDED.processing_duration_seconds,
      llm_usage_json = EXCLUDED.llm_usage_json,
      original_metadata_json = EXCLUDED.original_metadata_json,
      status = EXCLUDED.status,
      regenerated_at = now(),
      validation_issues = EXCLUDED.validation_issues,
      evidence_tier = EXCLUDED.evidence_tier,
      declared_competency_area = EXCLUDED.declared_competency_area,
      content_set = EXCLUDED.content_set
"""

MARK_DONE_SQL = """
UPDATE course_processing_checkpoint
   SET status = 'done', last_updated = now(), last_error = NULL
 WHERE course_id = $1
"""

MARK_FAILED_SQL = """
UPDATE course_processing_checkpoint
   SET status = CASE WHEN attempts >= $3 THEN 'dead' ELSE 'failed' END,
       last_updated = now(),
       last_error = $2
 WHERE course_id = $1
"""

LOG_ERROR_SQL = """
INSERT INTO course_processing_errors (course_id, error_message, created_at)
VALUES ($1, $2, now())
"""


# --------------------------------------------------------------------- helpers
def load_json_file(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"Missing required reference file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Could not parse JSON from {path}: {exc}") from exc


def _is_transient(exc: BaseException) -> bool:
    """
    Only transient failures are worth retrying.

    The v3.4 code retried on `Exception`, so permanent errors (bad credentials, a
    rejected schema, an unknown model) burned three backed-off attempts each.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    # A connection dropped mid-request. httpx deliberately does not subclass
    # OSError, so these slipped past the check above and failed a course outright
    # on the first attempt -- and because one blip kills every request in flight,
    # a whole batch died together. TransportError covers read/write/connect
    # errors, pool timeouts and RemoteProtocolError; a real HTTP status arrives as
    # HTTPStatusError, which the genai checks below classify instead.
    if isinstance(exc, httpx.TransportError):
        return True
    # The access token is refreshed over the network roughly hourly, so a long
    # batch run always does it mid-flight. A blip during that refresh raises this
    # -- GoogleAuthError, not OSError -- and would otherwise fail every course in
    # flight on the first attempt. RefreshError is deliberately not here: the
    # token endpoint rejecting the key is a permanent credentials problem.
    if isinstance(exc, google_auth_exceptions.TransportError):
        return True
    if isinstance(exc, json.JSONDecodeError):
        return True  # truncated candidate; a re-roll often succeeds
    if isinstance(exc, genai_errors.ServerError):
        return True
    if isinstance(exc, genai_errors.ClientError):
        return getattr(exc, "code", None) in (408, 409, 425, 429)
    if isinstance(exc, genai_errors.APIError):
        return getattr(exc, "code", None) in (408, 409, 425, 429, 500, 502, 503, 504)
    return False


class EnrichmentStore:
    """
    Platform-verified facts from cbp_tpc_ai.course_metadata_v3.

    Supplies language, duration and difficulty_level, which the folder-level
    metadata.json does not carry. Covers 3395 of the 3707 non-SCORM courses.
    Read-only: this worker never writes to the enrichment database.
    """

    def __init__(self, rows: Dict[str, Dict[str, Any]]) -> None:
        self.rows = rows

    @classmethod
    async def load(cls, dsn: Optional[str]) -> "EnrichmentStore":
        if not dsn:
            logger.warning("ENRICHMENT_DSN not set; language/duration will be inferred")
            return cls({})
        rows: Dict[str, Dict[str, Any]] = {}
        try:
            conn = await asyncpg.connect(dsn=dsn)
        except Exception as exc:
            logger.warning("enrichment DB unavailable (%s); continuing without it", exc)
            return cls({})
        try:
            records = await conn.fetch(
                "SELECT identifier, difficulty_level, language, duration, organisation "
                "FROM course_metadata_v3"
            )
            for rec in records:
                ident = rec["identifier"]
                if not ident:
                    continue
                langs = list(rec["language"] or [])
                orgs = list(rec["organisation"] or [])
                seconds: Optional[float] = None
                raw_duration = rec["duration"]
                if raw_duration:
                    try:
                        seconds = float(str(raw_duration).strip())
                    except (TypeError, ValueError):
                        seconds = None
                rows[ident] = {
                    "language": langs[0] if langs else None,
                    "all_languages": langs,
                    "duration_seconds": seconds,
                    "declared_difficulty": rec["difficulty_level"] or None,
                    "organisation": orgs[0] if orgs else None,
                }
        finally:
            await conn.close()
        logger.info("loaded platform facts for %d courses", len(rows))
        return cls(rows)

    def facts_for(
        self,
        course_id: str,
        folder_meta: Optional[Dict[str, Any]],
        content: Dict[str, Any],
    ) -> Dict[str, Any]:
        row = self.rows.get(course_id, {})
        meta = folder_meta or {}

        # Portal duration: the Learning API value saved in metadata.json (--api),
        # else course_metadata_v3.
        seconds: Optional[float] = None
        try:
            seconds = float(meta.get("duration")) if meta.get("duration") else None
        except (TypeError, ValueError):
            seconds = None
        duration_source = "portal_api"
        if not seconds:
            seconds = row.get("duration_seconds")
            duration_source = "platform"
        if not seconds:
            # VTT cue spans underestimate real duration (median ratio 0.59 against
            # platform values), so this is a fallback only and is labelled as such.
            seconds = content.get("transcript_seconds") or None
            duration_source = "transcript-derived (approximate)" if seconds else "unavailable"

        return {
            # Portal values copied as-is into the output (field_config.json).
            "course_name": meta.get("name"),
            "course_description": meta.get("description"),
            # Portal learning outcomes live in the course's "instructions" field
            # (one HTML string); validation.split_portal_list turns it into a list.
            "learning_outcomes": meta.get("instructions"),
            # Portal language tags: fallbacks only. The course language is
            # resolved from the transcription source language in process().
            "portal_language": meta.get("language"),
            "db_language": row.get("language"),
            "language": row.get("language"),
            "all_languages": row.get("all_languages") or [],
            "duration_seconds": seconds,
            "duration_source": duration_source,
            "provider": meta.get("organisation") or row.get("organisation"),
            "module_count": content.get("modules_total"),
            "modules_with_transcript": content.get("modules_with_transcript"),
            "pdf_count": content.get("pdf_count"),
            "course_category": meta.get("courseCategory"),
            # Present for audit only. v3.5 states the incoming difficulty level is
            # not correct, so it is never offered to the model as a fact.
            "_declared_difficulty_not_authoritative": row.get("declared_difficulty"),
        }


# ------------------------------------------------------------------ LLM calling
class LLMClient:
    def __init__(self, client: Any, static_prefix: str) -> None:
        self.client = client
        self.static_prefix = static_prefix

    async def generate(
        self, course_section: str, native_pdfs: Optional[List[Path]] = None
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        prompt = self.static_prefix + course_section
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=P.METADATA_SCHEMA,
            temperature=LLM_TEMPERATURE,
            max_output_tokens=LLM_MAX_OUTPUT_TOKENS,
        )
        parts = [types.Part.from_text(text=prompt)]
        # Attach PDFs whose text layer was unusable -- scanned (PyMuPDF does no
        # OCR) or written in a legacy non-Unicode Indic font. The model renders
        # the pages, which recovers both: a Krutidev-encoded Hindi document that
        # extracted as "varjkZ\"Vªh; ;ksx" reads correctly as
        # "अंतर्राष्ट्रीय योग" this way.
        for path in native_pdfs or []:
            try:
                parts.append(
                    types.Part.from_bytes(
                        data=path.read_bytes(), mime_type="application/pdf"
                    )
                )
            except OSError as exc:
                logger.warning("could not attach %s: %s", path, exc)
        contents = [types.Content(role="user", parts=parts)]

        last_exc: Optional[BaseException] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            start = time.time()
            try:
                resp = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=GENAI_MODEL_NAME, contents=contents, config=config
                    ),
                    timeout=LLM_TIMEOUT_SECONDS,
                )
                if not resp.text:
                    raise json.JSONDecodeError("empty candidate text", "", 0)
                parsed = json.loads(resp.text)
                usage = resp.usage_metadata.to_json_dict() if resp.usage_metadata else {}
                usage["_elapsed_seconds"] = round(time.time() - start, 2)
                usage["_attempts"] = attempt
                return parsed, usage
            except BaseException as exc:  # noqa: BLE001 - classified below
                last_exc = exc
                if not _is_transient(exc) or attempt == MAX_ATTEMPTS:
                    raise
                backoff = min(30.0, 2 ** attempt) + random.uniform(0, 1.0)
                logger.warning(
                    "LLM attempt %d/%d failed (%s: %s); retrying in %.1fs",
                    attempt, MAX_ATTEMPTS, type(exc).__name__, str(exc)[:160], backoff,
                )
                await asyncio.sleep(backoff)
        raise last_exc if last_exc else RuntimeError("LLM call failed")


# ------------------------------------------------------------------- processing
class Pipeline:
    def __init__(
        self,
        pool: asyncpg.Pool,
        llm: LLMClient,
        masters: MasterData,
        designation_index: DesignationIndex,
        enrichment: EnrichmentStore,
        genai_client: Any,
        dry_run: bool = False,
        api_mode: bool = False,
        run_dir: Optional[Path] = None,
        pdf_summarizer: Optional[PdfSummarizer] = None,
        lang_map: Optional[Dict[str, str]] = None,
        dump_prompts_dir: Optional[Path] = None,
    ) -> None:
        self.pdf_summarizer = pdf_summarizer
        self.lang_map = lang_map or {}
        self.dump_prompts_dir = dump_prompts_dir
        if dump_prompts_dir:
            dump_prompts_dir.mkdir(parents=True, exist_ok=True)
        self.pool = pool
        self.llm = llm
        self.masters = masters
        self.designations = designation_index
        self.enrichment = enrichment
        self.genai_client = genai_client
        self.dry_run = dry_run
        self.api_mode = api_mode
        self.stats = {
            "succeeded": 0, "failed": 0, "with_issues": 0, "skipped": 0,
            "prompt_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
            # Billed as output by thinking models, and easy to miss: it runs
            # 3.9k-4.6k per course, more than the visible answer itself.
            "thinking_tokens": 0,
            "insufficient_content": 0,
            "pdf_summary_calls": 0,
            "pdf_summary_cache_hits": 0,
        }
        self.latencies: List[float] = []
        self.outcomes: List[Dict[str, Any]] = []
        self._outcomes_lock = asyncio.Lock()
        # --run-dir: this run's records, written per course as each one commits
        # rather than exported in bulk at the end. A killed run therefore still
        # leaves every course it finished on disk, and only the courses it
        # actually generated -- never the rest of the content set.
        self.run_dir = run_dir
        self._run_dir_lock = asyncio.Lock()
        if self.run_dir:
            (self.run_dir / "courses").mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()
        # Position within the whole selection, so every course line carries
        # "[n/total]" and a tail -f shows how far along the run is.
        self.total_scope = 0
        self.completed = 0

    def _tick(self) -> str:
        self.completed += 1
        if self.total_scope:
            return f"[{self.completed}/{self.total_scope}]"
        return f"[{self.completed}]"

    async def _record(self, row: Dict[str, Any]) -> None:
        async with self._outcomes_lock:
            self.outcomes.append(row)

    async def _write_run_files(
        self, course_id: str, record: Dict[str, Any], meta: Dict[str, Any]
    ) -> None:
        """
        Persist one finished course into --run-dir, immediately.

        Mirrors the shape tools/export.py produces so the folder is readable by
        the same consumers: one pretty JSON per course, plus a records.jsonl
        line carrying the same _meta block the exporter adds. The append is
        serialised -- several courses finish concurrently and interleaved writes
        would corrupt the line-delimited file.
        """
        if not self.run_dir:
            return
        payload = json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True)
        line = json.dumps({**record, "_meta": meta}, ensure_ascii=False)
        async with self._run_dir_lock:
            await asyncio.to_thread(
                (self.run_dir / "courses" / f"{course_id}.json").write_text,
                payload, encoding="utf-8",
            )

            def _append() -> None:
                with (self.run_dir / "records.jsonl").open(
                    "a", encoding="utf-8", newline="\n"
                ) as fh:
                    fh.write(line + "\n")

            await asyncio.to_thread(_append)

    async def _finish_insufficient(
        self,
        course_id: str,
        course_dir: Path,
        facts: Dict[str, Any],
        tier: str,
        declared_area: Optional[str],
        original_raw: str,
        content: Dict[str, Any],
        start: float,
    ) -> bool:
        """No transcript and no PDF: write the portal-only record, skip the model."""
        reason = (
            "No transcript and no PDF content available, so nothing was generated; "
            "only portal values (title, description, duration) and language are recorded."
        )
        record = build_portal_only_record(
            course_id=course_id, facts=facts, evidence_tier=tier, reason=reason
        )
        record["ReferenceResources"] = {
            "AssignmentsAndPracticeLinks": [],
            "ExtendedLearning": list(content.get("reference_links") or []),
        }
        issues = record["explain"]["validation_issues"]
        self.stats["insufficient_content"] += 1
        status = "insufficient_content"
        logger.info(
            "%s %s skipped: %s (tier=%s, language=%s via %s)",
            self._tick(), course_id, status, tier, record.get("Language"),
            facts.get("language_source"),
        )
        if not self.dry_run:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        SAVE_RESULT_SQL,
                        course_id,
                        json.dumps(record, sort_keys=True, ensure_ascii=False),
                        str(course_dir),
                        None,
                        LLM_PROMPT_VERSION,
                        time.time() - start,
                        json.dumps({}),
                        original_raw if original_raw != "null" else None,
                        status,
                        json.dumps(issues, ensure_ascii=False),
                        tier,
                        declared_area,
                        CONTENT_SET,
                    )
                    await conn.execute(MARK_DONE_SQL, course_id)
            await self._write_run_files(course_id, record, {
                "course_id": course_id,
                "status": status,
                "evidence_tier": tier,
                "declared_competency_area": declared_area,
                "validation_issues": issues,
                "content_set": CONTENT_SET,
                "model": None,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            })
        await self._record({
            "course_id": course_id,
            "status": status,
            "evidence_tier": tier,
            "declared_area": declared_area,
            "language": record.get("Language"),
            "language_source": facts.get("language_source"),
            "transcript_chars": content.get("transcript_chars"),
            "pdf_count": content.get("pdf_count"),
            "seconds": round(time.time() - start, 1),
        })
        return True

    async def process(self, course_id: str, course_dir: Path) -> bool:
        start = time.time()
        original_raw = "null"
        declared_area: Optional[str] = None
        tier = "unknown"
        try:
            if self.api_mode:
                course_dir = await api_fetcher.materialize_course(course_id)
            if not course_dir.is_dir():
                raise FileNotFoundError(f"course folder not found: {course_dir}")

            folder_meta, original_raw, declared_area = course_io.load_course_metadata(
                course_dir
            )
            content = await course_io.extract_course_content(course_dir)
            tier = content["evidence_tier"]

            scorm_flag = bool((json.loads(original_raw) or {}).get("scorm")) if (
                original_raw and original_raw != "null"
            ) else False

            facts = self.enrichment.facts_for(course_id, folder_meta, content)

            # ---- language: transcription source language of the videos -------
            language, all_languages, language_source = lang_mod.resolve_course_language(
                content.get("module_ids") or [],
                self.lang_map,
                api_langs={
                    k: lang_mod.first_language(v)
                    for k, v in (content.get("module_source_langs") or {}).items()
                },
                portal_language=facts.get("portal_language"),
                db_language=facts.get("db_language"),
            )
            facts["language"] = language
            facts["all_languages"] = all_languages
            facts["language_source"] = language_source

            # ---- no transcript and no PDF: portal-only record, no model call --
            if tier in SKIP_TIERS:
                return await self._finish_insufficient(
                    course_id, course_dir, facts, tier, declared_area, original_raw,
                    content, start,
                )

            # ---- PDF chunk summaries -------------------------------------------
            pdf_summaries = ""
            if content.get("pdf_documents"):
                if self.dry_run or not self.pdf_summarizer:
                    pdf_summaries = (
                        f"[dry run: {len(content['pdf_documents'])} PDF document(s) "
                        "would be summarised here]"
                    )
                else:
                    pdf_summaries, s_usage = await self.pdf_summarizer.summarise_documents(
                        course_id, content["pdf_documents"]
                    )
                    self.stats["pdf_summary_calls"] += s_usage.get("calls", 0)
                    self.stats["pdf_summary_cache_hits"] += s_usage.get("cache_hits", 0)
                    self.stats["prompt_tokens"] += s_usage.get("prompt_tokens", 0)
                    self.stats["output_tokens"] += s_usage.get("output_tokens", 0)
                    self.stats["thinking_tokens"] += s_usage.get("thinking_tokens", 0)

            query = build_content_retrieval_query(content["transcript"], pdf_summaries)
            candidates = await self.designations.shortlist(
                self.genai_client, query, k=DESIGNATION_CANDIDATES
            )

            section = P.build_course_section(
                transcript=content["transcript"],
                pdf_summaries=pdf_summaries,
                designation_candidates=candidates,
                evidence_tier=tier,
                output_language=language,
                duration_seconds=facts.get("duration_seconds"),
                transcript_char_limit=PROMPT_TRANSCRIPT_CHARS,
                pdf_char_limit=PROMPT_PDF_SUMMARY_CHARS,
            )
            if self.dump_prompts_dir:
                (self.dump_prompts_dir / f"{course_id}.txt").write_text(
                    section, encoding="utf-8"
                )

            if self.dry_run:
                logger.info(
                    "[dry-run] %s tier=%s language=%s (%s) transcript=%d pdf_docs=%d "
                    "candidates=%d prompt=%d chars",
                    course_id, tier, language, language_source,
                    content["transcript_chars"], len(content.get("pdf_documents") or []),
                    len(candidates), len(self.llm.static_prefix) + len(section),
                )
                self.stats["skipped"] += 1
                await self._record({
                    "course_id": course_id,
                    "status": "dry_run",
                    "evidence_tier": tier,
                    "declared_area": declared_area,
                    "language": language,
                    "language_source": language_source,
                    "transcript_chars": content["transcript_chars"],
                    "pdf_count": content["pdf_count"],
                    "seconds": round(time.time() - start, 1),
                })
                return True

            # PDFs reach the model as summaries now, never as attachments.
            raw_record, usage = await self.llm.generate(section)

            record, issues = validate_and_repair(
                raw_record,
                course_id=course_id,
                masters=self.masters,
                designation_index=self.designations,
                authoritative_facts=facts,
                evidence_tier=tier,
                scorm_flag=scorm_flag,
            )
            # Reference links found on disk are authoritative; the model must not
            # invent URLs, so seed them here rather than asking for them.
            if content["reference_links"]:
                refs = record.setdefault("ReferenceResources", {})
                existing = set(refs.get("ExtendedLearning") or [])
                refs["ExtendedLearning"] = list(existing) + [
                    u for u in content["reference_links"] if u not in existing
                ]

            record["explain"]["declared_competency_area_for_audit"] = declared_area
            record["explain"]["agrees_with_declared_area"] = (
                declared_area is not None
                and not declared_area.startswith("MIXED")
                and record.get("PrimaryCompetencyArea", {}).get("name") == declared_area
            )

            duration = time.time() - start
            self.stats["succeeded"] += 1
            if issues:
                self.stats["with_issues"] += 1
            self.stats["prompt_tokens"] += usage.get("prompt_token_count") or 0
            self.stats["output_tokens"] += usage.get("candidates_token_count") or 0
            self.stats["cached_tokens"] += usage.get("cached_content_token_count") or 0
            self.stats["thinking_tokens"] += usage.get("thoughts_token_count") or 0
            self.latencies.append(duration)

            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        SAVE_RESULT_SQL,
                        course_id,
                        json.dumps(record, sort_keys=True, ensure_ascii=False),
                        str(course_dir),
                        GENAI_MODEL_NAME,
                        LLM_PROMPT_VERSION,
                        duration,
                        json.dumps(usage),
                        original_raw if original_raw != "null" else None,
                        "success_with_issues" if issues else "success",
                        json.dumps(issues, ensure_ascii=False),
                        tier,
                        declared_area,
                        CONTENT_SET,
                    )
                    await conn.execute(MARK_DONE_SQL, course_id)

            # After the commit, so the folder never advertises a course the
            # database does not have.
            await self._write_run_files(course_id, record, {
                # Explicit, so a records.jsonl identifies its courses even if a
                # record ever loses its own Do_ID.
                "course_id": course_id,
                "evidence_tier": tier,
                "declared_competency_area": declared_area,
                "validation_issues": issues,
                "content_set": CONTENT_SET,
                "model": GENAI_MODEL_NAME,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            })

            logger.info(
                "%s %s ok in %.1fs tier=%s area=%s roles=%d issues=%d "
                "tokens(in/cached/out)=%s/%s/%s",
                self._tick(), course_id, duration, tier,
                record.get("PrimaryCompetencyArea", {}).get("name"),
                len(record.get("Targetroles") or []), len(issues),
                usage.get("prompt_token_count"),
                usage.get("cached_content_token_count") or 0,
                usage.get("candidates_token_count"),
            )
            # At INFO, not DEBUG: these are the audit record for the run and the
            # signal for systemic prompt problems. Also stored in
            # course_metadata_regenerated.validation_issues.
            for issue in issues:
                logger.info("  %s issue: %s", course_id, issue)

            rubric = record.get("RubricScoring") or {}
            await self._record({
                "course_id": course_id,
                "status": "success_with_issues" if issues else "success",
                "evidence_tier": tier,
                "primary_area": record.get("PrimaryCompetencyArea", {}).get("name"),
                "declared_area": declared_area,
                "agrees_with_declared": record["explain"].get("agrees_with_declared_area"),
                "functional": len(record.get("FunctionalCompetencies") or []),
                "behavioural": len(record.get(P.BEHAVIOURAL_KEY) or []),
                "domain": len(record.get("DomainCompetencies") or []),
                "targetroles": len(record.get("Targetroles") or []),
                "total_score": rubric.get("TotalScore"),
                "classification": rubric.get("Classification"),
                "transcript_chars": content["transcript_chars"],
                "pdf_count": content["pdf_count"],
                "pdf_sent_natively": len(content.get("pdf_native") or []),
                "language": record.get("Language"),
                "language_source": language_source,
                "issue_count": len(issues),
                "issues": " | ".join(issues)[:2000],
                "prompt_tokens": usage.get("prompt_token_count"),
                "cached_tokens": usage.get("cached_content_token_count") or 0,
                "output_tokens": usage.get("candidates_token_count"),
                "seconds": round(duration, 1),
            })
            return True

        except Exception as exc:
            self.stats["failed"] += 1
            message = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "%s %s FAILED (tier=%s): %s", self._tick(), course_id, tier, message[:300]
            )
            await self._record({
                "course_id": course_id,
                "status": "failed",
                "evidence_tier": tier,
                "declared_area": declared_area,
                "seconds": round(time.time() - start, 1),
                # Full traceback is in the log; the CSV/DB keep a bounded copy.
                "error": message[:1000],
            })
            if not self.dry_run:
                try:
                    async with self.pool.acquire() as conn:
                        async with conn.transaction():
                            await conn.execute(
                                MARK_FAILED_SQL, course_id, message[:4000], MAX_ATTEMPTS
                            )
                            await conn.execute(LOG_ERROR_SQL, course_id, message[:4000])
                except Exception:
                    logger.exception("could not record failure for %s", course_id)
            return False


# ----------------------------------------------------------------- work sourcing
def discover_courses() -> List[Tuple[str, Path]]:
    """
    Course id -> folder, preferring the deduplicated manifest from
    tools/inventory.py. Falls back to scanning COURSES_BASE_PATH.
    """
    if COURSE_MANIFEST and Path(COURSE_MANIFEST).is_file():
        out: List[Tuple[str, Path]] = []
        with open(COURSE_MANIFEST, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                out.append((rec["course_id"], Path(rec["path"])))
        logger.info("discovered %d courses from manifest %s", len(out), COURSE_MANIFEST)
        return out

    if not COURSES_BASE_PATH.exists():
        logger.error("COURSES_BASE_PATH does not exist: %s", COURSES_BASE_PATH)
        return []
    found = [(d.name, d) for d in course_io.find_course_dirs(COURSES_BASE_PATH)]
    logger.info("discovered %d courses under %s", len(found), COURSES_BASE_PATH)
    return found


async def populate_checkpoints(pool: asyncpg.Pool, courses: List[Tuple[str, Path]]) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            for course_id, path in courses:
                await conn.execute(
                    UPSERT_CHECKPOINT_SQL, course_id, str(path),
                    CONTENT_SET, LLM_PROMPT_VERSION,
                )
    logger.info("checkpoint table populated for %d courses", len(courses))


async def setup_database(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        for stmt in DDL_STATEMENTS:
            await conn.execute(stmt)
        await conn.execute(MIGRATE_PK_SQL)
    logger.info("database schema ready (v3.5 columns + version-scoped primary key)")


async def run(args: argparse.Namespace) -> None:
    kcm_json = load_json_file(KCM_PATH)
    sgos_json = load_json_file(SGOS_PATH)

    masters = MasterData(kcm_json, sgos_json)
    logger.info(
        "masters: %d KCM pairs, %d SGOS sectors",
        len(masters.kcm_pairs), len(masters.sgos),
    )

    if DESIGNATION_INDEX_PATH.is_file():
        designation_index = DesignationIndex.load(DESIGNATION_INDEX_PATH)
        logger.info("designation index: %d entries (vector)", len(designation_index.ids))
    else:
        designation_index = DesignationIndex.lexical_only(DESIGNATIONS_PATH)
        logger.warning(
            "no embedding index at %s; using lexical-only retrieval "
            "(build it with tools/build_designation_index.py)",
            DESIGNATION_INDEX_PATH,
        )

    if not GOOGLE_PROJECT_ID:
        raise RuntimeError("GOOGLE_PROJECT_ID is not set")
    genai_client = genai.Client(
        project=GOOGLE_PROJECT_ID, location=GOOGLE_LOCATION, vertexai=True
    )
    logger.info("model %s @ %s", GENAI_MODEL_NAME, GOOGLE_LOCATION)

    static_prefix = P.build_static_prefix(kcm_json, sgos_json)
    logger.info("static prompt prefix: %d chars (cacheable)", len(static_prefix))
    llm = LLMClient(genai_client, static_prefix)

    enrichment = await EnrichmentStore.load(ENRICHMENT_DSN)
    lang_map = lang_mod.load_language_csv(args.language_csv or LANGUAGE_CSV)
    api_fetcher.REFRESH = bool(args.refresh_api)
    pdf_summarizer = PdfSummarizer(
        genai_client, PDF_SUMMARY_MODEL,
        is_transient=_is_transient,
        max_attempts=MAX_ATTEMPTS,
        timeout_seconds=LLM_TIMEOUT_SECONDS,
        temperature=LLM_TEMPERATURE,
    )
    logger.info(
        "v3.6: %d fields generated by the model, field config %s; PDF summaries via %s",
        len(P.METADATA_SCHEMA["properties"]), P.FIELD_CONFIG_PATH, PDF_SUMMARY_MODEL,
    )

    concurrency = max(1, args.batch_size)
    pool = await asyncpg.create_pool(
        dsn=DB_DSN, min_size=1, max_size=max(2, concurrency)
    )
    try:
        await setup_database(pool)

        dry_run = not args.execute
        if dry_run:
            logger.warning(
                "DRY RUN: no AI calls and no writes. Re-run with --execute to apply."
            )

        pipeline = Pipeline(
            pool, llm, masters, designation_index, enrichment, genai_client,
            dry_run=dry_run, api_mode=args.api,
            # A dry run generates nothing, so it must not create the folder or
            # append to a previous run's records.jsonl.
            run_dir=None if dry_run else args.run_dir,
            pdf_summarizer=pdf_summarizer,
            lang_map=lang_map,
            dump_prompts_dir=args.dump_prompts,
        )

        if args.sample:
            local = discover_courses()
            picked = pick_sample(
                local, args.sample, seed=args.sample_seed, include=args.sample_include or []
            )
            if not picked:
                logger.error("--sample: no courses found to sample from")
                return
            sample_dir = args.run_dir or LOG_DIR
            sample_dir.mkdir(parents=True, exist_ok=True)
            ids_file = sample_dir / "sample_ids.txt"
            ids_file.write_text("\n".join(c for c, _, _ in picked) + "\n", encoding="utf-8")
            for cid, _, bucket in picked:
                logger.info("  sample: %s  [%s]", cid, bucket)
            logger.info("--sample: %d course(s) written to %s", len(picked), ids_file)
            args.course_id = [c for c, _, _ in picked]
            if args.api:
                courses = [(cid, api_fetcher.API_CACHE_PATH / cid) for cid in args.course_id]
            else:
                courses = [(c, p) for c, p, _ in picked]
        elif args.api:
            # No folder to scan: content is fetched (and cached) on demand per
            # course_id, so the queue is exactly the ids the caller supplied.
            courses = [(cid, api_fetcher.API_CACHE_PATH / cid) for cid in args.course_id]
        elif args.course_id:
            courses = [(c, p) for c, p in discover_courses() if c in set(args.course_id)]
            if not courses:
                logger.error("no matching course folders for %s", args.course_id)
                return
        else:
            courses = discover_courses()
            if args.tier:
                courses = _filter_by_tier(courses, args.tier)

        if args.force and not dry_run:
            await reset_courses(pool, [cid for cid, _ in courses])

        if not dry_run:
            await populate_checkpoints(pool, courses)
            await reclaim_stale(pool)
            done = await already_done(pool, [cid for cid, _ in courses])
            if done:
                logger.info(
                    "%d of %d course(s) already done for %s; skipping "
                    "(use --force to redo)",
                    len(done), len(courses), LLM_PROMPT_VERSION,
                )
            courses = [(c, p) for c, p in courses if c not in done]

        if args.limit:
            courses = courses[: args.limit]
        pipeline.total_scope = len(courses)
        logger.info("%d course(s) to process, concurrency=%d", len(courses), concurrency)
        if not dry_run:
            await log_progress(pool, 0)

        if dry_run:
            # Walk the list directly: the DB queue is claim-based, and a dry run
            # neither claims nor completes, so draining would re-serve forever.
            sem = asyncio.Semaphore(concurrency)

            async def _one(cid: str, path: Path) -> bool:
                async with sem:
                    return await pipeline.process(cid, path)

            await asyncio.gather(
                *(_one(cid, path) for cid, path in courses), return_exceptions=True
            )
        else:
            await drain(
                pool, pipeline,
                serve=args.serve,
                limit=args.limit,
                only_ids=[cid for cid, _ in courses],
                concurrency=concurrency,
            )

        if pipeline.run_dir:
            _compact_run_jsonl(pipeline.run_dir)

        remaining = await count_outstanding(pool) if not dry_run else None
        _report(
            pipeline.stats,
            pipeline.latencies,
            wall_seconds=time.time() - pipeline.started_at,
            concurrency=concurrency,
            remaining=remaining,
        )
        _write_outcome_csv(pipeline.outcomes, args.out)
    finally:
        await pool.close()


ALREADY_DONE_SQL = """
SELECT c.course_id
  FROM course_processing_checkpoint c
  JOIN course_metadata_regenerated r
    ON r.course_id = c.course_id AND r.llm_prompt_version = $2
 WHERE c.course_id = ANY($1) AND c.status = 'done'
"""

RESET_COURSES_SQL = """
UPDATE course_processing_checkpoint
   SET status = 'pending', attempts = 0, last_error = NULL, last_updated = now()
 WHERE course_id = ANY($1)
"""


COUNT_OUTSTANDING_SQL = """
SELECT count(*) FROM course_processing_checkpoint
 WHERE content_set = $1 AND status IN ('pending', 'failed') AND attempts < $2
"""

# Overall position, so a long run reports "done X of Y" rather than only a
# count of what this invocation happened to touch.
PROGRESS_SQL = """
SELECT count(*)                                            AS total,
       count(*) FILTER (WHERE status = 'done')             AS done,
       count(*) FILTER (WHERE status = 'failed')           AS failed,
       count(*) FILTER (WHERE status = 'dead')             AS dead,
       count(*) FILTER (WHERE status = 'in_progress')      AS in_progress,
       count(*) FILTER (WHERE status = 'pending')          AS pending
  FROM course_processing_checkpoint
 WHERE content_set = $1
"""


async def log_progress(pool: asyncpg.Pool, this_run: int, per_hour: float = 0.0) -> None:
    async with pool.acquire() as conn:
        r = await conn.fetchrow(PROGRESS_SQL, CONTENT_SET)
    if not r or not r["total"]:
        return
    done, total = int(r["done"]), int(r["total"])
    pct = done / total * 100 if total else 0.0
    remaining = total - done - int(r["dead"])
    eta = ""
    if per_hour > 0 and remaining > 0:
        eta = f", eta {_fmt_duration(remaining / per_hour * 3600)} at {per_hour:.0f}/hr"
    logger.info(
        "PROGRESS %s/%s done (%.1f%%) | %d this run | pending %d, failed %d, dead %d%s",
        f"{done:,}", f"{total:,}", pct, this_run,
        int(r["pending"]), int(r["failed"]), int(r["dead"]), eta,
    )


async def count_outstanding(pool: asyncpg.Pool) -> int:
    """Courses still to do for this content set — the basis for the projection."""
    async with pool.acquire() as conn:
        return int(await conn.fetchval(COUNT_OUTSTANDING_SQL, CONTENT_SET, MAX_ATTEMPTS) or 0)


async def already_done(pool: asyncpg.Pool, ids: List[str]) -> set:
    """Courses with a stored result for this prompt version. Re-runs skip these."""
    if not ids:
        return set()
    async with pool.acquire() as conn:
        rows = await conn.fetch(ALREADY_DONE_SQL, ids, LLM_PROMPT_VERSION)
    return {r["course_id"] for r in rows}


async def reset_courses(pool: asyncpg.Pool, ids: List[str]) -> None:
    if not ids:
        return
    async with pool.acquire() as conn:
        await conn.execute(RESET_COURSES_SQL, ids)
    logger.warning("--force: reset %d course(s) back to pending", len(ids))


def _write_outcome_csv(outcomes: List[Dict[str, Any]], out: Optional[Path]) -> None:
    """One row per course attempted, per the bulk_scripts convention."""
    if not outcomes:
        return
    path = out or LOG_DIR / (
        f"meta_gen_{P.PROMPT_VERSION}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "course_id", "status", "evidence_tier", "primary_area",
        "declared_area", "agrees_with_declared", "functional", "behavioural",
        "domain", "targetroles", "total_score", "classification",
        "language", "language_source",
        "transcript_chars", "pdf_count", "pdf_sent_natively", "issue_count", "issues",
        "prompt_tokens", "cached_tokens", "output_tokens", "seconds", "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in outcomes:
            writer.writerow(row)
    logger.info("outcome CSV -> %s (%d rows)", path, len(outcomes))


def _compact_run_jsonl(run_dir: Path) -> None:
    """
    Collapse records.jsonl to one line per course, keeping the newest.

    The file is appended to as courses commit, and a --run-dir accumulates
    across runs, so regenerating a course (--force, or a later run of the same
    id) leaves an older line behind it. Compacting at the end keeps the file
    authoritative without giving up the crash-safe append during the run.
    """
    path = run_dir / "records.jsonl"
    if not path.is_file():
        return
    latest: Dict[str, str] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # A line torn by a hard kill mid-write; drop it, the course is
                # still in the database and its courses/<id>.json is intact.
                logger.warning("records.jsonl: skipping unparseable line")
                continue
            cid = (rec.get("_meta") or {}).get("course_id") or rec.get("Do_ID")
            if cid:
                latest[str(cid)] = line.rstrip("\n")
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(latest.values()) + "\n", encoding="utf-8")
    tmp.replace(path)
    logger.info("run dir: %d course(s) in %s", len(latest), path)


# --sample: share of the sample per bucket. "scorm" is any SCORM course; the
# rest are evidence tiers of non-SCORM courses. Shortfalls go to "transcript".
SAMPLE_SHARES = [
    ("transcript", 0.5), ("pdf_only", 0.2), ("thin_transcript", 0.1),
    ("scorm", 0.1), ("metadata_only", 0.1),
]


def pick_sample(
    courses: List[Tuple[str, Path]], n: int, seed: int = 42, include: Optional[List[str]] = None
) -> List[Tuple[str, Path, str]]:
    """
    n courses spread across evidence tiers (unlike --limit, which takes the
    first n). Tiers come from course_io.quick_tier, a cheap file-level estimate,
    so only as many folders are inspected as it takes to fill the buckets.
    Courses named in `include` are always in the sample.
    """
    by_id = dict(courses)
    picked: List[Tuple[str, Path, str]] = []
    for cid in include or []:
        if cid in by_id:
            picked.append((cid, by_id[cid], "included"))
        else:
            logger.warning("--sample-include %s: not found locally; adding by id", cid)
            picked.append((cid, COURSES_BASE_PATH / cid, "included"))
    remaining = max(0, n - len(picked))
    quotas: Dict[str, int] = {}
    for bucket, share in SAMPLE_SHARES:
        quotas[bucket] = int(round(remaining * share))
    quotas["transcript"] += remaining - sum(quotas.values())

    pool = [(c, p) for c, p in courses if c not in {x[0] for x in picked}]
    random.Random(seed).shuffle(pool)
    leftovers: List[Tuple[str, Path, str]] = []
    for cid, path in pool:
        if sum(quotas.values()) <= 0:
            break
        tier, scorm = course_io.quick_tier(path)
        bucket = "scorm" if scorm else tier
        if quotas.get(bucket, 0) > 0:
            quotas[bucket] -= 1
            picked.append((cid, path, bucket))
        elif bucket in ("transcript", "pdf_only", "thin_transcript") and len(leftovers) < n:
            leftovers.append((cid, path, bucket))
    # Buckets that could not be filled: top up with other content courses.
    short = sum(quotas.values())
    picked.extend(leftovers[:short])
    return picked


def _filter_by_tier(courses: List[Tuple[str, Path]], tiers: List[str]) -> List[Tuple[str, Path]]:
    if not (COURSE_MANIFEST and Path(COURSE_MANIFEST).is_file()):
        logger.warning("--tier requires COURSE_MANIFEST; ignoring")
        return courses
    wanted = set(tiers)
    keep = set()
    with open(COURSE_MANIFEST, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rec = json.loads(line)
                if rec.get("tier") in wanted:
                    keep.add(rec["course_id"])
    filtered = [(c, p) for c, p in courses if c in keep]
    logger.info("tier filter %s -> %d courses", sorted(wanted), len(filtered))
    return filtered


async def reclaim_stale(pool: asyncpg.Pool) -> None:
    """Return rows stranded in 'in_progress' by a killed worker to the queue."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(RECLAIM_STALE_SQL, CONTENT_SET, str(STALE_CLAIM_MINUTES))
    if rows:
        logger.warning(
            "reclaimed %d course(s) stranded in 'in_progress' for >%d min: %s",
            len(rows), STALE_CLAIM_MINUTES,
            ", ".join(r["course_id"] for r in rows[:5])
            + (" ..." if len(rows) > 5 else ""),
        )


async def release_claims(pool: asyncpg.Pool, course_ids: set[str]) -> None:
    """
    Put claims this run never processed back on the queue, immediately.

    Without this the rows wait out STALE_CLAIM_MINUTES before reclaim_stale
    picks them up, and an interrupted run re-started inside that window keeps
    selecting them, keeps failing to claim them, and does less work than
    --limit asked for -- with nothing in the log to say why.
    """
    if not course_ids:
        return
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(RELEASE_CLAIM_SQL, sorted(course_ids))
    except Exception as exc:
        # Best effort: the pool may already be closing, or the loop tearing
        # down under a hard cancellation. reclaim_stale is the backstop.
        logger.warning(
            "could not release %d unprocessed claim(s) (%s); "
            "they will be reclaimed after %d min",
            len(course_ids), type(exc).__name__, STALE_CLAIM_MINUTES,
        )
        return
    if rows:
        logger.info(
            "released %d unprocessed claim(s) back to pending: %s",
            len(rows),
            ", ".join(r["course_id"] for r in rows[:5])
            + (" ..." if len(rows) > 5 else ""),
        )


async def drain(
    pool: asyncpg.Pool,
    pipeline: Pipeline,
    serve: bool,
    limit: Optional[int],
    only_ids: Optional[List[str]] = None,
    concurrency: int = MAX_CONCURRENCY,
) -> None:
    """
    Claim and process pending work until drained (or forever with --serve).

    only_ids scopes the queue to a specific selection. Without it, --tier/--limit
    would appear to work but drain would still pick up any other pending row for
    this content set, including leftovers from earlier runs.
    """
    processed = 0
    idle_rounds = 0
    semaphore = asyncio.Semaphore(concurrency)
    # Claims this run holds but has not resolved to done/failed. A claim is a
    # lease, and dropping one on the floor is expensive: the row sits in
    # 'in_progress' until STALE_CLAIM_MINUTES elapses, invisible to the pending
    # query but still counted by --limit, so every run in between quietly does
    # less work than asked. Whatever is still in here when drain exits goes back.
    outstanding: set[str] = set()

    async def guarded(cid: str, path: Path) -> bool:
        async with semaphore:
            # Claimed before the semaphore was free, and the run is now winding
            # down: leave it in `outstanding` so the claim is handed back.
            if _shutdown.is_set():
                return False
            result = await pipeline.process(cid, path)
            # Deliberately not a finally: process() marks the row done or
            # failed on every path it *returns* from, so only then is the lease
            # no longer ours. If it raises -- a cancellation mid-call -- the id
            # stays in the set and the claim goes back to the queue.
            outstanding.discard(cid)
            return result

    try:
        while not _shutdown.is_set():
            batch_size = WORKER_BATCH_SIZE
            if limit:
                remaining = limit - processed
                if remaining <= 0:
                    break
                batch_size = min(batch_size, remaining)

            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    SELECT_PENDING_SQL, batch_size, MAX_ATTEMPTS, CONTENT_SET, only_ids
                )

            if not rows:
                if not serve:
                    logger.info("no pending courses remain; draining complete")
                    break
                idle_rounds += 1
                logger.info("idle (round %d); sleeping 30s", idle_rounds)
                try:
                    await asyncio.wait_for(_shutdown.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                continue

            idle_rounds = 0
            tasks = []
            for row in rows:
                cid = row["course_id"]
                source_folder = row["source_folder"]
                if not pipeline.dry_run:
                    # A dry run must not claim work: claiming increments attempts and
                    # leaves rows stuck in 'in_progress', since nothing ever completes.
                    async with pool.acquire() as conn:
                        claimed = await conn.fetchrow(CLAIM_COURSE_SQL, cid)
                    if not claimed:
                        continue
                    source_folder = claimed["source_folder"]
                    outstanding.add(cid)
                folder = Path(source_folder) if source_folder else COURSES_BASE_PATH / cid
                tasks.append(guarded(cid, folder))

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                processed += len(tasks)
                elapsed = time.time() - pipeline.started_at
                per_hour = (
                    pipeline.stats["succeeded"] / (elapsed / 3600) if elapsed > 0 else 0.0
                )
                await log_progress(pool, processed, per_hour)
    finally:
        await release_claims(pool, outstanding)


def token_cost(prompt_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    """
    USD for one or more calls.

    `output` must already include thinking tokens: thinking is billed at the
    output rate, and on this workload it exceeds the visible answer.
    Cached prompt tokens are billed at the cached rate instead of the full one.
    """
    uncached = max(0, prompt_tokens - cached_tokens)
    return (
        uncached / 1_000_000 * PRICE_INPUT_PER_M
        + cached_tokens / 1_000_000 * PRICE_CACHED_INPUT_PER_M
        + output_tokens / 1_000_000 * PRICE_OUTPUT_PER_M
    )


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _report(
    stats: Dict[str, Any],
    latencies: List[float],
    wall_seconds: float,
    concurrency: int,
    remaining: Optional[int] = None,
) -> None:
    done = stats["succeeded"]
    billed_output = stats["output_tokens"] + stats["thinking_tokens"]

    logger.info("=" * 66)
    logger.info("RUN SUMMARY (%s, model %s)", LLM_PROMPT_VERSION, GENAI_MODEL_NAME)
    logger.info("  succeeded / with issues / failed : %d / %d / %d",
                done, stats["with_issues"], stats["failed"])
    if stats["skipped"]:
        logger.info("  dry-run previews                 : %d", stats["skipped"])
    if stats.get("insufficient_content"):
        logger.info("  skipped, no transcript/PDF       : %d (portal-only record)",
                    stats["insufficient_content"])
    if stats.get("pdf_summary_calls") or stats.get("pdf_summary_cache_hits"):
        logger.info("  PDF summary calls / cache hits   : %d / %d",
                    stats["pdf_summary_calls"], stats["pdf_summary_cache_hits"])

    if not done:
        logger.info("=" * 66)
        return

    ordered = sorted(latencies)
    mean = sum(ordered) / len(ordered)
    median = ordered[len(ordered) // 2]
    p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]

    # Per-course latency is what one course costs in time; throughput is what the
    # run achieves with `concurrency` of them overlapping. Only throughput
    # predicts wall-clock, and the two differ by roughly the concurrency factor.
    per_hour = done / (wall_seconds / 3600) if wall_seconds > 0 else 0.0

    logger.info("  -- latency (per course) --")
    logger.info("    mean / median / p90            : %.1fs / %.1fs / %.1fs", mean, median, p90)
    logger.info("    fastest / slowest              : %.1fs / %.1fs", ordered[0], ordered[-1])
    logger.info("  -- throughput (concurrency %d) --", concurrency)
    logger.info("    wall clock                     : %s", _fmt_duration(wall_seconds))
    logger.info("    courses/hour                   : %.0f", per_hour)
    logger.info("    effective seconds/course       : %.1fs", wall_seconds / done)

    logger.info("  -- tokens (mean per course) --")
    logger.info("    input                          : %s", f"{stats['prompt_tokens'] // done:,}")
    logger.info("    cached input                   : %s", f"{stats['cached_tokens'] // done:,}")
    logger.info("    output (answer)                : %s", f"{stats['output_tokens'] // done:,}")
    logger.info("    output (thinking, billed)      : %s", f"{stats['thinking_tokens'] // done:,}")

    cost = token_cost(stats["prompt_tokens"], billed_output, stats["cached_tokens"])
    logger.info("  -- cost @ $%.2f/$%.2f per 1M (cached $%.2f) --",
                PRICE_INPUT_PER_M, PRICE_OUTPUT_PER_M, PRICE_CACHED_INPUT_PER_M)
    logger.info("    this run                       : $%.2f", cost)
    logger.info("    mean per course                : $%.4f", cost / done)

    if remaining:
        eta = remaining / per_hour * 3600 if per_hour > 0 else 0
        logger.info("  -- projection for %s remaining courses --", f"{remaining:,}")
        logger.info("    at this throughput             : %s", _fmt_duration(eta))
        logger.info("    estimated cost                 : $%.2f", cost / done * remaining)
        if not stats["cached_tokens"]:
            # The static prefix is identical on every call, so if it were served
            # from cache the saving is the whole prefix at the cached rate.
            prefix = stats["prompt_tokens"] // done
            saving_per_course = token_cost(prefix, 0) - token_cost(prefix, 0, prefix)
            logger.info("    no cache hits observed; caching the static prefix")
            logger.info("    could save up to               : $%.2f", saving_per_course * remaining)
    logger.info("=" * 66)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    def handler() -> None:
        if not _shutdown.is_set():
            logger.warning("shutdown requested; finishing in-flight courses")
            _shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handler)
        except (NotImplementedError, RuntimeError):
            # Windows has no add_signal_handler, so Ctrl-C would raise
            # KeyboardInterrupt straight through the event loop: in-flight
            # courses are cancelled mid-call and their claims stranded, since
            # cleanup code never gets a turn. Route the signal into the loop
            # instead. Restoring the default handler first means a second
            # Ctrl-C still aborts hard, for a run that will not wind down.
            def route_to_loop(_signum: int, _frame: Any, _sig: int = sig) -> None:
                signal.signal(_sig, signal.SIG_DFL)
                loop.call_soon_threadsafe(handler)

            try:
                signal.signal(sig, route_to_loop)
            except (ValueError, OSError, AttributeError):
                pass


REQUIRED_ENV = ["DB_DSN", "GOOGLE_PROJECT_ID", "GOOGLE_APPLICATION_CREDENTIALS"]


def check_env(api_mode: bool = False) -> None:
    """Fail immediately naming what is missing, rather than part-way through a run."""
    required = REQUIRED_ENV + (["KARMAYOGI_API_KEY"] if api_mode else [])
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + "\nSet them in .env (see the iGOT COURSE METADATA REGENERATION section)."
        )
    creds = Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    if not creds.is_file():
        raise SystemExit(f"GOOGLE_APPLICATION_CREDENTIALS points at a missing file: {creds}")
    for label, path in (("KCM_PATH", KCM_PATH), ("SGOS_PATH", SGOS_PATH)):
        if not path.is_file():
            raise SystemExit(f"{label} points at a missing file: {path}")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Regenerate iGOT course metadata (v3.6, content-only input)"
    )
    ap.add_argument("--limit", type=int, help="process at most N courses")
    ap.add_argument("--course-id", action="append", help="process specific course id(s)")
    ap.add_argument(
        "--course-id-file", type=Path,
        help="text file of course ids, one per line; merged with --course-id",
    )
    ap.add_argument(
        "--api", action="store_true",
        help="fetch course content from the Learning API (api_fetcher.py) instead of "
             "COURSES_BASE_PATH. Requires KARMAYOGI_API_KEY and at least one "
             "--course-id / --course-id-file; fetched content is cached under "
             "API_CACHE_PATH so a course is never re-fetched.",
    )
    ap.add_argument(
        "--tier", action="append",
        choices=["transcript", "thin_transcript", "pdf_only", "links_only", "metadata_only"],
        help="restrict to courses in the given evidence tier(s)",
    )
    ap.add_argument(
        "--sample", type=int, metavar="N",
        help="process a sample of N courses spread across evidence tiers (about half "
             "transcript, plus pdf_only, thin_transcript, one SCORM and one with no "
             "content). The chosen ids are written to <run-dir or LOG_DIR>/sample_ids.txt "
             "so the same set can be re-run with --course-id-file.",
    )
    ap.add_argument("--sample-seed", type=int, default=42,
                    help="random seed for --sample (default 42), for a repeatable pick")
    ap.add_argument(
        "--sample-include", action="append", metavar="COURSE_ID",
        help="course id that must be in the --sample (repeatable)",
    )
    ap.add_argument(
        "--language-csv", type=Path,
        help="transcription source-language CSV (resource_id,timestamp,source_language); "
             "default LANGUAGE_CSV env or ../OneDrive_2026-11-06/transcription_soruce_lang.csv",
    )
    ap.add_argument(
        "--refresh-api", action="store_true",
        help="with --api: refetch courses already in API_CACHE_PATH (needed for courses "
             "cached before duration/language were saved)",
    )
    ap.add_argument(
        "--dump-prompts", type=Path, metavar="DIR",
        help="write each course's per-course prompt section to DIR/<id>.txt (works in "
             "dry runs too), to check exactly what the model is given",
    )
    ap.add_argument("--serve", action="store_true", help="stay resident and poll for work")
    ap.add_argument(
        "--execute", action="store_true",
        help="make real AI calls and DB writes. Without this the run is a dry run: "
             "prompts and shortlists are built, nothing is called or written.",
    )
    ap.add_argument(
        "--force", action="store_true",
        help="reprocess courses already marked done for this prompt version",
    )
    ap.add_argument(
        "--batch-size", type=int, default=MAX_CONCURRENCY,
        help=f"concurrent courses in flight (default {MAX_CONCURRENCY})",
    )
    ap.add_argument("--out", type=Path, help="outcome CSV path (default under logs/)")
    ap.add_argument(
        "--run-dir", type=Path,
        help="write this run's records here as each course commits: "
             "courses/<id>.json plus an appended records.jsonl. Only the courses "
             "this run generates are written, so an interrupted run still leaves "
             "a complete, self-consistent folder of what it finished.",
    )
    args = ap.parse_args()

    if args.course_id_file:
        file_ids = [
            line.strip() for line in args.course_id_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        seen = set(args.course_id or [])
        merged = list(args.course_id or [])
        for cid in file_ids:
            if cid not in seen:
                seen.add(cid)
                merged.append(cid)
        args.course_id = merged

    if args.api and not args.course_id and not args.sample:
        ap.error("--api requires --course-id, --course-id-file or --sample")

    return args


def main() -> None:
    args = parse_args()
    check_env(api_mode=args.api)

    async def _main() -> None:
        _install_signal_handlers(asyncio.get_running_loop())
        await run(args)

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("interrupted")
    except Exception as exc:
        logger.critical("fatal: %s: %s", type(exc).__name__, exc, exc_info=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
