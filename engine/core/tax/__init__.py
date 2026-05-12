"""Tax engine — runtime rules and adjustments (gh#156 onwards).

Public surface intentionally minimal. Today this exposes the wash-sale
detector; future work adds the configurable jurisdiction engine
(gh#81), the regulatory report generator (gh#155), and runtime
enforcement that integrates with the live order pipeline.
"""

from engine.core.tax.wash_sale import (
    WASH_SALE_WINDOW_DAYS,
    Trade,
    TradeSide,
    WashSaleAdjustment,
    detect_wash_sales,
    detect_wash_sales_for_jurisdiction,
)

__all__ = [
    "WASH_SALE_WINDOW_DAYS",
    "Trade",
    "TradeSide",
    "WashSaleAdjustment",
    "detect_wash_sales",
    "detect_wash_sales_for_jurisdiction",
]
