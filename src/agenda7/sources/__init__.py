"""Source adapters.

Each adapter normalizes one upstream feed into agenda7.domain.SourceDocument so
nothing downstream knows which feed a document came from. Adding FCC ECFS or a
municipal portal means adding a module here and nothing else.
"""

from agenda7.sources.regulations_gov import RegulationsGovClient

__all__ = ["RegulationsGovClient"]
