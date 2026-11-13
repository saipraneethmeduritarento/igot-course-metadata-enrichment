"""
PDF chunk summarisation (v3.6).

The main metadata call used to receive raw PDF text, truncated to share a fixed
character budget across every document in the course, so long documents were cut
off part-way. Instead, each document is now split into chunks, each chunk is
summarised by Gemini, and the main call receives the summaries.

  - Text PDFs (and .pdf.txt sidecars): split on paragraph boundaries into
    ~PDF_CHUNK_CHARS chunks, one summary call per chunk.
  - Scanned / legacy-font PDFs (no usable text layer): the PDF bytes are sent and
    the model reads the pages, one call per document.

Summaries are cached on disk by content hash, so re-running a course (or a
course that shares a document with another) does not pay for them again.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from google.genai import types

from prompts import PROMPTS

logger = logging.getLogger("course-regenerator.pdf_summarizer")

PDF_CHUNK_CHARS = int(os.environ.get("PDF_CHUNK_CHARS", "12000"))
PDF_SUMMARY_CONCURRENCY = int(os.environ.get("PDF_SUMMARY_CONCURRENCY", "4"))
# Guard against a course with dozens of long PDFs turning into hundreds of calls.
# Chunks are allocated round-robin across documents, so every document keeps at
# least its opening chunk before any document gets a second one.
PDF_SUMMARY_MAX_CHUNKS_PER_COURSE = int(
    os.environ.get("PDF_SUMMARY_MAX_CHUNKS_PER_COURSE", "40")
)
PDF_SUMMARY_CACHE = Path(os.environ.get("PDF_SUMMARY_CACHE", "pdf_summary_cache"))
PDF_SUMMARY_MAX_OUTPUT_TOKENS = int(os.environ.get("PDF_SUMMARY_MAX_OUTPUT_TOKENS", "8192"))
# Prompt text and its cache-key version live in src/prompts/<version>.yaml
# (pdf_summary). Bump pdf_summary.version when the instructions change, so cached
# summaries are not reused.
SUMMARY_PROMPT_VERSION: str = str(PROMPTS["pdf_summary"]["version"])
SUMMARY_INSTRUCTIONS: str = PROMPTS["pdf_summary"]["instructions"]


def split_into_chunks(text: str, chunk_chars: int = PDF_CHUNK_CHARS) -> List[str]:
    """Split on blank lines, packing paragraphs up to chunk_chars; hard-split giants."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for para in paragraphs:
        while len(para) > chunk_chars:
            if current:
                chunks.append("\n\n".join(current))
                current, size = [], 0
            chunks.append(para[:chunk_chars])
            para = para[chunk_chars:]
        if size + len(para) > chunk_chars and current:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(para)
        size += len(para) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _cache_key(kind: str, payload: bytes) -> str:
    h = hashlib.sha1()
    h.update(f"{SUMMARY_PROMPT_VERSION}:{kind}:".encode())
    h.update(payload)
    return h.hexdigest()


