"""Tool dispatch — routes an MCP ``tools/call`` to the right adapter.

The dispatcher is transport-agnostic: given a tool name, validated arguments,
an :class:`~engine.mcp.auth.AuthPrincipal`, and the
:class:`~engine.mcp.adapters.EngineServices`, it invokes the adapter,
applies pagination to list-heavy responses, and runs the result through the
:class:`~engine.mcp.pagination.ResultGuard` so no response can blow out the
assistant's context window.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from engine.mcp.adapters import EngineServices
from engine.mcp.errors import MCPError, ValidationError, map_engine_exception
from engine.mcp.pagination import ResultGuard, page_to_dict, paginate
from engine.mcp.tool_definitions import get_tool

if TYPE_CHECKING:
    from engine.mcp.auth import AuthPrincipal
    from engine.mcp.progress import ProgressReporter

# Adapter functions are async (services, principal, arguments) -> dict.
AdapterFunc = Callable[
    [EngineServices, "AuthPrincipal", dict[str, Any]],
    Awaitable[dict[str, Any]],
]


async def _run_backtest(
    services: EngineServices,
    principal: AuthPrincipal,
    arguments: dict[str, Any],
    progress: ProgressReporter | None = None,
) -> dict[str, Any]:
    from engine.mcp.adapters.backtest_adapter import run_backtest

    return await run_backtest(services, principal, arguments, progress=progress)


# Tools whose primary payload is a list that should be cursor-paginated.
_PAGINATED_KEYS: dict[str, str] = {
    "get_orders": "orders",
    "get_market_data": "bars",
    "list_strategies": "strategies",
    "search_strategies": "strategies",
}

_ADAPTERS: dict[str, AdapterFunc] = {}


def _register(name: str) -> Callable[[AdapterFunc], AdapterFunc]:
    def decorator(func: AdapterFunc) -> AdapterFunc:
        _ADAPTERS[name] = func
        return func

    return decorator


@_register("get_portfolio_status")
async def _portfolio_status(services, principal, arguments):  # type: ignore[no-untyped-def]
    from engine.mcp.adapters.portfolio_adapter import get_portfolio_status

    return await get_portfolio_status(services, principal, arguments)


@_register("get_positions")
async def _positions(services, principal, arguments):  # type: ignore[no-untyped-def]
    from engine.mcp.adapters.portfolio_adapter import get_positions

    return await get_positions(services, principal, arguments)


@_register("get_orders")
async def _orders(services, principal, arguments):  # type: ignore[no-untyped-def]
    from engine.mcp.adapters.portfolio_adapter import get_orders

    return await get_orders(services, principal, arguments)


@_register("list_strategies")
async def _list_strategies(services, principal, arguments):  # type: ignore[no-untyped-def]
    from engine.mcp.adapters.strategy_adapter import list_strategies

    return await list_strategies(services, principal, arguments)


@_register("get_strategy_details")
async def _strategy_details(services, principal, arguments):  # type: ignore[no-untyped-def]
    from engine.mcp.adapters.strategy_adapter import get_strategy_details

    return await get_strategy_details(services, principal, arguments)


@_register("search_strategies")
async def _search_strategies(services, principal, arguments):  # type: ignore[no-untyped-def]
    from engine.mcp.adapters.strategy_adapter import search_strategies

    return await search_strategies(services, principal, arguments)


@_register("get_market_data")
async def _market_data(services, principal, arguments):  # type: ignore[no-untyped-def]
    from engine.mcp.adapters.market_data_adapter import get_market_data

    return await get_market_data(services, principal, arguments)


@_register("get_cost_model")
async def _cost_model(services, principal, arguments):  # type: ignore[no-untyped-def]
    from engine.mcp.adapters.market_data_adapter import get_cost_model

    return await get_cost_model(services, principal, arguments)


@_register("get_performance_metrics")
async def _perf_metrics(services, principal, arguments):  # type: ignore[no-untyped-def]
    from engine.mcp.adapters.market_data_adapter import get_performance_metrics

    return await get_performance_metrics(services, principal, arguments)


def _validate_arguments(tool_name: str, arguments: Any) -> dict[str, Any]:
    """Lightweight argument validation against the tool's JSON Schema.

    The MCP SDK already validates inputs against ``inputSchema`` before
    dispatch when ``validate_input=True``; this is a defence-in-depth check
    that guarantees a dict payload and required-field presence even if a
    caller bypasses SDK validation (e.g. direct dispatch in tests).
    """
    if not isinstance(arguments, dict):
        raise ValidationError(f"Arguments to {tool_name!r} must be a JSON object")
    definition = get_tool(tool_name)
    if definition is None:
        return arguments
    for required in definition.input_schema.get("required", []):
        if required not in arguments:
            raise ValidationError(f"Missing required argument: {required!r}")
    return arguments


async def dispatch_tool(
    name: str,
    arguments: dict[str, Any],
    services: EngineServices,
    principal: AuthPrincipal,
    progress: ProgressReporter | None = None,
) -> dict[str, Any]:
    """Execute a tool by name and return a JSON-serialisable result dict.

    Raises :class:`~engine.mcp.errors.MCPError` subclasses for expected
    failures; unexpected engine exceptions are normalised via
    :func:`~engine.mcp.errors.map_engine_exception`.
    """
    if get_tool(name) is None:
        raise ValidationError(f"Unknown tool: {name!r}")

    arguments = _validate_arguments(name, arguments)

    try:
        if name == "run_backtest":
            result = await _run_backtest(services, principal, arguments, progress=progress)
        else:
            adapter = _ADAPTERS.get(name)
            if adapter is None:
                raise ValidationError(f"Tool {name!r} is registered but has no adapter")
            result = await adapter(services, principal, arguments)
    except MCPError:
        raise
    except Exception as exc:
        raise map_engine_exception(exc) from exc

    # Apply cursor pagination to list-heavy tools.
    list_key = _PAGINATED_KEYS.get(name)
    if list_key and isinstance(result.get(list_key), list):
        page = paginate(
            result[list_key],
            cursor=arguments.get("cursor"),
            limit=arguments.get("limit"),
        )
        result = {k: v for k, v in result.items() if k != list_key}
        result.update(page_to_dict(page, items_key=list_key))

    return ResultGuard().guard(result)


__all__ = ["dispatch_tool"]
