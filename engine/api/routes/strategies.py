"""
Strategy management API routes — install, configure, activate, monitor.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from engine.api.auth.dependency import get_current_user
from engine.db.models import User
from engine.legal.dependencies import require_legal_acceptance

router = APIRouter(dependencies=[Depends(require_legal_acceptance)])


class StrategyConfigRequest(BaseModel):
    params: dict = Field(default_factory=dict)


@router.get("/")
async def list_strategies(request: Request, user: User = Depends(get_current_user)):
    """List all installed strategies and their status."""
    registry = request.app.state.plugin_registry
    return {"strategies": registry.list_all()}


@router.get("/{strategy_id}")
async def get_strategy(strategy_id: str, request: Request, user: User = Depends(get_current_user)):
    """Get details for a specific strategy."""
    registry = request.app.state.plugin_registry
    entry = registry.get(strategy_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")
    return {
        "id": entry.manifest.id,
        "name": entry.manifest.name,
        "version": entry.manifest.version,
        "author": entry.manifest.author,
        "description": entry.manifest.description,
        "config_schema": entry.manifest.config_schema,
        "data_feeds": entry.manifest.data_feeds,
        "watchlist": entry.manifest.watchlist,
        "requires_network": entry.manifest.requires_network(),
        "requires_gpu": entry.manifest.requires_gpu(),
        "is_loaded": entry.is_loaded,
    }


@router.post("/{strategy_id}/activate")
async def activate_strategy(
    strategy_id: str,
    config: StrategyConfigRequest,
    request: Request,
    user: User = Depends(get_current_user),
):
    """Initialize and activate a strategy with given configuration."""
    registry = request.app.state.plugin_registry
    entry = registry.get(strategy_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")

    try:
        from plugins.sdk import StrategyConfig

        strategy_config = StrategyConfig(
            strategy_id=strategy_id,
            params=config.params,
        )
        instance = await entry.instantiate(strategy_config)
        return {
            "status": "activated",
            "strategy_id": strategy_id,
            "name": instance.name,
            "version": instance.version,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to activate: {e!s}")


@router.post("/{strategy_id}/deactivate")
async def deactivate_strategy(
    strategy_id: str,
    request: Request,
    user: User = Depends(get_current_user),
):
    """Deactivate and unload a strategy."""
    registry = request.app.state.plugin_registry
    await registry.unload(strategy_id)
    return {"status": "deactivated", "strategy_id": strategy_id}


@router.post("/{strategy_id}/reload")
async def reload_strategy(
    strategy_id: str,
    request: Request,
    user: User = Depends(get_current_user),
):
    """Hot-reload a strategy from disk."""
    registry = request.app.state.plugin_registry
    success = await registry.reload(strategy_id)
    if not success:
        raise HTTPException(status_code=500, detail="Reload failed")
    return {"status": "reloaded", "strategy_id": strategy_id}


@router.get("/{strategy_id}/health")
async def strategy_health(
    strategy_id: str,
    request: Request,
    user: User = Depends(get_current_user),
):
    """Get runtime health metrics for an active strategy."""
    registry = request.app.state.plugin_registry
    entry = registry.get(strategy_id)
    if not entry or not entry.is_loaded:
        raise HTTPException(status_code=404, detail="Strategy not active")
    # Sandbox metrics would come from the sandbox wrapper
    return {"strategy_id": strategy_id, "is_loaded": entry.is_loaded}
