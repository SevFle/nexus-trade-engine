"""
Marketplace API — browse, search, install, and rate community strategies.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from engine.api.auth.dependency import get_current_user, require_role
from engine.db.models import User

router = APIRouter()


class MarketplaceEntry(BaseModel):
    id: str
    name: str
    version: str
    author: str
    description: str
    category: str
    tags: list[str] = []
    rating: float = 0.0
    downloads: int = 0
    backtest_sharpe: float | None = None
    min_capital: float = 0.0


class InstallRequest(BaseModel):
    strategy_id: str
    version: str = "latest"


@router.get("/browse")
async def browse_marketplace(
    category: str = None,
    search: str = None,
    sort_by: str = "downloads",
    page: int = 1,
    per_page: int = 20,
    user: User = Depends(get_current_user),
):
    """Browse available strategies in the marketplace."""
    # TODO: Query marketplace registry (could be remote API or local DB)
    return {
        "strategies": [],
        "total": 0,
        "page": page,
        "per_page": per_page,
        "filters": {"category": category, "search": search, "sort_by": sort_by},
    }


@router.get("/categories")
async def list_categories(user: User = Depends(get_current_user)):
    """List available strategy categories."""
    return {
        "categories": [
            {
                "id": "algorithmic",
                "name": "Fixed Algorithm",
                "description": "Deterministic rule-based strategies",
            },
            {
                "id": "ml",
                "name": "Machine Learning",
                "description": "Neural nets, ensemble models, deep learning",
            },
            {
                "id": "llm",
                "name": "LLM-Powered",
                "description": "Strategies using large language models",
            },
            {
                "id": "hybrid",
                "name": "Hybrid / Multi-Model",
                "description": "Combinations of multiple approaches",
            },
            {
                "id": "income",
                "name": "Income / Yield",
                "description": "Dividend and options income strategies",
            },
            {
                "id": "macro",
                "name": "Macro / Regime",
                "description": "Macro-driven allocation strategies",
            },
        ]
    }


@router.post("/install")
async def install_strategy(
    req: InstallRequest,
    user: User = Depends(require_role("developer")),
):
    """Install a strategy from the marketplace."""
    # TODO: Download strategy package, validate manifest, install to plugin dir
    return {
        "status": "not_implemented",
        "strategy_id": req.strategy_id,
        "message": "Marketplace installation coming soon.",
    }


@router.delete("/uninstall/{strategy_id}")
async def uninstall_strategy(
    strategy_id: str,
    user: User = Depends(require_role("developer")),
):
    """Uninstall a strategy."""
    # TODO: Deactivate, remove files, update DB
    return {"status": "not_implemented", "strategy_id": strategy_id}


@router.post("/{strategy_id}/rate")
async def rate_strategy(
    strategy_id: str,
    rating: int,
    review: str = "",
    user: User = Depends(get_current_user),
):
    """Rate and review a marketplace strategy."""
    if not 1 <= rating <= 5:
        raise HTTPException(status_code=400, detail="Rating must be 1-5")
    return {"status": "not_implemented"}
