"""Multi-strategy portfolio — capital allocation, combined positions,
risk-adjusted signal merging.

A :class:`MultiStrategyPortfolio` owns three concerns that the lower-level
:class:`~engine.core.strategy_orchestrator.StrategyOrchestrator` (a pure
signal voter) deliberately does not:

1. **Capital allocation** — each registered strategy is assigned a
   ``capital_weight`` (a relative share of a fixed ``total_capital``).
   Dollar allocations are computed on demand by normalising the weights,
   so the weights need not sum to 1.0; the portfolio is the source of
   truth for *how much money* each strategy may deploy.
2. **Evaluation** — every registered strategy is invoked with the *same*
   ``market_data`` and the portfolio's :class:`~engine.core.cost_model.ICostModel`
   (per spec), each receiving an independent deep copy so a misbehaving
   plugin cannot poison its siblings or the caller's originals. A single
   failing / timed-out strategy is isolated: its error is recorded and the
   remaining strategies still contribute.
3. **Risk-adjusted signal merging** — per-symbol, the capital-weighted
   *dollar exposure* is netted across strategies (BUY = +capital·weight,
   SELL = -capital·weight). The merged side is the sign of the net; the
   merged weight is ``|net exposure| / total_capital`` clamped to ``[0, 1]``.
   This makes the merge "risk-adjusted": a strategy with more capital at
   risk moves the decision proportionally more, and the resulting weight is
   itself a measure of how much of the book is committed to the position.
   Opposing signals on the same symbol net out (a stalemate resolves to
   HOLD); HOLD signals abstain and contribute nothing.

The class owns no I/O and no global state — it is a synchronous registry
with one async entry point (:meth:`evaluate_all`).
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from engine.core.signal import Side, Signal

if TYPE_CHECKING:

    from engine.core.cost_model import ICostModel

logger = structlog.get_logger()


class MultiStrategyPortfolioError(ValueError):
    """Bad portfolio configuration: bad weight, duplicate id, unknown
    merge mode, a strategy missing its ``id`` / ``evaluate``, or a
    capital/weight value that is not a finite number."""


class SignalMergeMode(StrEnum):
    """How per-strategy signals are merged into one decision per symbol.

    ``RISK_ADJUSTED`` (the default and currently only mode) nets each
    symbol's signed capital-weighted dollar exposure: a strategy's vote is
    scaled by the dollars it has been allocated, so capital at risk — not
    raw headcount — drives the merged decision. Additional modes are
    reserved for future cycles.
    """

    RISK_ADJUSTED = "risk_adjusted"


# Signed multiplier for each side when netting exposure. HOLD abstains.
_SIDE_SIGN: dict[Side, float] = {
    Side.BUY: 1.0,
    Side.SELL: -1.0,
    Side.HOLD: 0.0,
}

# Stamped onto merged output so audit code can tell portfolio-level
# decisions apart from raw per-strategy signals.
_PORTFOLIO_STRATEGY_ID = "portfolio"

# Net-exposure dead band: |net| below this is a tie → HOLD, so float dust
# (e.g. 100.0 - 50.0 - 50.0 == 1.4e-14) cannot flip a HOLD into a phantom
# signal.
_NET_EPSILON = 1e-9


@dataclass(frozen=True)
class CombinedPosition:
    """One symbol's merged position across every contributing strategy.

    ``net_exposure`` is the signed dollar exposure (BUY positive, SELL
    negative); ``net_weight`` is ``|net_exposure| / total_capital`` clamped
    to ``[0, 1]`` — i.e. what fraction of total capital the net position
    represents. ``contributors`` lists the strategy ids that expressed a
    non-HOLD, finite-weight opinion on the symbol.
    """

    symbol: str
    side: Side
    net_weight: float
    net_exposure: float
    contributors: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)


@dataclass(frozen=True)
class PortfolioEvaluation:
    """Outcome of one :meth:`evaluate_all` cycle.

    The result is richer than a bare ``list[Signal]``: ``positions`` gives
    per-symbol combined exposure for risk reporting, ``per_strategy_signals``
    preserves full provenance for audit/traceability, and ``errors`` reports
    any strategy that raised (or timed out) so a misbehaving plugin can
    never silently disappear from the record.
    """

    signals: list[Signal] = field(default_factory=list)
    positions: dict[str, CombinedPosition] = field(default_factory=dict)
    per_strategy_signals: dict[str, list[Signal]] = field(default_factory=dict)
    total_capital: float = 0.0
    capital_deployed: float = 0.0
    net_exposure: float = 0.0
    capital_utilization: float = 0.0
    merge_mode: str = ""
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def trade_signals(self) -> list[Signal]:
        """Merged signals that express a non-HOLD intent."""
        return [s for s in self.signals if s.side != Side.HOLD]

    @property
    def is_noop(self) -> bool:
        """True when no merged signal was produced (empty registry, every
        strategy returned nothing, or total capital is zero)."""
        return len(self.signals) == 0


# --------------------------------------------------------------------- #
# Validation helpers (module-private)
# --------------------------------------------------------------------- #


def _strategy_id(strategy: Any) -> str:
    """Resolve a strategy's identifier (string attribute or no-arg
    callable) and verify it exposes a callable ``evaluate``."""
    sid = getattr(strategy, "id", None)
    if callable(sid):
        sid = sid()
    if not isinstance(sid, str) or not sid:
        raise MultiStrategyPortfolioError(
            f"strategy must expose a non-empty string `id`, got {sid!r}"
        )
    if not callable(getattr(strategy, "evaluate", None)):
        raise MultiStrategyPortfolioError(
            f"strategy {sid!r} must expose a callable `evaluate` method"
        )
    return sid


def _finite(value: float, label: str) -> float:
    """Coerce ``value`` to ``float`` and require it to be finite."""
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise MultiStrategyPortfolioError(
            f"{label} must be a number, got {value!r}"
        ) from exc
    if not math.isfinite(num):
        raise MultiStrategyPortfolioError(f"{label} must be finite, got {value!r}")
    return num


def _resolve_mode(merge_mode: object) -> SignalMergeMode:
    try:
        return SignalMergeMode(merge_mode)
    except ValueError as exc:
        valid = ", ".join(sorted(m.value for m in SignalMergeMode))
        raise MultiStrategyPortfolioError(
            f"unknown merge mode {merge_mode!r}; expected one of {valid}"
        ) from exc


# --------------------------------------------------------------------- #
# The portfolio
# --------------------------------------------------------------------- #


class MultiStrategyPortfolio:
    """A capital-aware registry of strategies that merges their signals.

    Construct once with a fixed ``total_capital`` and the portfolio's
    :class:`~engine.core.cost_model.ICostModel`, :meth:`register` the
    strategies you want to run (each with a relative ``capital_weight``),
    then call :meth:`evaluate_all` each cycle with the shared market data.
    """

    def __init__(
        self,
        total_capital: float,
        cost_model: ICostModel,
        *,
        eval_timeout: float = 30.0,
        max_strategies: int = 50,
    ) -> None:
        self._total_capital = _finite(total_capital, "total_capital")
        if self._total_capital < 0:
            raise MultiStrategyPortfolioError(
                f"total_capital must be non-negative, got {self._total_capital!r}"
            )
        self._cost_model = cost_model

        value = _finite(eval_timeout, "eval_timeout")
        if value <= 0:
            raise MultiStrategyPortfolioError(
                f"eval_timeout must be a finite, positive number, got {eval_timeout!r}"
            )
        self._eval_timeout = value

        max_n = int(max_strategies)
        if max_n < 1:
            raise MultiStrategyPortfolioError(
                f"max_strategies must be >= 1, got {max_strategies!r}"
            )
        self._max_strategies = max_n

        # Insertion-ordered so evaluate_all iterates deterministically.
        self._strategies: dict[str, Any] = {}
        self._weights: dict[str, float] = {}

    # -- introspection -------------------------------------------------

    def __len__(self) -> int:
        return len(self._strategies)

    def __contains__(self, strategy_id: object) -> bool:
        return strategy_id in self._strategies

    @property
    def total_capital(self) -> float:
        """Total deployable capital backing this portfolio."""
        return self._total_capital

    @property
    def strategy_ids(self) -> list[str]:
        """Registered strategy ids in registration order."""
        return list(self._strategies)

    @property
    def capital_weights(self) -> dict[str, float]:
        """Snapshot of the per-strategy capital weights (raw, un-normalised)."""
        return dict(self._weights)

    def get_capital_weight(self, strategy_id: str) -> float | None:
        """Raw capital weight for ``strategy_id`` or ``None`` if unregistered."""
        return self._weights.get(strategy_id)

    # -- registration --------------------------------------------------

    def register(self, strategy: Any, capital_weight: float = 1.0) -> None:
        """Register ``strategy`` with the given relative ``capital_weight``.

        ``capital_weight`` must be a finite, non-negative number. Weights
        are *relative*: they need not sum to 1.0 — dollar allocations are
        computed by normalising against the weight sum at lookup time, so
        registering ``{a: 2, b: 1}`` deploys 2/3 of capital to ``a`` and
        1/3 to ``b``. Re-registering an id is rejected (use
        :meth:`set_capital_weight` to change an existing allocation).
        """
        sid = _strategy_id(strategy)
        if len(self._strategies) >= self._max_strategies and sid not in self._strategies:
            raise MultiStrategyPortfolioError(
                f"max_strategies ({self._max_strategies}) reached; "
                f"unregister a strategy before adding {sid!r}"
            )
        weight = _finite(capital_weight, f"capital_weight[{sid!r}]")
        if weight < 0:
            raise MultiStrategyPortfolioError(
                f"capital_weight for {sid!r} must be non-negative, got {weight!r}"
            )
        if sid in self._strategies:
            raise MultiStrategyPortfolioError(f"strategy {sid!r} already registered")
        self._strategies[sid] = strategy
        self._weights[sid] = weight
        logger.info(
            "portfolio.registered", strategy_id=sid, capital_weight=weight
        )

    def set_capital_weight(self, strategy_id: str, capital_weight: float) -> None:
        """Update an already-registered strategy's capital weight."""
        if strategy_id not in self._strategies:
            raise MultiStrategyPortfolioError(
                f"cannot set weight for unknown strategy {strategy_id!r}"
            )
        weight = _finite(capital_weight, f"capital_weight[{strategy_id!r}]")
        if weight < 0:
            raise MultiStrategyPortfolioError(
                f"capital_weight for {strategy_id!r} must be non-negative, got {weight!r}"
            )
        self._weights[strategy_id] = weight
        logger.info(
            "portfolio.weight_updated",
            strategy_id=strategy_id,
            capital_weight=weight,
        )

    def unregister(self, strategy_id: str) -> bool:
        """Remove ``strategy_id``. Returns True if it was present."""
        existed = self._strategies.pop(strategy_id, None) is not None
        self._weights.pop(strategy_id, None)
        if existed:
            logger.info("portfolio.unregistered", strategy_id=strategy_id)
        return existed

    # -- capital allocation --------------------------------------------

    def _weight_sum(self) -> float:
        """Sum of all capital weights (0.0 for an empty registry)."""
        return float(sum(self._weights.values()))

    def capital_weight_normalized(self, strategy_id: str) -> float:
        """Normalised share of capital for ``strategy_id`` in ``[0, 1]``.

        Unknown strategies, or any strategy when the total weight is zero,
        resolve to ``0.0`` rather than raising, so allocation lookups are
        total — callers never have to pre-filter.
        """
        total = self._weight_sum()
        if total <= 0:
            return 0.0
        w = self._weights.get(strategy_id, 0.0)
        if w <= 0:
            return 0.0
        return w / total

    def allocation(self, strategy_id: str) -> float:
        """Dollar capital assigned to ``strategy_id``.

        Equals ``capital_weight_normalized(strategy_id) * total_capital``.
        Returns ``0.0`` for unknown strategies, when the strategy's weight
        is zero, when all weights are zero, or when ``total_capital`` is
        zero.
        """
        return self.capital_weight_normalized(strategy_id) * self._total_capital

    def allocations(self) -> dict[str, float]:
        """Dollar allocation for every registered strategy, keyed by id."""
        total = self._weight_sum()
        if total <= 0 or self._total_capital <= 0:
            return dict.fromkeys(self._strategies, 0.0)
        return {
            sid: (w / total) * self._total_capital
            for sid, w in self._weights.items()
        }

    # -- evaluation & merging ------------------------------------------

    async def evaluate_all(
        self,
        market_data: Any,
        *,
        merge_mode: str | SignalMergeMode = SignalMergeMode.RISK_ADJUSTED,
    ) -> PortfolioEvaluation:
        """Evaluate every registered strategy against the same
        ``market_data`` and the portfolio's cost model, then merge the
        resulting signals into one risk-adjusted decision per symbol.

        Parameters
        ----------
        market_data:
            Forwarded to each strategy's ``evaluate`` as an independent
            deep copy, so one strategy mutating it cannot leak to its
            siblings or the caller's original.
        merge_mode:
            One of :class:`SignalMergeMode` (currently only
            ``risk_adjusted``). Controls how per-strategy signals are
            collapsed per symbol.

        Returns
        -------
        PortfolioEvaluation
            ``signals`` holds the merged decisions; ``positions`` the
            per-symbol combined exposure; ``per_strategy_signals`` the raw
            per-strategy provenance; ``errors`` maps any failed strategy id
            (raised *or* timed out) to its error message. A strategy that
            exceeds the configured ``eval_timeout`` is reported as a
            ``TimeoutError`` entry rather than stalling the cycle.

        Raises
        ------
        MultiStrategyPortfolioError
            If ``merge_mode`` is not a known mode.
        """
        mode = _resolve_mode(merge_mode)

        # Empty registry -> no-op. Short-circuit before evaluation so
        # callers get a clean, empty result with consistent bookkeeping.
        if not self._strategies:
            return PortfolioEvaluation(
                signals=[],
                positions={},
                per_strategy_signals={},
                total_capital=self._total_capital,
                capital_deployed=0.0,
                net_exposure=0.0,
                capital_utilization=0.0,
                merge_mode=mode.value,
                errors={},
            )

        allocations = self.allocations()
        per_strategy_signals: dict[str, list[Signal]] = {}
        errors: dict[str, str] = {}

        # Snapshot the registry before iterating. A strategy that
        # registers / unregisters a sibling mid-cycle must not mutate the
        # dict we are walking. Strategies added during this cycle are
        # intentionally excluded — they run on the next one.
        for sid, strategy in list(self._strategies.items()):
            # Independent deep copies keep cross-strategy comparisons
            # apples-to-apples even if a plugin mutates its inputs.
            md = copy.deepcopy(market_data)
            cm = copy.deepcopy(self._cost_model)
            try:
                raw = strategy.evaluate(md, cm)
            except Exception as exc:
                # The synchronous call frame is never bounded by the
                # timeout — it has already returned (or raised) before the
                # cap could apply. A strategy that raises the builtin
                # ``TimeoutError`` itself lands here and is reported as
                # ``strategy_failed``, never masquerading as a timeout.
                logger.exception("portfolio.strategy_failed", strategy_id=sid)
                errors[sid] = f"{type(exc).__name__}: {exc}"
                continue
            # Support both sync (returns list) and async (returns coroutine)
            # strategies transparently. Only the awaitable result is bounded
            # by the per-strategy timeout, and the guard is kept tight
            # around ``wait_for`` alone so a builtin ``TimeoutError`` raised
            # anywhere else is not misclassified as a deadline expiry.
            if inspect.isawaitable(raw):
                try:
                    raw = await asyncio.wait_for(raw, timeout=self._eval_timeout)
                except TimeoutError:
                    logger.warning(
                        "portfolio.strategy_timeout",
                        strategy_id=sid,
                        timeout=self._eval_timeout,
                    )
                    errors[sid] = (
                        f"TimeoutError: evaluate exceeded "
                        f"{self._eval_timeout}s timeout"
                    )
                    continue
            signals = list(raw) if raw else []
            per_strategy_signals[sid] = signals

        merged, positions = self._merge(per_strategy_signals, allocations)

        # Gross capital at risk = sum of |net exposure| across symbols.
        capital_deployed = float(sum(abs(p.net_exposure) for p in positions.values()))
        net_exposure = float(sum(p.net_exposure for p in positions.values()))
        utilization = (
            capital_deployed / self._total_capital
            if self._total_capital > 0
            else 0.0
        )

        return PortfolioEvaluation(
            signals=merged,
            positions=positions,
            per_strategy_signals=per_strategy_signals,
            total_capital=self._total_capital,
            capital_deployed=capital_deployed,
            net_exposure=net_exposure,
            capital_utilization=utilization,
            merge_mode=mode.value,
            errors=errors,
        )

    # -- the merge core ------------------------------------------------

    def _merge(
        self,
        per_strategy_signals: dict[str, list[Signal]],
        allocations: dict[str, float],
    ) -> tuple[list[Signal], dict[str, CombinedPosition]]:
        """Collapse per-strategy signals into one risk-adjusted decision
        per symbol. Returns ``(merged_signals, positions)``."""
        # Group every signal by symbol, remembering which registered
        # strategy emitted it (the registry key, not sig.strategy_id, so
        # a strategy that mislabels its own signals is still allocated
        # against its real capital share).
        per_symbol: dict[str, list[tuple[str, Signal]]] = {}
        for sid, signals in per_strategy_signals.items():
            for sig in signals:
                per_symbol.setdefault(sig.symbol, []).append((sid, sig))

        merged: list[Signal] = []
        positions: dict[str, CombinedPosition] = {}
        for symbol, group in per_symbol.items():
            position = self._merge_symbol(symbol, group, allocations)
            positions[symbol] = position
            if position.side != Side.HOLD or position.net_weight > 0.0:
                # Only emit a non-trivial signal. A pure-HOLD symbol with
                # zero net weight still appears in ``positions`` (so risk
                # reports know it was considered) but is dropped from the
                # tradeable signal list to match the rest of the engine's
                # "HOLD = no action" semantics.
                merged.append(self._to_signal(position, group))

        return merged, positions

    def _merge_symbol(
        self,
        symbol: str,
        group: list[tuple[str, Signal]],
        allocations: dict[str, float],
    ) -> CombinedPosition:
        """Net the capital-weighted dollar exposure for one symbol.

        For each contributing signal: signed exposure = side_sign *
        (strategy's dollar allocation) * signal.weight. BUY adds, SELL
        subtracts, HOLD abstains. A non-finite signal weight cannot scale
        an exposure, so it abstains (matching the orchestrator's guard
        against NaN/Inf poisoning the sum).
        """
        net = 0.0
        contributors: list[str] = []
        contributor_signals: list[Signal] = []
        for sid, sig in group:
            try:
                sign = _SIDE_SIGN[sig.side]
            except (KeyError, ValueError) as exc:
                raise MultiStrategyPortfolioError(
                    f"unsupported side {sig.side!r} on {symbol!r}"
                ) from exc
            if sign == 0.0:
                continue  # HOLD abstains
            if not math.isfinite(sig.weight):
                continue  # non-finite weight -> abstention
            alloc = float(allocations.get(sid, 0.0))
            net += sign * alloc * sig.weight
            contributors.append(sid)
            contributor_signals.append(sig)

        # Resolve the merged side from the net sign; float dust within
        # _NET_EPSILON is a tie -> HOLD.
        if net > _NET_EPSILON:
            side = Side.BUY
        elif net < -_NET_EPSILON:
            side = Side.SELL
        else:
            side = Side.HOLD

        net_weight = 0.0
        if self._total_capital > 0 and side != Side.HOLD:
            net_weight = min(abs(net) / self._total_capital, 1.0)

        return CombinedPosition(
            symbol=symbol,
            side=side,
            net_weight=net_weight,
            net_exposure=net,
            contributors=contributors,
            signals=contributor_signals,
        )

    @staticmethod
    def _to_signal(
        position: CombinedPosition, group: list[tuple[str, Signal]]
    ) -> Signal:
        """Build the merged :class:`Signal` for a combined position.

        ``side``/``weight`` reflect the merged decision; ``strategy_id`` is
        overwritten to ``"portfolio"`` so the audit trail shows the merge;
        ``metadata`` is deep-copied (nested dicts/lists included) so
        downstream mutation cannot leak back to the source signals; and
        ``portfolio_contributors`` records which strategies voted so the
        decision stays auditable. The first contributing signal is used as
        the template so symbol/instrument fields propagate correctly.
        """
        template = group[0][1] if group else None
        if template is None:  # pragma: no cover - group is never empty here
            raise MultiStrategyPortfolioError(
                f"cannot build signal for {position.symbol!r}: no contributors"
            )
        metadata = dict(copy.deepcopy(template.metadata) or {})
        metadata["portfolio_contributors"] = list(position.contributors)
        return template.model_copy(
            update={
                "side": position.side,
                "weight": position.net_weight,
                "strategy_id": _PORTFOLIO_STRATEGY_ID,
                "metadata": metadata,
            }
        )


__all__ = [
    "CombinedPosition",
    "MultiStrategyPortfolio",
    "MultiStrategyPortfolioError",
    "PortfolioEvaluation",
    "SignalMergeMode",
]
