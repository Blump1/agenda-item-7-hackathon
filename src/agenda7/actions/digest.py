"""Render a run into something a human actually reads.

Markdown, not email. The digest is an artifact on disk (and optionally in Cloud
Storage) rather than a message sent somewhere, so it can be reviewed before
anything is transmitted. Wiring it to a real delivery channel is a deployment
decision, covered in docs/DEPLOY.md.

Ordering is by urgency, not by discovery: the closest deadline goes first,
because that is the only question the reader actually has.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Sequence

from agenda7.domain import CommentDraft, DocketPosition


def _urgency(days_out: int) -> str:
    if days_out <= 3:
        return "**CLOSING**"
    if days_out <= 14:
        return "soon"
    return ""


def render_digest(
    *,
    run_id: str,
    watch_label: str,
    discovered: int,
    triaged: int,
    relevant: int,
    analyzed: int,
    failed: int,
    positions: Sequence[DocketPosition],
    deadlines: Sequence[dict[str, Any]],
    drafts: Sequence[CommentDraft] = (),
    rejected: Sequence[tuple[str, str]] = (),
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    out: list[str] = [
        "# Agenda Item 7 — run digest",
        "",
        f"**Watch:** {watch_label}  ",
        f"**Run:** `{run_id}`  ",
        f"**Generated:** {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "---",
        "",
    ]

    # Deadlines first: this is the part with consequences.
    out += ["## Deadlines ahead", ""]
    if deadlines:
        out += ["| Due | In | Docket | What |", "| --- | --- | --- | --- |"]
        for row in deadlines:
            days = int(row.get("days_out", 0))
            flag = _urgency(days)
            what = str(row.get("what", "")).replace("|", "\\|")
            out.append(
                f"| {row['due']} | {days}d {flag} | `{row['docket_id']}` | {what} |"
            )
    else:
        out.append("_No deadlines currently tracked._")
    out.append("")

    out += ["## What changed", ""]
    if positions:
        for p in positions:
            out += [
                f"### `{p.docket_id}` — revision {p.revision}",
                "",
                f"{p.current_understanding or '_no summary recorded_'}",
                "",
            ]
            if p.parties:
                out.append("**Positions on record**")
                out.append("")
                for party in p.parties:
                    out.append(f"- **{party.party}** — _{party.stance}_: {party.summary}")
                out.append("")
            if p.history:
                out.append("**History**")
                out.append("")
                for entry in p.history[-6:]:
                    out.append(f"- {entry}")
                out.append("")
    else:
        out += ["_Nothing new was analyzed this run._", ""]

    if drafts:
        out += ["## Comment drafts staged for your review", ""]
        for d in drafts:
            out += [
                f"### `{d.docket_id}` — {d.subject}",
                "",
                f"Deadline: **{d.comment_deadline or 'unknown'}**  ",
                f"Filing: {d.filing_url or 'n/a'}",
                "",
            ]
            if d.needs_from_you:
                out.append("**Needs you before filing:**")
                out.append("")
                for item in d.needs_from_you:
                    out.append(f"- {item}")
                out.append("")
            out += ["<details><summary>Draft text</summary>", "", d.body, "", "</details>", ""]
        out += [
            "> These are drafts. Nothing has been filed, and this system has no "
            "ability to file. Review, edit, and submit yourself.",
            "",
        ]

    # Rejections last but present: an unexplained silent filter is not
    # trustworthy, and reading the rejections is how the gate gets tuned.
    if rejected:
        out += [
            "## Filtered out",
            "",
            f"<details><summary>{len(rejected)} document(s) judged not relevant</summary>",
            "",
        ]
        for title, why in rejected:
            out.append(f"- **{title[:110]}** — {why}")
        out += ["", "</details>", ""]

    out += [
        "---",
        "",
        f"Discovered {discovered} · triaged {triaged} · relevant {relevant} · "
        f"analyzed {analyzed} · failed {failed}",
        "",
    ]
    return "\n".join(out)


def digest_filename(run_id: str, when: date | None = None) -> str:
    when = when or date.today()
    return f"digest-{when.isoformat()}-{run_id}.md"
