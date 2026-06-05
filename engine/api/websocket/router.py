"""WebSocket router (SEV-275).

Adds the new endpoints alongside the legacy ``/api/v1/ws`` route:

- ``/api/v1/ws/v2``         — unified multiplexed stream
- ``/api/v1/ws/portfolio``  — pre-bound portfolio stream
- ``/api/v1/ws/orders``     — pre-bound orders stream
- ``/api/v1/ws/market``     — market data stream (ticks + depth)

All four endpoints share the handler implementation in
:mod:`engine.api.websocket.handlers`; only the allowed channel
families differ.
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket

from engine.api.websocket.connection_manager_v2 import get_manager_v2
from engine.api.websocket.handlers import (
    serve_market,
    serve_orders,
    serve_portfolio,
    serve_unified,
)

router = APIRouter()


@router.websocket("/ws/v2")
async def ws_v2_endpoint(ws: WebSocket) -> None:
    await serve_unified(ws, get_manager_v2())


@router.websocket("/ws/portfolio")
async def ws_portfolio_endpoint(ws: WebSocket) -> None:
    await serve_portfolio(ws, get_manager_v2())


@router.websocket("/ws/orders")
async def ws_orders_endpoint(ws: WebSocket) -> None:
    await serve_orders(ws, get_manager_v2())


@router.websocket("/ws/market")
async def ws_market_endpoint(ws: WebSocket) -> None:
    await serve_market(ws, get_manager_v2())


__all__ = ["router"]
