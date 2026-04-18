"""
Portfolio API routes.
"""

from engine.api.auth.dependency import get_current_user
from engine.db.models import User
from engine.deps import get_db
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


class CreatePortfolioRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    initial_cash: float = Field(default=100_000.0, ge=0)
    mode: str = Field(default="paper", pattern="^(backtest|paper|live)$")


class PortfolioResponse(BaseModel):
    id: str
    name: str
    mode: str
    initial_cash: float
    current_cash: float
    total_value: float
    realized_pnl: float
    is_active: bool

    class Config:
        from_attributes = True


@router.post("/", response_model=PortfolioResponse)
async def create_portfolio(
    req: CreatePortfolioRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from engine.db.models import Portfolio, Position

    portfolio = Portfolio(
        user_id=user.id,
        name=req.name,
        initial_capital=req.initial_cash,
    )
    db.add(portfolio)
    await db.flush()
    await db.refresh(portfolio)
    return PortfolioResponse(
        id=str(portfolio.id),
        name=portfolio.name,
        mode="paper",
        initial_cash=float(portfolio.initial_capital),
        current_cash=float(portfolio.initial_capital),
        total_value=float(portfolio.initial_capital),
        realized_pnl=0.0,
        is_active=True,
    )


@router.get("/", response_model=list[PortfolioResponse])
async def list_portfolios(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from engine.db.models import Portfolio

    result = await db.execute(select(Portfolio).where(Portfolio.user_id == user.id))
    portfolios = result.scalars().all()
    return [
        PortfolioResponse(
            id=str(p.id),
            name=p.name,
            mode="paper",
            initial_cash=float(p.initial_capital),
            current_cash=float(p.initial_capital),
            total_value=float(p.initial_capital),
            realized_pnl=0.0,
            is_active=True,
        )
        for p in portfolios
    ]


@router.get("/{portfolio_id}")
async def get_portfolio(
    portfolio_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from engine.db.models import Portfolio

    result = await db.execute(select(Portfolio).where(Portfolio.id == portfolio_id))
    portfolio = result.scalar_one_or_none()
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if portfolio.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    return {"id": str(portfolio.id), "name": portfolio.name}


@router.delete("/{portfolio_id}")
async def archive_portfolio(
    portfolio_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from engine.db.models import Portfolio

    result = await db.execute(select(Portfolio).where(Portfolio.id == portfolio_id))
    portfolio = result.scalar_one_or_none()
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if portfolio.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    return {"status": "archived", "id": portfolio_id}
