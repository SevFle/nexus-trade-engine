"""Canonical :class:`AssetType` taxonomy for the Nexus engine.

This module is the lightweight, dependency-free contract layer for
Multi-Asset Support. It defines the public ``AssetType`` enum (the
user-facing classification: *stock, option, future, forex, crypto, etf*)
plus a small set of coercion helpers:

* :meth:`AssetType.from_string` — parse a free-form string into the enum
  (case-insensitive, tolerant of an already-typed value).
* :meth:`AssetType.from_asset_class` — bridge from the internal
  ``InstrumentAssetClass`` taxonomy (data-routing) to the public
  ``AssetType`` taxonomy.
* :meth:`AssetType.from_instrument` — inspect any instrument-like object
  (has ``asset_type`` and/or ``asset_class`` attributes) and report its
  ``AssetType``. Handles the ``None`` sentinel used by
  :class:`engine.core.instruments.Instrument`.

It is deliberately kept free of heavy engine imports so that any
subsystem — data providers, the MCP server, the portfolio manager, the
SDK — can import the canonical asset-type contract without pulling in
the whole engine graph. The reverse dependency (Instrument → AssetType)
lives in :mod:`engine.core.instruments`; to avoid an import cycle this
module only references ``Instrument``/``InstrumentAssetClass`` under
``TYPE_CHECKING`` and does the runtime import lazily inside
:meth:`AssetType.from_instrument`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class AssetType(StrEnum):
    """Public, user-facing classification of a tradable asset.

    This is intentionally coarser than the internal
    ``InstrumentAssetClass`` taxonomy: callers that only need to know
    "is this a stock / option / future / forex / crypto / etf?" should
    use ``AssetType``. Anything that needs data-routing granularity
    (crypto spot vs. perpetual vs. dated future) should keep using
    ``InstrumentAssetClass``.

    The string values are stable identifiers used in serialization, API
    payloads, and the MCP tool surface — do **not** rename them.
    """

    STOCK = "stock"
    OPTION = "option"
    FUTURE = "future"
    FOREX = "forex"
    CRYPTO = "crypto"
    ETF = "etf"

    # ── Coercion helpers ────────────────────────────────────────────

    @classmethod
    def from_string(cls, value: Any) -> AssetType:
        """Parse a string (or already-typed value) into an :class:`AssetType`.

        Accepts:

        * an :class:`AssetType` instance — returned unchanged;
        * any :class:`str` — matched **case-insensitively** and after
          stripping surrounding whitespace, so ``"Stock"``, ``" STOCK "``,
          and ``"stock"`` all resolve to :attr:`AssetType.STOCK`.

        Raises :class:`ValueError` (with the set of valid values) when
        the input does not match a known asset type, or :class:`TypeError`
        when the input is neither a string nor an :class:`AssetType`.
        """
        if isinstance(value, AssetType):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            try:
                return cls(normalized)
            except ValueError:
                valid = ", ".join(sorted(member.value for member in cls))
                msg = f"unknown asset type {value!r}; expected one of: {valid}"
                raise ValueError(msg) from None
        msg = f"cannot parse AssetType from {type(value).__name__}; expected str or AssetType"
        raise TypeError(msg)

    @classmethod
    def from_asset_class(cls, asset_class: Any) -> AssetType:
        """Map an internal ``InstrumentAssetClass`` to the public ``AssetType``.

        Accepts the enum member itself or its string value
        (``"equity"``, ``"crypto_perp"``, …). Multiple internal crypto
        flavors (spot / perpetual / dated future) collapse onto the
        single public :attr:`AssetType.CRYPTO` class because callers at
        this taxonomy level do not need to distinguish them.

        Raises :class:`ValueError` for an unrecognized asset class.
        """
        _class_to_type: dict[str, AssetType] = {
            "equity": cls.STOCK,
            "etf": cls.ETF,
            "option": cls.OPTION,
            "future": cls.FUTURE,
            "forex": cls.FOREX,
            "crypto": cls.CRYPTO,
            "crypto_perp": cls.CRYPTO,
            "crypto_future": cls.CRYPTO,
        }
        normalized = str(asset_class).strip().lower()
        try:
            return _class_to_type[normalized]
        except KeyError:
            valid = ", ".join(sorted(_class_to_type))
            msg = f"unsupported asset_class {asset_class!r}; expected one of: {valid}"
            raise ValueError(msg) from None

    @classmethod
    def from_instrument(cls, instrument: Any) -> AssetType:
        """Derive the :class:`AssetType` for an instrument-like object.

        Resolution order:

        1. If the object exposes an ``asset_type`` attribute that is not
           ``None`` and parses to a valid :class:`AssetType`, return it.
           A ``None`` value is the sentinel meaning "user did not
           provide one", so it is skipped rather than treated as the
           answer. This is the canonical path for
           :class:`engine.core.instruments.Instrument` instances created
           after the ``asset_type`` field was introduced.
        2. Otherwise, if it exposes an ``asset_class`` attribute, bridge
           via :meth:`from_asset_class`. This keeps legacy / minimal
           instrument-like objects (e.g. data-provider DTOs that only
           carry a routing class) usable.

        Raises :class:`ValueError` when neither attribute yields a
        usable classification.
        """
        asset_type = getattr(instrument, "asset_type", None)
        # ``None`` is the explicit sentinel for "not provided" — fall
        # through to the asset_class bridge instead of returning None.
        if asset_type is not None:
            try:
                return cls.from_string(asset_type)
            except (ValueError, TypeError):
                pass  # fall through to asset_class bridge

        asset_class = getattr(instrument, "asset_class", None)
        if asset_class is not None:
            return cls.from_asset_class(asset_class)

        msg = (
            f"cannot determine AssetType from {instrument!r}: "
            f"no resolvable asset_type / asset_class attribute"
        )
        raise ValueError(msg)


__all__ = ["AssetType"]
