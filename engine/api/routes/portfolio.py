from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.dependency import get_current_user
from engine.db.models import InstalledStrategy, Portfolio, Position, User
from engine.deps import get_db
from engine.legal.dependencies import require_legal_acceptance

router = APIRouter(dependencies=[Depends(require_legal_acceptance)])


class CreatePortfolioRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="")
    initial_capital: float = Field(default=100_000.0, ge=0)


class PortfolioResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    name: str
    description: str
    initial_capital: float
    created_at: str


@router.post("/", response_model=PortfolioResponse)
async def create_portfolio(
    req: CreatePortfolioRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from decimal import Decimal

    portfolio = Portfolio(
        user_id=user.id,
        name=req.name,
        description=req.description,
        initial_capital=Decimal(str(req.initial_capital)),
    )
    db.add(portfolio)
    await db.flush()
    await db.refresh(portfolio)
    return PortfolioResponse(
        id=str(portfolio.id),
        name=portfolio.name,
        description=portfolio.description,
        initial_capital=float(portfolio.initial_capital),
        created_at=portfolio.created_at.isoformat(),
    )


@router.get("/", response_model=list[PortfolioResponse])
async def list_portfolios(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Portfolio).where(Portfolio.user_id == user.id))
    portfolios = result.scalars().all()
    return [
        PortfolioResponse(
            id=str(p.id),
            name=p.name,
            description=p.description,
            initial_capital=float(p.initial_capital),
            created_at=p.created_at.isoformat(),
        )
        for p in portfolios
    ]


class PortfolioSummaryResponse(BaseModel):
    """Aggregate portfolio overview for the dashboard.

    ``total_value`` is deployed capital plus unrealised P&L across open
    positions. ``total_pnl`` / ``total_pnl_pct`` are the unrealised P&L —
    the engine does not yet persist an intraday baseline, so this is the
    best available P&L signal for the overview card. ``active_strategies``
    counts installed strategies flagged ``is_active`` across the caller's
    portfolios.
    """

    total_value: float
    total_pnl: float
    total_pnl_pct: float
    active_strategies: int
    open_positions: int
    currency: str = "USD"
    as_of: str


@router.get("/summary", response_model=PortfolioSummaryResponse)
async def get_portfolio_summary(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return an aggregate portfolio overview for the authenticated user.

    Aggregates across *all* of the user's portfolios:

    * total deployed capital (``Portfolio.initial_capital``)
    * unrealised P&L from open positions (market value - cost basis)
    * count of open (non-zero-quantity) positions
    * count of installed strategies flagged ``is_active``

    Users with no portfolios get a zeroed summary rather than a 404, so the
    dashboard renders a sensible empty state on first run.
    """
    portfolios = (
        await db.execute(select(Portfolio).where(Portfolio.user_id == user.id))
    ).scalars().all()

    portfolio_ids = [p.id for p in portfolios]

    positions: list[Position] = []
    if portfolio_ids:
        positions = (
            await db.execute(
                select(Position).where(Position.portfolio_id.in_(portfolio_ids))
            )
        ).scalars().all()

    total_capital = Decimal("0")
    for p in portfolios:
        total_capital += p.initial_capital or Decimal("0")

    total_cost_basis = Decimal("0")
    total_market_value = Decimal("0")
    open_positions = 0
    for pos in positions:
        qty = pos.quantity or Decimal("0")
        if qty == 0:
            continue
        open_positions += 1
        total_cost_basis += qty * (pos.avg_entry_price or Decimal("0"))
        total_market_value += qty * (pos.current_price or Decimal("0"))

    unrealized_pnl = total_market_value - total_cost_basis
    total_value = total_capital + unrealized_pnl
    pnl_pct = (
        float(unrealized_pnl / total_cost_basis * Decimal("100"))
        if total_cost_basis != 0
        else 0.0
    )

    active_strategies = 0
    if portfolio_ids:
        active_rows = (
            await db.execute(
                select(InstalledStrategy).where(
                    InstalledStrategy.portfolio_id.in_(portfolio_ids),
                    InstalledStrategy.is_active.is_(True),
                )
            )
        ).scalars().all()
        active_strategies = len(active_rows)

    return PortfolioSummaryResponse(
        total_value=float(total_value),
        total_pnl=float(unrealized_pnl),
        total_pnl_pct=pnl_pct,
        active_strategies=active_strategies,
        open_positions=open_positions,
        currency="USD",
        as_of=datetime.now(UTC).isoformat(),
    )


@router.get("/{portfolio_id}", response_model=PortfolioResponse)
async def get_portfolio(
    portfolio_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    import uuid

    try:
        pid = uuid.UUID(portfolio_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid portfolio ID")

    result = await db.execute(select(Portfolio).where(Portfolio.id == pid))
    portfolio = result.scalar_one_or_none()
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if portfolio.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    return PortfolioResponse(
        id=str(portfolio.id),
        name=portfolio.name,
        description=portfolio.description,
        initial_capital=float(portfolio.initial_capital),
        created_at=portfolio.created_at.isoformat(),
    )


@router.delete("/{portfolio_id}")
async def archive_portfolio(
    portfolio_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    import uuid

    try:
        pid = uuid.UUID(portfolio_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid portfolio ID")

    result = await db.execute(select(Portfolio).where(Portfolio.id == pid))
    portfolio = result.scalar_one_or_none()
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if portfolio.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    await db.delete(portfolio)
    return {"status": "deleted", "id": portfolio_id}
