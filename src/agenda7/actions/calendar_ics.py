"""Turn tracked deadlines into an importable calendar.

An .ics file rather than a Google Calendar API call, deliberately. Writing to
someone's real calendar needs OAuth and consent per run; a file is portable,
auditable, offline, imports into Google Calendar, Outlook, or Apple Calendar
alike, and can be inspected before it touches anything. docs/DEPLOY.md covers
wiring the live Calendar API later if that trade stops being worth it.

Format follows RFC 5545. The fiddly parts -- octet-aware line folding and text
escaping -- are implemented rather than approximated, because calendar clients
reject malformed files with no useful error.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

PRODID = "-//agenda-item-7//deadline tracker//EN"

# Remind well before the date. A comment deadline you learn about the morning
# it closes is not much better than missing it.
REMINDERS_DAYS_BEFORE = (14, 3)


def _escape(text: str) -> str:
    """Escape per RFC 5545 section 3.3.11. Order matters: backslash first."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> list[str]:
    """Fold to 75 octets per line, continuations prefixed with a space.

    The limit is octets, not characters, so folding is done on the UTF-8
    encoding and split on a byte boundary that does not bisect a codepoint.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return [line]

    out: list[str] = []
    limit = 75
    while raw:
        chunk = raw[:limit]
        # Back off if we split a multi-byte sequence.
        while chunk and (chunk[-1] & 0xC0) == 0x80:
            chunk = chunk[:-1]
        if not chunk:  # pathological; emit what we can
            chunk = raw[:limit]
        out.append(chunk.decode("utf-8", errors="ignore"))
        raw = raw[len(chunk) :]
        limit = 74  # continuation lines lose one octet to the leading space
    return [out[0]] + [" " + part for part in out[1:]]


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _uid(docket_id: str, what: str, due: date) -> str:
    """Stable UID so re-importing updates the event instead of duplicating it."""
    digest = hashlib.sha1(f"{docket_id}|{what}|{due}".encode("utf-8")).hexdigest()[:16]
    return f"{digest}@agenda-item-7"


def build_calendar(
    deadlines: Iterable[dict[str, Any]],
    *,
    now: datetime | None = None,
    calendar_name: str = "Agenda Item 7 deadlines",
) -> str:
    """Render deadline rows (as produced by Store.upcoming_deadlines) as .ics."""
    now = now or datetime.now(timezone.utc)
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(calendar_name)}",
    ]

    count = 0
    for row in deadlines:
        due = row.get("due")
        if isinstance(due, str):
            due = date.fromisoformat(due)
        if not isinstance(due, date):
            continue

        docket = str(row.get("docket_id") or "unknown")
        what = str(row.get("what") or "Deadline")
        summary = f"{docket}: {what}"
        description_bits = [
            f"Docket: {docket}",
            f"Deadline: {what}",
        ]
        if row.get("url"):
            description_bits.append(f"Filing: {row['url']}")
        description_bits.append(
            "Tracked automatically by Agenda Item 7. Verify against the filing "
            "before relying on this date."
        )

        lines += [
            "BEGIN:VEVENT",
            f"UID:{_uid(docket, what, due)}",
            f"DTSTAMP:{_stamp(now)}",
            # All-day event: DTEND is exclusive, so it is the following day.
            f"DTSTART;VALUE=DATE:{due.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(due + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_escape(summary)}",
            f"DESCRIPTION:{_escape(chr(10).join(description_bits))}",
            "TRANSP:TRANSPARENT",
        ]
        if row.get("url"):
            lines.append(f"URL:{_escape(str(row['url']))}")

        for days in REMINDERS_DAYS_BEFORE:
            lines += [
                "BEGIN:VALARM",
                f"TRIGGER:-P{days}D",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{_escape(f'{days} days until: {summary}')}",
                "END:VALARM",
            ]
        lines.append("END:VEVENT")
        count += 1

    lines.append("END:VCALENDAR")

    folded: list[str] = []
    for line in lines:
        folded.extend(_fold(line))
    # RFC 5545 requires CRLF line endings and a trailing break.
    return "\r\n".join(folded) + "\r\n"


def event_count(ics: str) -> int:
    return ics.count("BEGIN:VEVENT")
