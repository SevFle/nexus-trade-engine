"""Reference data — canonical symbol master, identifier resolution,
exchange metadata, sector classifications, and ingestion contract.

This package gives every other engine subsystem a single source of truth
for "what is this instrument" and "how do I look it up". Tickers go in,
:class:`RefInstrument` records come out — keyed by stable internal UUIDs
that survive symbol changes (e.g. FB → META) and cross-venue listings.
"""

from engine.reference.exceptions import AmbiguousSymbolError
from engine.reference.model import (
    Classification,
    GICSNode,
    InstrumentIds,
    Listing,
    RefInstrument,
    Venue,
)
from engine.reference.resolver import Resolver

__all__ = [
    "AmbiguousSymbolError",
    "Classification",
    "GICSNode",
    "InstrumentIds",
    "Listing",
    "RefInstrument",
    "Resolver",
    "Venue",
]
