"""Sector / industry / asset-class classification utilities.

Includes:
- A skeletal GICS hierarchy (subset — full table lands with the GICS
  ingestion adapter) and ``is_valid_gics_path`` for cross-level checks.
- Crypto taxonomy lookup (L1, L2, DeFi, stablecoin, meme, …).
- Forex pair classification (major / minor / exotic).
"""

from __future__ import annotations

# Representative GICS subset — enough for meaningful rejection. The
# ingestion job will load the canonical table from MSCI's published
# codebook in the follow-up issue.
_GICS_HIERARCHY: dict[str, dict[str, dict[str, list[str]]]] = {
    "Information Technology": {
        "Software & Services": {
            "Software": [
                "Application Software",
                "Systems Software",
            ],
            "IT Services": [
                "IT Consulting & Other Services",
                "Internet Services & Infrastructure",
            ],
        },
        "Technology Hardware & Equipment": {
            "Technology Hardware, Storage & Peripherals": [
                "Technology Hardware, Storage & Peripherals",
            ],
            "Communications Equipment": ["Communications Equipment"],
        },
        "Semiconductors & Semiconductor Equipment": {
            "Semiconductors & Semiconductor Equipment": [
                "Semiconductors",
                "Semiconductor Materials & Equipment",
            ],
        },
    },
    "Health Care": {
        "Pharmaceuticals, Biotechnology & Life Sciences": {
            "Pharmaceuticals": ["Pharmaceuticals"],
            "Biotechnology": ["Biotechnology"],
        },
    },
    "Financials": {
        "Banks": {"Banks": ["Diversified Banks", "Regional Banks"]},
    },
}


def is_valid_gics_path(
    sector: str,
    industry_group: str,
    industry: str,
    sub_industry: str,
) -> bool:
    """Return True iff the four-level GICS path rolls up cleanly."""
    igs = _GICS_HIERARCHY.get(sector)
    if igs is None:
        return False
    inds = igs.get(industry_group)
    if inds is None:
        return False
    subs = inds.get(industry)
    if subs is None:
        return False
    return sub_industry in subs


_CRYPTO_TAXONOMY: dict[str, str] = {
    "BTC": "l1",
    "ETH": "l1",
    "SOL": "l1",
    "ADA": "l1",
    "AVAX": "l1",
    "DOT": "l1",
    "ARB": "l2",
    "OP": "l2",
    "MATIC": "l2",
    "USDT": "stablecoin",
    "USDC": "stablecoin",
    "DAI": "stablecoin",
    "BUSD": "stablecoin",
    "UNI": "defi",
    "AAVE": "defi",
    "MKR": "defi",
    "DOGE": "meme",
    "SHIB": "meme",
    "PEPE": "meme",
}


def crypto_taxonomy(symbol: str) -> str:
    """Return canonical crypto class for a base symbol (or 'unknown')."""
    return _CRYPTO_TAXONOMY.get(symbol.upper(), "unknown")


_FX_MAJORS = frozenset(
    {
        ("EUR", "USD"),
        ("USD", "JPY"),
        ("GBP", "USD"),
        ("USD", "CHF"),
        ("AUD", "USD"),
        ("USD", "CAD"),
        ("NZD", "USD"),
    }
)
_FX_MINORS_BASES = frozenset({"EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"})


def forex_pair_class(base: str, quote: str) -> str:
    """Classify a forex pair as 'major', 'minor', or 'exotic'."""
    base_u = base.upper()
    quote_u = quote.upper()
    if (base_u, quote_u) in _FX_MAJORS or (quote_u, base_u) in _FX_MAJORS:
        return "major"
    if base_u in _FX_MINORS_BASES and quote_u in _FX_MINORS_BASES:
        return "minor"
    return "exotic"


__all__ = [
    "crypto_taxonomy",
    "forex_pair_class",
    "is_valid_gics_path",
]
