from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.api.auth.dependency import get_current_user
from engine.db.models import Portfolio, User
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
