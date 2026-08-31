"""Regulations.gov API v4 adapter.

Deliberately thin and defensive. The upstream API is JSON:API shaped and its
filter semantics have sharp edges, so every field read goes through .get() and
the raw payload is retained on SourceDocument for later re-parsing without a
refetch (which matters because standard API keys are rate limited).

Docs: https://open.gsa.gov/api/regulationsgov/
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Iterator

import httpx
from dateutil import parser as dateparser
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from agenda7.domain import SourceDocument, SourceKind, Watch

log = logging.getLogger(__name__)

WEB_DOCUMENT_URL = "https://www.regulations.gov/document/{doc_id}"

# Both verified against the live API, not taken from the docs.
MIN_PAGE_SIZE = 5
MAX_PAGE_SIZE = 250


class RateLimited(Exception):
    """Raised on HTTP 429 so tenacity can back off."""


class RegulationsGovClient:
    """Synchronous client. The poller is I/O-light and runs on a schedule, so
    async buys nothing here and costs debuggability."""

    def __init__(self, api_key: str, base_url: str, timeout: float = 30.0) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "X-Api-Key": api_key,
                "Accept": "application/vnd.api+json",
                "User-Agent": "agenda-item-7/0.1 (personal hackathon project)",
            },
        )
        self.last_rate_limit: dict[str, str] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RegulationsGovClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------- low level ----------------

    @retry(
        retry=retry_if_exception_type((RateLimited, httpx.TransportError)),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._base}/{path.lstrip('/')}"
        resp = self._client.get(url, params=params or {})

        # Surface quota state so the caller can log it; a standard key is ~1000/hr
        # and blowing through it mid-demo is an avoidable embarrassment.
        for header in ("X-RateLimit-Limit", "X-RateLimit-Remaining"):
            if header in resp.headers:
                self.last_rate_limit[header] = resp.headers[header]

        if resp.status_code == 429:
            raise RateLimited(resp.text[:400])
        resp.raise_for_status()
        return resp.json()

    # ---------------- queries ----------------

    def search_documents(
        self,
        *,
        search_term: str | None = None,
        agency_ids: list[str] | None = None,
        docket_id: str | None = None,
        document_types: list[str] | None = None,
        posted_on_or_after: date | None = None,
        page_size: int = 25,
        page_number: int = 1,
        sort: str = "-postedDate",
    ) -> dict[str, Any]:
        # Verified against the live API: page[size] below 5 is a hard 400
        # ("Page size parameter must be a positive number of 5 or greater"),
        # and 250 is the ceiling.
        params: dict[str, Any] = {
            "page[size]": max(MIN_PAGE_SIZE, min(page_size, MAX_PAGE_SIZE)),
            "page[number]": page_number,
            "sort": sort,
        }
        if search_term:
            params["filter[searchTerm]"] = search_term
        if agency_ids:
            params["filter[agencyId]"] = ",".join(agency_ids)
        if docket_id:
            params["filter[docketId]"] = docket_id
        if document_types:
            # Verified: comma-separated values are OR'd, and an unrecognized
            # type yields 0 results rather than an error -- so a typo fails
            # silently and looks like "nothing was posted".
            params["filter[documentType]"] = ",".join(document_types)
        if posted_on_or_after:
            params["filter[postedDate][ge]"] = posted_on_or_after.isoformat()
        return self._get("documents", params)

    def get_document(self, document_id: str) -> dict[str, Any]:
        """Detail fetch. This is where fileFormats (the actual PDF URLs) live -
        the search listing does not include them."""
        return self._get(f"documents/{document_id}", {"include": "attachments"})

    # ---------------- normalization ----------------

    @staticmethod
    def _as_date(value: Any) -> date | None:
        if not value:
            return None
        try:
            return dateparser.parse(str(value)).date()
        except (ValueError, TypeError, OverflowError):
            log.debug("unparseable date: %r", value)
            return None

    @staticmethod
    def _as_datetime(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return dateparser.parse(str(value))
        except (ValueError, TypeError, OverflowError):
            return None

    @staticmethod
    def _derive_docket_id(doc_id: str) -> str | None:
        """Recover a docket id from a document id.

        Observed on live data: `docketId` is sometimes null in search listings
        even though the document plainly belongs to one. Document ids follow the
        {docketId}-{sequence} convention (FCC-2026-3271-0001 -> FCC-2026-3271),
        so the docket is recoverable without spending a detail request.
        """
        head, _, tail = str(doc_id).rpartition("-")
        return head if head and tail.isdigit() else None

    @staticmethod
    def _file_urls(attrs: dict[str, Any]) -> list[str]:
        return [
            fmt["fileUrl"]
            for fmt in (attrs.get("fileFormats") or [])
            if isinstance(fmt, dict) and fmt.get("fileUrl")
        ]

    @classmethod
    def to_source_document(cls, record: dict[str, Any]) -> SourceDocument:
        attrs = record.get("attributes") or {}
        doc_id = str(record.get("id") or attrs.get("objectId") or "")

        return SourceDocument(
            source=SourceKind.REGULATIONS_GOV,
            source_id=doc_id,
            title=(attrs.get("title") or "(untitled)").strip(),
            docket_id=attrs.get("docketId") or cls._derive_docket_id(doc_id),
            agency_id=attrs.get("agencyId"),
            document_type=attrs.get("documentType"),
            posted_date=cls._as_date(attrs.get("postedDate")),
            comment_start_date=cls._as_date(attrs.get("commentStartDate")),
            comment_end_date=cls._as_date(attrs.get("commentEndDate")),
            open_for_comment=bool(attrs.get("openForComment", False)),
            within_comment_period=bool(attrs.get("withinCommentPeriod", False)),
            last_modified=cls._as_datetime(attrs.get("lastModifiedDate")),
            web_url=WEB_DOCUMENT_URL.format(doc_id=doc_id) if doc_id else None,
            file_urls=cls._file_urls(attrs),
            raw=record,
        )

    def enrich_from_detail(self, doc: SourceDocument) -> SourceDocument:
        """Spend one detail request to add the fields the analyst needs.

        Only called for documents that survived triage. `docAbstract` and
        `pageCount` matter most: the abstract often makes a PDF download
        unnecessary, and the page count lets the analyst decide whether a
        document is worth the tokens before committing to it.
        """
        payload = self.get_document(doc.source_id)
        attrs = (payload.get("data") or {}).get("attributes") or {}
        if not attrs:
            return doc

        merged = doc.model_copy(
            update={
                "abstract": attrs.get("docAbstract") or doc.abstract,
                "page_count": attrs.get("pageCount") or doc.page_count,
                "topics": [t for t in (attrs.get("topics") or []) if isinstance(t, str)],
                "file_urls": self._file_urls(attrs) or doc.file_urls,
                "docket_id": attrs.get("docketId") or doc.docket_id,
                "comment_end_date": self._as_date(attrs.get("commentEndDate"))
                or doc.comment_end_date,
                "within_comment_period": bool(
                    attrs.get("withinCommentPeriod", doc.within_comment_period)
                ),
            }
        )
        return merged

    # ---------------- watch execution ----------------

    def documents_for_watch(
        self,
        watch: Watch,
        *,
        since: date | None = None,
        lookback_days: int = 7,
        max_docs: int = 40,
    ) -> Iterator[SourceDocument]:
        """Yield normalized documents matching a watch, newest first.

        A watch with several docket_ids fans out into one query per docket,
        because the upstream filter takes a single docketId.
        """
        floor = since or (date.today() - timedelta(days=lookback_days))
        targets: list[str | None] = list(watch.docket_ids) or [None]

        emitted = 0
        for docket_id in targets:
            page = 1
            while emitted < max_docs:
                # Never request fewer than MIN_PAGE_SIZE even when only a couple
                # of slots remain; the API 400s instead of clamping.
                want = max(MIN_PAGE_SIZE, min(25, max_docs - emitted))
                payload = self.search_documents(
                    search_term=watch.search_term,
                    agency_ids=watch.agency_ids,
                    docket_id=docket_id,
                    document_types=watch.document_types,
                    posted_on_or_after=floor,
                    page_size=want,
                    page_number=page,
                )
                records = payload.get("data") or []
                if not records:
                    break
                for record in records:
                    yield self.to_source_document(record)
                    emitted += 1
                    if emitted >= max_docs:
                        break

                # meta.hasNextPage is authoritative. A short page is not a safe
                # terminator, because max_docs can force an odd page size that
                # legitimately returns fewer rows than requested.
                if not (payload.get("meta") or {}).get("hasNextPage"):
                    break
                page += 1
