"""
Portfolio API routes.
"""

from db.models import PortfolioRecord, PositionRecord
from db.session import get_db
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
    id: int
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
async def create_portfolio(req: CreatePortfolioRequest, db: AsyncSession = Depends(get_db)):
    portfolio = PortfolioRecord(
        user_id=1,  # TODO: get from auth
        name=req.name,
        mode=req.mode,
        initial_cash=req.initial_cash,
        current_cash=req.initial_cash,
        total_value=req.initial_cash,
    )
    db.add(portfolio)
    await db.flush()
    await db.refresh(portfolio)
    return portfolio


@router.get("/", response_model=list[PortfolioResponse])
async def list_portfolios(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PortfolioRecord).where(PortfolioRecord.user_id == 1, PortfolioRecord.is_active == True)
    )
    return result.scalars().all()


@router.get("/{portfolio_id}", response_model=PortfolioResponse)
async def get_portfolio(portfolio_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PortfolioRecord).where(PortfolioRecord.id == portfolio_id))
    portfolio = result.scalar_one_or_none()
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return portfolio


@router.get("/{portfolio_id}/positions")
async def get_positions(portfolio_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PositionRecord).where(PositionRecord.portfolio_id == portfolio_id)
    )
    positions = result.scalars().all()
    return [
        {
            "symbol": p.symbol,
            "quantity": p.quantity,
            "avg_cost": p.avg_cost,
            "current_price": p.current_price,
            "unrealized_pnl": p.unrealized_pnl,
            "market_value": p.quantity * p.current_price,
        }
        for p in positions
    ]


@router.delete("/{portfolio_id}")
async def archive_portfolio(portfolio_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PortfolioRecord).where(PortfolioRecord.id == portfolio_id))
    portfolio = result.scalar_one_or_none()
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    portfolio.is_active = False
    return {"status": "archived", "id": portfolio_id}
