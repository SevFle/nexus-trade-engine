"""ScoringExecutor — orchestrates multi-factor Z-score scoring for a strategy.

Given an IScoringStrategy and universe data, computes winsorized Z-scores
per factor, weights them into a composite, and returns a ranked ScoringResult.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from nexus_sdk.scoring import (
    FactorDirection,
    FactorScore,
    IScoringStrategy,
    ScoringFactor,
    ScoringResult,
    SymbolScore,
    ZScoreNormalizer,
)

if TYPE_CHECKING:
    from nexus_sdk.strategy import MarketState

logger = structlog.get_logger()

MIN_DATA_POINTS = 10


class ScoringExecutor:
    def __init__(self, strategy: IScoringStrategy, min_data_points: int = MIN_DATA_POINTS) -> None:
        self._strategy = strategy
        self._min_data_points = min_data_points

    def execute(
        self,
        _universe: list[str],
        _market: MarketState,
        _costs: Any,
    ) -> ScoringResult:
        return ScoringResult(strategy_id=self._strategy.id, scores=[])

    def compute_scores(
        self,
        universe: list[str],
        raw_data: dict[str, dict[str, float | None]],
    ) -> ScoringResult:
        factors = self._strategy.get_scoring_factors()
        excluded_factors: list[str] = []

        symbols_with_data = [s for s in universe if s in raw_data]

        factor_z_scores: dict[str, dict[str, float]] = {}
        for factor in factors:
            z_map = self._compute_factor_z_scores(factor, symbols_with_data, raw_data)
            if z_map is None:
                excluded_factors.append(factor.name)
                logger.warning(
                    "scoring.factor_excluded",
                    factor=factor.name,
                    reason="insufficient_data",
                )
            else:
                factor_z_scores[factor.name] = z_map

        if not factor_z_scores:
            logger.warning("scoring.no_valid_factors", strategy=self._strategy.id)
            return ScoringResult(
                strategy_id=self._strategy.id,
                scores=[],
                excluded_factors=excluded_factors,
            )

        total_weight = sum(f.weight for f in factors if f.name not in excluded_factors)
        if total_weight == 0:
            return ScoringResult(
                strategy_id=self._strategy.id,
                scores=[],
                excluded_factors=excluded_factors,
            )

        weight_normalizer = 1.0 / total_weight

        scores: list[SymbolScore] = []
        for symbol in symbols_with_data:
            composite = 0.0
            factor_score_map: dict[str, FactorScore] = {}
            for factor in factors:
                if factor.name in excluded_factors:
                    continue
                z = factor_z_scores.get(factor.name, {}).get(symbol, 0.0)
                if factor.direction == FactorDirection.LOWER_IS_BETTER:
                    z = -z
                composite += factor.weight * weight_normalizer * z
                raw_val = raw_data[symbol].get(factor.name)
                factor_score_map[factor.name] = FactorScore(
                    factor_name=factor.name,
                    z_score=z,
                    raw_value=raw_val,
                )

            scores.append(
                SymbolScore(
                    symbol=symbol,
                    composite_score=composite,
                    factor_scores=factor_score_map,
                )
            )

        normalizer = ZScoreNormalizer()
        composites = [s.composite_score for s in scores]
        scaled = normalizer.scale_to_range(composites, low=0.0, high=100.0)
        for i, score in enumerate(scores):
            score.composite_score = scaled[i]

        result = ScoringResult(
            strategy_id=self._strategy.id,
            scores=scores,
            excluded_factors=excluded_factors,
        )

        logger.info(
            "scoring.completed",
            strategy=self._strategy.id,
            universe_size=len(universe),
            scored=len(scores),
            excluded_factors=excluded_factors,
            top_symbol=result.scores[0].symbol if result.scores else None,
        )

        return result

    def _compute_factor_z_scores(
        self,
        factor: ScoringFactor,
        symbols: list[str],
        raw_data: dict[str, dict[str, float | None]],
    ) -> dict[str, float] | None:
        pairs: list[tuple[str, float]] = []
        for symbol in symbols:
            val = raw_data[symbol].get(factor.name)
            if val is not None:
                pairs.append((symbol, val))

        if len(pairs) < self._min_data_points:
            return None

        normalizer = ZScoreNormalizer(
            winsorize_lower=factor.winsorize_pct[0],
            winsorize_upper=factor.winsorize_pct[1],
        )
        values: list[float | None] = [v for _, v in pairs]
        z_scores = normalizer.winsorize_and_standardize(values)

        return {symbol: z for (symbol, _), z in zip(pairs, z_scores, strict=True)}
