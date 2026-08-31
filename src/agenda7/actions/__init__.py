"""Actions: what the agent does once it understands something.

Three outputs, none of which transmit anything:

  calendar_ics - deadlines as an importable .ics file with lead-time reminders
  digest       - a readable markdown summary of what changed and what is due
  drafter      - a public comment drafted and staged for human review

The consistent choice here is artifact over transmission. Sending mail, writing
to a real calendar, or filing a comment all require consent that a background
job cannot meaningfully obtain, so this layer produces reviewable files and
stops. docs/DEPLOY.md documents how to wire delivery if you decide to.
"""

from agenda7.actions.calendar_ics import build_calendar, event_count
from agenda7.actions.digest import digest_filename, render_digest
from agenda7.actions.drafter import build_drafter_agent, drafter_agent, drafter_parts

__all__ = [
    "build_calendar",
    "build_drafter_agent",
    "digest_filename",
    "drafter_agent",
    "drafter_parts",
    "event_count",
    "render_digest",
]
