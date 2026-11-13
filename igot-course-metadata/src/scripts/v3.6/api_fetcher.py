"""
Fetching course content from the Karmayogi Learning API, as an alternative to
reading it off local disk (course_io.py).

Used only when meta_gen.py is run with --api. materialize_course() downloads
a course's metadata, PDFs and English VTT transcripts and writes them into the
exact on-disk shape course_io.py already reads (see its module docstring), so
extract_course_content()/load_course_metadata() need no changes at all to
consume API-fetched content.

Modelled on the ai-assessment-fork service's src/assessment/fetcher.py, which
fetches the same content for a different purpose (assessment generation).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("course-regenerator.api_fetcher")

KARMAYOGI_API_KEY = os.environ.get("KARMAYOGI_API_KEY")
KARMAYOGI_BASE_URL = os.environ.get("KARMAYOGI_BASE_URL", "https://igotkarmayogi.gov.in")
LEARNING_AI_BASE_URL = os.environ.get(
    "LEARNING_AI_BASE_URL", "https://learning-ai.prod.karmayogibharat.net"
)
API_CACHE_PATH = Path(os.environ.get("API_CACHE_PATH", "api_course_cache"))
API_REQUEST_TIMEOUT = float(os.environ.get("API_REQUEST_TIMEOUT", "60"))
# The Learning API returns 504s under load (2 of 5 transcoder calls in a manual
# test), so every API call retries transient failures with backoff, and the
# number of calls in flight across all courses is capped.
API_MAX_ATTEMPTS = int(os.environ.get("API_MAX_ATTEMPTS", "4"))
API_CONCURRENCY = int(os.environ.get("API_CONCURRENCY", "8"))
# Set by meta_gen.py --refresh-api: refetch courses already in the cache (needed
# for courses cached before duration/language were saved).
REFRESH = False
_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
_api_semaphore: Optional[asyncio.Semaphore] = None

SEARCH_API_URL = f"{KARMAYOGI_BASE_URL}/api/content/v1/search"
TRANSCODER_STATS_URL = f"{LEARNING_AI_BASE_URL}/api/kb-pipeline/v3/transcoder/stats"

# authorization carries the literal "Bearer <token>" string, used verbatim.
API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "authorization": KARMAYOGI_API_KEY or "",
    "org": "dopt",
    "rootorg": "igot",
    "locale": "en",
}

# Content types observed on SCORM packages in the platform's own exports.
# Best-effort only -- not confirmed against a live SCORM course through this
# API, so a course misdetected here just gets scorm=False, same as any course
# whose metadata.json never carried the field at all.
_SCORM_MIME_TYPES = ("application/vnd.ekstep.h5p-archive", "application/vnd.ekstep.html-archive")


def _require_api_key() -> None:
    if not KARMAYOGI_API_KEY:
        raise RuntimeError(
            "KARMAYOGI_API_KEY is not set; required to fetch course content via --api"
        )


def strip_html(text: Optional[str]) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def _declared_area_string(raw: Any) -> str:
    """
    Match the local export's competencies_v6 shape: a newline-joined string of
    one competency-area name per module (e.g. "Functional\\nFunctional"),
    which course_io.load_course_metadata splits on "\\n".

    Confirmed against a live course that the Learning API instead returns a
    list of competency objects (one per theme/sub-theme mapping), each
    carrying its area under competencyAreaName -- not a pre-joined string.
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        areas = [
            str(c.get("competencyAreaName") or "").strip()
            for c in raw if isinstance(c, dict)
        ]
        return "\n".join(a for a in areas if a)
    return ""


def _semaphore() -> asyncio.Semaphore:
    global _api_semaphore
    if _api_semaphore is None:
        _api_semaphore = asyncio.Semaphore(max(1, API_CONCURRENCY))
    return _api_semaphore


