"""Reference-data ingestion adapters.

Base contract :class:`IngestionAdapter` is in :mod:`.base`. Concrete
adapters (OpenFIGI, SEC EDGAR, MIC, Polygon, Alpaca, Binance) live in
sibling modules. PR1 ships the contract and an OpenFIGI skeleton; HTTP
implementations land in follow-up issues.
"""

from engine.reference.ingestion.base import IngestionAdapter, IngestionResult

__all__ = ["IngestionAdapter", "IngestionResult"]