class PdfSummarizer:
    def __init__(
        self,
        client: Any,
        model: str,
        *,
        is_transient: Callable[[BaseException], bool],
        max_attempts: int = 3,
        timeout_seconds: float = 300.0,
        temperature: float = 0.1,
        cache_dir: Path = PDF_SUMMARY_CACHE,
    ) -> None:
        self.client = client
        self.model = model
        self.is_transient = is_transient
        self.max_attempts = max_attempts
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._sem = asyncio.Semaphore(max(1, PDF_SUMMARY_CONCURRENCY))

    # ------------------------------------------------------------------ calls
    async def _call(self, parts: List[Any]) -> Tuple[str, Dict[str, int]]:
        config = types.GenerateContentConfig(
            temperature=self.temperature,
            max_output_tokens=PDF_SUMMARY_MAX_OUTPUT_TOKENS,
        )
        contents = [types.Content(role="user", parts=parts)]
        last_exc: Optional[BaseException] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                async with self._sem:
                    resp = await asyncio.wait_for(
                        self.client.aio.models.generate_content(
                            model=self.model, contents=contents, config=config
                        ),
                        timeout=self.timeout_seconds,
                    )
                um = resp.usage_metadata
                usage = {
                    "prompt_tokens": (um.prompt_token_count or 0) if um else 0,
                    "output_tokens": (um.candidates_token_count or 0) if um else 0,
                    "thinking_tokens": (getattr(um, "thoughts_token_count", 0) or 0) if um else 0,
                    "calls": 1,
                }
                return (resp.text or "").strip(), usage
            except BaseException as exc:  # noqa: BLE001 - classified below
                last_exc = exc
                if not self.is_transient(exc) or attempt == self.max_attempts:
                    raise
                backoff = min(30.0, 2 ** attempt) + random.uniform(0, 1.0)
                logger.warning(
                    "PDF summary attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt, self.max_attempts, type(exc).__name__, backoff,
                )
                await asyncio.sleep(backoff)
        raise last_exc if last_exc else RuntimeError("PDF summary call failed")

    async def _summarise(
        self, key: str, parts: List[Any], label: str
    ) -> Tuple[str, Dict[str, int]]:
        cache_file = self.cache_dir / f"{key}.txt"
        if cache_file.is_file():
            return cache_file.read_text(encoding="utf-8"), {"cache_hits": 1}
        try:
            text, usage = await self._call(parts)
        except Exception as exc:
            # One bad chunk must not fail the course: it just contributes nothing.
            logger.warning("PDF summary failed for %s: %s", label, str(exc)[:200])
            return "", {"failed": 1}
        cache_file.write_text(text, encoding="utf-8")
        return text, usage

    # ------------------------------------------------------------------- API
    async def summarise_documents(
        self, course_id: str, documents: List[Dict[str, Any]]
    ) -> Tuple[str, Dict[str, int]]:
        """
        Returns (pdf_summaries text for the prompt, usage totals).
        documents: [{"label", "text"} | {"label", "native_path"}] from course_io.
        """
        if not documents:
            return "", {}
        start = time.time()

        # Plan the calls: text chunks round-robin across documents, then natives.
        per_doc_chunks: List[Tuple[str, List[str]]] = [
            (d["label"], split_into_chunks(d["text"]))
            for d in documents if d.get("text")
        ]
        natives = [d for d in documents if d.get("native_path")]
        budget = max(0, PDF_SUMMARY_MAX_CHUNKS_PER_COURSE - len(natives))
        selected: Dict[str, List[int]] = {label: [] for label, _ in per_doc_chunks}
        depth = 0
        while budget > 0:
            progressed = False
            for label, chunks in per_doc_chunks:
                if depth < len(chunks) and budget > 0:
                    selected[label].append(depth)
                    budget -= 1
                    progressed = True
            if not progressed:
                break
            depth += 1
        skipped_chunks = sum(len(c) for _, c in per_doc_chunks) - sum(
            len(v) for v in selected.values()
        )

        jobs: List[Tuple[str, int, int, Any]] = []  # (label, chunk_no, n_chunks, coroutine)
        for label, chunks in per_doc_chunks:
            for i in selected[label]:
                chunk = chunks[i]
                key = _cache_key("text", chunk.encode("utf-8"))
                parts = [types.Part.from_text(
                    text=f"{SUMMARY_INSTRUCTIONS}\n\nDocument: {label}"
                         f" (part {i + 1} of {len(chunks)})\n<BeginText>\n{chunk}\n<EndText>"
                )]
                jobs.append((label, i, len(chunks), self._summarise(
                    key, parts, f"{course_id}/{label}#{i + 1}"
                )))
        for d in natives:
            path: Path = d["native_path"]
            try:
                data = path.read_bytes()
            except OSError as exc:
                logger.warning("%s: could not read %s: %s", course_id, path, exc)
                continue
            key = _cache_key("pdf", data)
            parts = [
                types.Part.from_bytes(data=data, mime_type="application/pdf"),
                types.Part.from_text(
                    text=f"{SUMMARY_INSTRUCTIONS}\n\nDocument: {d['label']} (attached PDF; "
                         "summarise the whole document)"
                ),
            ]
            jobs.append((d["label"], 0, 1, self._summarise(
                key, parts, f"{course_id}/{d['label']} (native)"
            )))

        results = await asyncio.gather(*(job[3] for job in jobs))

        totals: Dict[str, int] = {}
        by_doc: Dict[str, List[Tuple[int, int, str]]] = {}
        for (label, i, n, _), (text, usage) in zip(jobs, results):
            for k, v in usage.items():
                totals[k] = totals.get(k, 0) + v
            if text and text.strip() != "NO_CONTENT":
                by_doc.setdefault(label, []).append((i, n, text.strip()))

        blocks: List[str] = []
        for label in dict.fromkeys(job[0] for job in jobs):
            parts_ = sorted(by_doc.get(label, []))
            if not parts_:
                continue
            if len(parts_) == 1 and parts_[0][1] == 1:
                body = parts_[0][2]
            else:
                body = "\n\n".join(f"(Part {i + 1} of {n}) {t}" for i, n, t in parts_)
            blocks.append(f"## Document: {label}\n{body}")
        if skipped_chunks:
            blocks.append(
                f"[...{skipped_chunks} further document part(s) not summarised "
                f"(limit {PDF_SUMMARY_MAX_CHUNKS_PER_COURSE} per course)...]"
            )

        logger.info(
            "%s: summarised %d PDF part(s) from %d document(s) in %.1fs "
            "(cache hits %d, failed %d)",
            course_id, len(jobs), len(documents), time.time() - start,
            totals.get("cache_hits", 0), totals.get("failed", 0),
        )
        return "\n\n---\n\n".join(blocks), totals
