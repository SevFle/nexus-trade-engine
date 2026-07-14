"""Historical OHLCV data-provider interface.

This module defines :class:`IDataProvider`, the contract for adapters that
load *historical* OHLCV bars from a local/offline source (CSV, Parquet, …)
into a :class:`polars.DataFrame`.

.. note::

    This is intentionally distinct from the *live* market-data
    :class:`engine.data.providers.base.IDataProvider`, which fetches quotes
    and bars from remote brokers over HTTP and returns pandas frames. The two
    share a name because both express "a source of OHLCV data", but this
    interface is a small, synchronous, file-backed contract
    (:meth:`load_data` + :meth:`validate`) used by the offline backtesting /
    analysis path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from pathlib import Path

    import polars as pl

# Canonical OHLCV column set required from every historical source. The
# ``timestamp`` column is intentionally first: historical loaders index/order
# by it, unlike the live provider tuple in
# :data:`engine.data.providers.base.OHLCV_COLUMNS` (which has no timestamp
# because the pandas ``DatetimeIndex`` carries it).
OHLCV_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


class DataValidationError(ValueError):
    """Raised when a historical data source fails validation.

    Typically raised by :meth:`IDataProvider.validate` when a required OHLCV
    column is missing or the source is unreadable.
    """


class IDataProvider(ABC):
    """Contract for historical OHLCV data providers.

    Concrete implementations (e.g.
    :class:`~engine.data.csv_provider.CSVHistoricalDataProvider`) load bars
    into a :class:`polars.DataFrame` containing the canonical columns in
    :data:`OHLCV_COLUMNS`, with ``timestamp`` parsed to a polars ``Datetime``.

    Implementations should:

    * normalise column names to lowercase canonical names,
    * parse ``timestamp`` into a :class:`polars.Datetime`, and
    * return rows sorted ascending by ``timestamp``.
    """

    #: Human-readable name identifying the provider backend (e.g. ``"csv"``).
    name: str

    @abstractmethod
    def load_data(self, source: str | Path, **kwargs: Any) -> pl.DataFrame:
        """Load and return historical OHLCV bars from ``source``.

        Args:
            source: Path (or string) to the data source.
            **kwargs: Backend-specific read options forwarded to the reader.

        Returns:
            A :class:`polars.DataFrame` with the canonical OHLCV columns,
            ``timestamp`` parsed to ``Datetime``, sorted ascending.

        Raises:
            DataValidationError: If ``source`` is missing required columns or
                is unreadable.
        """

    @abstractmethod
    def validate(self, source: str | Path, **kwargs: Any) -> bool:
        """Validate that ``source`` has the required OHLCV structure.

        Args:
            source: Path (or string) to the data source.
            **kwargs: Backend-specific read options forwarded to the reader.

        Returns:
            ``True`` when ``source`` contains every required column.

        Raises:
            DataValidationError: When a required column is missing or the
                source cannot be read.
        """


__all__ = ["OHLCV_COLUMNS", "DataValidationError", "IDataProvider"]
