"""
Course language from the transcription source language of its videos.

The portal's own language tag is often wrong (e.g. Hindi courses tagged
English). The transcription pipeline records the spoken language of every
video it transcribes:

  - per video, live: transcoder stats `default_lang` (saved by api_fetcher.py
    into each module's metadata.json as `source_language`), and
  - in bulk: the BigQuery export `transcription_soruce_lang.csv`
    (resource_id, timestamp, source_language), keyed by video resource id.

Rule agreed with the team (Nov 2026):
  1. one language per video: the FIRST language listed ("[Hindi,Sanskrit]" -> Hindi);
  2. the course language is the most common one across its videos (ties go to
     the first video in course order);
  3. if no video has a transcription language, fall back to the portal tag,
     then to course_metadata_v3.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("course-regenerator.language")

# Codes and spellings seen in the transcription table and the portal.
_CODES = {
    "en": "English", "eng": "English",
    "hi": "Hindi", "hin": "Hindi",
    "bn": "Bengali", "gu": "Gujarati", "kn": "Kannada", "ml": "Malayalam",
    "mr": "Marathi", "pa": "Punjabi", "ta": "Tamil", "te": "Telugu",
    "or": "Odia", "od": "Odia", "as": "Assamese", "ur": "Urdu",
    "sa": "Sanskrit", "ne": "Nepali", "ks": "Kashmiri", "kok": "Konkani",
    "mai": "Maithili", "sd": "Sindhi", "doi": "Dogri", "mni": "Manipuri",
    "sat": "Santali", "brx": "Bodo",
}
_SPELLING = {"oriya": "Odia", "bangla": "Bengali"}


def normalize_language(value: Optional[str]) -> Optional[str]:
    """'hi' / 'hindi' / ' Hindi ' -> 'Hindi'. Unknown names are title-cased."""
    if value is None:
        return None
    v = str(value).strip().strip("[]\"'").strip()
    if not v:
        return None
    low = v.lower()
    if low in _CODES:
        return _CODES[low]
    if low in _SPELLING:
        return _SPELLING[low]
    return v[:1].upper() + v[1:].lower()


def first_language(raw: Optional[str]) -> Optional[str]:
    """'[Hindi,Sanskrit]' -> 'Hindi'; '[en]' -> 'English'; '' -> None."""
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[,;|]", str(raw).strip().strip("[]"))
    for item in items:
        lang = normalize_language(item)
        if lang:
            return lang
    return None


def load_language_csv(path: Optional[Path]) -> Dict[str, str]:
    """
    resource_id -> language (first listed, normalised). For a resource_id that
    appears more than once, the row with the latest timestamp wins.
    """
    if not path:
        return {}
    path = Path(path)
    if not path.is_file():
        logger.warning("language CSV not found at %s; using fallbacks only", path)
        return {}
    best: Dict[str, Tuple[str, str]] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            rid = (row.get("resource_id") or "").strip()
            lang = first_language(row.get("source_language"))
            if not rid or not lang:
                continue
            ts = (row.get("timestamp") or "").strip()
            if rid not in best or ts > best[rid][0]:
                best[rid] = (ts, lang)
    out = {rid: lang for rid, (_, lang) in best.items()}
    logger.info("language CSV: %d video languages from %s", len(out), path)
    return out


def module_source_language(module_dir: Path) -> Optional[str]:
    """`source_language` saved by api_fetcher.py into a module's metadata.json."""
    meta = module_dir / "metadata.json"
    if not meta.is_file():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return first_language(data.get("source_language")) if isinstance(data, dict) else None


def resolve_course_language(
    module_ids: Iterable[str],
    lang_map: Dict[str, str],
    api_langs: Optional[Dict[str, str]] = None,
    portal_language: Optional[str] = None,
    db_language: Optional[str] = None,
) -> Tuple[Optional[str], List[str], str]:
    """
    Returns (language, distinct languages across videos, source).
    source is "transcription_api", "transcription_csv", "portal", "portal_db" or "none".
    """
    votes: List[str] = []
    used_api = used_csv = False
    for mid in module_ids:
        lang = (api_langs or {}).get(mid)
        if lang:
            used_api = True
        else:
            lang = lang_map.get(mid)
            if lang:
                used_csv = True
        if lang:
            votes.append(lang)

    if votes:
        counts = Counter(votes)
        top = max(counts.values())
        # Ties: first video (in course order) whose language has the top count.
        language = next(v for v in votes if counts[v] == top)
        distinct = list(dict.fromkeys(votes))
        source = "transcription_api" if used_api else "transcription_csv"
        if used_api and used_csv:
            source = "transcription_api+csv"
        return language, distinct, source

    for value, source in ((portal_language, "portal"), (db_language, "portal_db")):
        lang = first_language(value)
        if lang:
            return lang, [lang], source
    return None, [], "none"
