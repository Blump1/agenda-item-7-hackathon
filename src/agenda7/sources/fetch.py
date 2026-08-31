"""Fetch source documents and stage them for the analyst.

Two-hop by design: download from the agency, then park a copy in Cloud Storage
and hand Gemini a gs:// URI rather than inline bytes. That buys three things:

  * no inline request-size ceiling on long filings
  * provenance -- the exact bytes analyzed are retained and re-readable
  * cheaper reruns, because re-analysis does not re-hit the agency

Inline bytes remain a fallback for when no bucket is configured, so the module
works on a laptop with nothing provisioned.
"""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from agenda7.domain import SourceDocument

log = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 25 * 1024 * 1024  # generous; long filings are legitimately big
PREFERRED_TYPES = ("pdf", "htm", "html", "txt", "doc", "docx")

# downloads.regulations.gov rejects requests whose User-Agent lacks a
# "Mozilla/5.0" token, returning 403 with an HTML body. This is a coarse CDN
# filter, not authentication -- the API key is neither required nor accepted
# there, and these are public comment documents whose URLs the API itself
# hands out in fileFormats. So we satisfy the prefix requirement while still
# identifying honestly who is calling and why, rather than impersonating a
# browser.
USER_AGENT = (
    "Mozilla/5.0 (compatible; agenda-item-7/0.1; "
    "+https://github.com/Blump1/agenda-item-7)"
)


@dataclass
class StagedFile:
    """A document body ready to hand to a model, one way or the other."""

    mime_type: str
    source_url: str
    size: int
    gcs_uri: str | None = None
    data: bytes | None = None

    @property
    def is_staged(self) -> bool:
        return bool(self.gcs_uri or self.data)


def pick_primary_url(doc: SourceDocument) -> str | None:
    """Choose the best single file to analyze.

    Prefers PDF: agency PDFs carry the authoritative layout (tables, appendices)
    that the HTML rendering often flattens or truncates.
    """
    if not doc.file_urls:
        return None
    for want in PREFERRED_TYPES:
        for url in doc.file_urls:
            if url.lower().rsplit(".", 1)[-1] == want:
                return url
    return doc.file_urls[0]


def guess_mime(url: str) -> str:
    guessed, _ = mimetypes.guess_type(url)
    return guessed or "application/octet-stream"


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
def download(url: str, *, max_bytes: int = DEFAULT_MAX_BYTES, timeout: float = 60.0) -> tuple[bytes, str]:
    """Stream a file down, refusing anything over max_bytes.

    Streams rather than buffering the whole response so an unexpectedly huge
    document is abandoned early instead of after it has already been paid for.
    """
    with httpx.Client(
        timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            mime = resp.headers.get("content-type", "").split(";")[0].strip() or guess_mime(url)
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"{url} exceeds max_bytes ({max_bytes})")
                chunks.append(chunk)
    return b"".join(chunks), mime


def upload_to_gcs(
    *, project: str, bucket: str, doc: SourceDocument, data: bytes, mime_type: str, url: str
) -> str:
    """Park the bytes in Cloud Storage and return the gs:// URI.

    Object layout mirrors the document identity so the bucket stays browsable:
      sources/<source>/<docket>/<document-id>/<filename>
    """
    from google.cloud import storage

    filename = url.rsplit("/", 1)[-1] or "content"
    blob_path = (
        f"sources/{doc.source.value}/{doc.docket_id or 'no-docket'}/{doc.source_id}/{filename}"
    )
    client = storage.Client(project=project)
    blob = client.bucket(bucket).blob(blob_path)
    blob.upload_from_string(data, content_type=mime_type)
    uri = f"gs://{bucket}/{blob_path}"
    log.info("staged %s (%d bytes) at %s", doc.source_id, len(data), uri)
    return uri


def stage_document(
    doc: SourceDocument,
    *,
    project: str = "",
    bucket: str = "",
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> StagedFile | None:
    """Download the primary file and stage it, preferring GCS over inline bytes."""
    url = pick_primary_url(doc)
    if not url:
        log.warning("%s has no file urls; nothing to analyze", doc.source_id)
        return None

    try:
        data, mime = download(url, max_bytes=max_bytes)
    except Exception as exc:  # noqa: BLE001
        log.error("download failed for %s: %s: %s", url, type(exc).__name__, exc)
        return None

    staged = StagedFile(mime_type=mime, source_url=url, size=len(data))

    if project and bucket:
        try:
            staged.gcs_uri = upload_to_gcs(
                project=project, bucket=bucket, doc=doc, data=data, mime_type=mime, url=url
            )
            return staged
        except Exception as exc:  # noqa: BLE001
            # Not fatal: fall back to inline so a storage problem does not stop
            # the pipeline from producing an analysis.
            log.warning("GCS staging failed, falling back to inline: %s", exc)

    staged.data = data
    return staged