async def _request(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    """One API call with retry on 429/5xx and transport errors."""
    last_exc: Optional[BaseException] = None
    for attempt in range(1, API_MAX_ATTEMPTS + 1):
        try:
            async with _semaphore():
                resp = await client.request(method, url, **kwargs)
            if resp.status_code in _RETRY_STATUS and attempt < API_MAX_ATTEMPTS:
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()
            return resp
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if (status is not None and status not in _RETRY_STATUS) or attempt == API_MAX_ATTEMPTS:
                raise
            backoff = min(20.0, 2 ** attempt) + random.uniform(0, 1.0)
            logger.info(
                "API %s %s failed (%s); retry %d/%d in %.1fs",
                method, url.split("?")[0], status or type(exc).__name__,
                attempt, API_MAX_ATTEMPTS - 1, backoff,
            )
            await asyncio.sleep(backoff)
    raise last_exc if last_exc else RuntimeError("API call failed")


async def search_content(client: httpx.AsyncClient, identifier: str) -> Optional[Dict[str, Any]]:
    body = {
        "request": {
            "filters": {"identifier": identifier},
            "isSecureSettingsDisabled": True,
            "status": ["Live"],
            "fields": [],
            "limit": 1,
        }
    }
    resp = await _request(client, "POST", SEARCH_API_URL, json=body)
    content = (resp.json().get("result") or {}).get("content") or []
    return content[0] if content else None


def _extract_vtt_urls(obj: Any, found: Optional[List[str]] = None) -> List[str]:
    if found is None:
        found = []
    if isinstance(obj, str):
        if obj.endswith(".vtt"):
            found.append(obj)
    elif isinstance(obj, list):
        for item in obj:
            _extract_vtt_urls(item, found)
    elif isinstance(obj, dict):
        for value in obj.values():
            _extract_vtt_urls(value, found)
    return found


def _find_default_lang(obj: Any) -> Optional[str]:
    """The transcoder's `default_lang`: the spoken (source) language of the video."""
    if isinstance(obj, dict):
        value = obj.get("default_lang")
        if isinstance(value, str) and value.strip():
            return value.strip()
        for v in obj.values():
            found = _find_default_lang(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_default_lang(item)
            if found:
                return found
    return None


async def fetch_transcription_info(
    client: httpx.AsyncClient, video_id: str
) -> Tuple[List[str], Optional[str]]:
    """
    (English subtitle URLs, source language code) for one video. ([], None) if
    nothing is published yet or the transcoder keeps failing.
    """
    try:
        resp = await _request(
            client, "GET", TRANSCODER_STATS_URL, params={"resource_id": video_id}
        )
    except httpx.HTTPError as exc:
        logger.warning("transcoder stats failed for video %s: %s", video_id, exc)
        return [], None
    data = resp.json()
    urls = _extract_vtt_urls(data)
    english = [u for u in urls if "/en/" in u or "/english/" in u.lower()]
    return english, _find_default_lang(data)


async def fetch_vtt_urls_for_video(client: httpx.AsyncClient, video_id: str) -> List[str]:
    """English subtitle URLs for one video, or [] if none are published yet."""
    urls, _ = await fetch_transcription_info(client, video_id)
    return urls


async def _download_to(url: str, dest: Path, *, authed_client: httpx.AsyncClient) -> bool:
    """
    Download url -> dest. Signed CDN links (VTT/PDF artifactUrl) 401 when the
    Karmayogi auth header is attached, so downloads always use a fresh,
    header-less client -- mirrors ai-assessment-fork's fetcher.py.
    """
    try:
        async with httpx.AsyncClient(timeout=API_REQUEST_TIMEOUT) as plain:
            resp = await _request(plain, "GET", url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return True
    except httpx.HTTPError as exc:
        logger.warning("download failed for %s: %s", url, exc)
        return False


def _dir_name(identifier: str) -> str:
    return identifier if identifier.startswith("do_") else f"do_{identifier}"


async def _materialize_leaf(
    client: httpx.AsyncClient, course_id: str, leaf_id: str, course_dir: Path
) -> None:
    """
    Materialize one entry from the course's leafNodes.

    Confirmed against a live course: leafNodes are flat -- each id names a
    terminal content item (a video, a PDF, or a quiz/assessment as
    application/json) directly, never a container with nested children. So
    the leaf's own mimeType decides what to do with it; there is nothing to
    recurse into.
    """
    leaf = await search_content(client, leaf_id)
    if not leaf:
        logger.warning("%s: leaf %s not found; skipping", course_id, leaf_id)
        return

    leaf_dir = course_dir / _dir_name(leaf_id)
    leaf_dir.mkdir(parents=True, exist_ok=True)
    mime = leaf.get("mimeType")
    leaf_meta: Dict[str, Any] = {
        "name": leaf.get("name"),
        "identifier": leaf_id,
        "mimeType": mime,
        "duration": leaf.get("duration"),
    }

    def _write_leaf_meta() -> None:
        (leaf_dir / "metadata.json").write_text(
            json.dumps(leaf_meta, ensure_ascii=False), encoding="utf-8"
        )

    _write_leaf_meta()

    if mime == "application/pdf" and leaf.get("artifactUrl"):
        name = str(leaf.get("name") or leaf_id).strip()
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        await _download_to(leaf["artifactUrl"], leaf_dir / name, authed_client=client)
        # course_io.extract_course_content reads reference links from
        # pdf_links.txt/video.txt, not from the PDFs/VTTs it already parsed for
        # text -- match the local export so ReferenceResources.ExtendedLearning
        # is seeded the same way regardless of content source.
        (leaf_dir / "pdf_links.txt").write_text(leaf["artifactUrl"], encoding="utf-8")

    elif mime == "video/mp4":
        if leaf.get("artifactUrl"):
            (leaf_dir / "video.txt").write_text(leaf["artifactUrl"], encoding="utf-8")
        vtt_urls, source_lang = await fetch_transcription_info(client, leaf_id)
        if source_lang:
            # Spoken language of this video; language.py votes on these.
            leaf_meta["source_language"] = source_lang
            _write_leaf_meta()
        parts: List[str] = []
        for url in vtt_urls:
            async with httpx.AsyncClient(timeout=API_REQUEST_TIMEOUT) as plain:
                try:
                    resp = await _request(plain, "GET", url)
                    parts.append(resp.text)
                except httpx.HTTPError as exc:
                    logger.warning("%s: VTT download failed for %s: %s", course_id, url, exc)
        if parts:
            (leaf_dir / "english_subtitles.vtt").write_text(
                "\n\n".join(parts), encoding="utf-8"
            )
    # Other mimeTypes (e.g. application/json quiz/assessment nodes) carry no
    # text or transcript content course_io.py would otherwise extract.


async def materialize_course(course_id: str, cache_base: Path = API_CACHE_PATH) -> Path:
    """
    Ensure course_id's content exists on disk under cache_base, fetching it
    from the Learning API if this is the first time it has been requested.

    Returns the course folder, in the same shape a locally-exported course
    folder would have -- callers should pass it straight to
    course_io.load_course_metadata / extract_course_content unchanged.
    """
    course_dir = cache_base / course_id
    if (course_dir / "metadata.json").is_file():
        if not REFRESH:
            logger.info("%s: served from local API cache (%s)", course_id, course_dir)
            return course_dir
        # --refresh-api: start clean so files from the old fetch don't linger.
        if course_dir.resolve().parent == Path(cache_base).resolve():
            shutil.rmtree(course_dir)

    _require_api_key()
    async with httpx.AsyncClient(timeout=API_REQUEST_TIMEOUT, headers=API_HEADERS) as client:
        root = await search_content(client, course_id)
        if not root:
            raise RuntimeError(f"course {course_id} not found via Learning API")

        course_dir.mkdir(parents=True, exist_ok=True)
        mime = str(root.get("mimeType") or "")
        if mime in _SCORM_MIME_TYPES:
            logger.info("%s: mimeType %s looks SCORM-like; scorm flag not verified", course_id, mime)

        # The API returns a list (e.g. ["Institute of ..."]); the local export's
        # metadata.json always carries a single string. meta_gen.py's tag-dropping
        # logic does str(organisation).strip() expecting that shape, so match it
        # rather than leaving a list to stringify as "['Institute of ...']".
        organisation = root.get("organisation")
        if isinstance(organisation, list):
            organisation = organisation[0] if organisation else None

        metadata = {
            "name": root.get("name"),
            "description": root.get("description"),
            "keywords": root.get("keywords"),
            "organisation": organisation,
            "courseCategory": root.get("courseCategory"),
            "instructions": strip_html(root.get("instructions")),
            "competencies_v6": _declared_area_string(root.get("competencies_v6")),
            "scorm": False,
            # Portal facts used as-is in the output (title/description above too).
            "duration": root.get("duration"),
            "language": root.get("language"),
            "learningMode": root.get("learningMode"),
            "primaryCategory": root.get("primaryCategory"),
            "mimeType": mime,
        }
        (course_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )

        for leaf_id in root.get("leafNodes") or []:
            await _materialize_leaf(client, course_id, leaf_id, course_dir)

    logger.info("%s: fetched from Learning API -> %s", course_id, course_dir)
    return course_dir
