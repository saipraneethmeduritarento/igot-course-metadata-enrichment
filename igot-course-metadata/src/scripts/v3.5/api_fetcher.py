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

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("course-regenerator.api_fetcher")

KARMAYOGI_API_KEY = os.environ.get("KARMAYOGI_API_KEY")
KARMAYOGI_BASE_URL = os.environ.get("KARMAYOGI_BASE_URL", "https://igotkarmayogi.gov.in")
LEARNING_AI_BASE_URL = os.environ.get(
    "LEARNING_AI_BASE_URL", "https://learning-ai.prod.karmayogibharat.net"
)
API_CACHE_PATH = Path(os.environ.get("API_CACHE_PATH", "api_course_cache"))
API_REQUEST_TIMEOUT = float(os.environ.get("API_REQUEST_TIMEOUT", "60"))

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
    resp = await client.post(SEARCH_API_URL, json=body)
    resp.raise_for_status()
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


async def fetch_vtt_urls_for_video(client: httpx.AsyncClient, video_id: str) -> List[str]:
    """English subtitle URLs for one video, or [] if none are published yet."""
    try:
        resp = await client.get(TRANSCODER_STATS_URL, params={"resource_id": video_id})
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("transcoder stats failed for video %s: %s", video_id, exc)
        return []
    urls = _extract_vtt_urls(resp.json())
    return [u for u in urls if "/en/" in u or "/english/" in u.lower()]


async def _download_to(url: str, dest: Path, *, authed_client: httpx.AsyncClient) -> bool:
    """
    Download url -> dest. Signed CDN links (VTT/PDF artifactUrl) 401 when the
    Karmayogi auth header is attached, so downloads always use a fresh,
    header-less client -- mirrors ai-assessment-fork's fetcher.py.
    """
    try:
        async with httpx.AsyncClient(timeout=API_REQUEST_TIMEOUT) as plain:
            resp = await plain.get(url)
            resp.raise_for_status()
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
    (leaf_dir / "metadata.json").write_text(
        json.dumps({"name": leaf.get("name")}, ensure_ascii=False), encoding="utf-8"
    )

    mime = leaf.get("mimeType")
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
        vtt_urls = await fetch_vtt_urls_for_video(client, leaf_id)
        parts: List[str] = []
        for url in vtt_urls:
            async with httpx.AsyncClient(timeout=API_REQUEST_TIMEOUT) as plain:
                try:
                    resp = await plain.get(url)
                    resp.raise_for_status()
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
        logger.info("%s: served from local API cache (%s)", course_id, course_dir)
        return course_dir

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
        }
        (course_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )

        for leaf_id in root.get("leafNodes") or []:
            await _materialize_leaf(client, course_id, leaf_id, course_dir)

    logger.info("%s: fetched from Learning API -> %s", course_id, course_dir)
    return course_dir
