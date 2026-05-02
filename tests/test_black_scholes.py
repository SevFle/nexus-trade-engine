"""Unit tests for the Black-Scholes pricer + Greeks (gh#83)."""

from __future__ import annotations

import math

import pytest

from engine.core.options import (
    Greeks,
    OptionType,
    bs_greeks,
    bs_price,
    implied_volatility,
)


# ---------------------------------------------------------------------------
# Pricing — golden values
# ---------------------------------------------------------------------------
#
# Reference: Hull (10th ed.), Table 15.4 / Example 15.6.
# S=42, K=40, T=0.5, r=0.10, sigma=0.20 → Call ≈ 4.7594, Put ≈ 0.8086.


class TestPriceGoldens:
    def test_hull_example_call(self):
        price = bs_price(
            option_type=OptionType.CALL,
            S=42.0,
            K=40.0,
            T=0.5,
            r=0.10,
            sigma=0.20,
        )
        assert price == pytest.approx(4.7594, abs=1e-3)

    def test_hull_example_put(self):
        price = bs_price(
            option_type=OptionType.PUT,
            S=42.0,
            K=40.0,
            T=0.5,
            r=0.10,
            sigma=0.20,
        )
        assert price == pytest.approx(0.8086, abs=1e-3)

    def test_atm_call_no_drift_equals_n(self):
        # ATM, r = q = 0, sigma=0.2, T=1 → call = S * (2N(sigma/2) - 1).
        S = 100.0
        sigma = 0.20
        T = 1.0
        expected = S * (
            2 * (0.5 * (1 + math.erf((sigma / 2) / math.sqrt(2)))) - 1
        )
        price = bs_price(
            option_type=OptionType.CALL,
            S=S,
            K=S,
            T=T,
            r=0.0,
            sigma=sigma,
        )
        assert price == pytest.approx(expected, rel=1e-9)


class TestPutCallParity:
    def test_parity_holds(self):
        # C - P = S * exp(-qT) - K * exp(-rT)
        S, K, T, r, q, sigma = 100.0, 95.0, 0.75, 0.04, 0.01, 0.30
        c = bs_price(option_type=OptionType.CALL, S=S, K=K, T=T, r=r, sigma=sigma, q=q)
        p = bs_price(option_type=OptionType.PUT, S=S, K=K, T=T, r=r, sigma=sigma, q=q)
        rhs = S * math.exp(-q * T) - K * math.exp(-r * T)
        assert c - p == pytest.approx(rhs, abs=1e-9)


class TestEdgeCases:
    def test_t_zero_call_intrinsic(self):
        assert bs_price(
            option_type=OptionType.CALL, S=110, K=100, T=0, r=0.05, sigma=0.2
        ) == 10.0
        assert bs_price(
            option_type=OptionType.CALL, S=90, K=100, T=0, r=0.05, sigma=0.2
        ) == 0.0

    def test_t_zero_put_intrinsic(self):
        assert bs_price(
            option_type=OptionType.PUT, S=90, K=100, T=0, r=0.05, sigma=0.2
        ) == 10.0

    def test_zero_underlying_call_zero(self):
        assert bs_price(
            option_type=OptionType.CALL, S=0, K=100, T=1.0, r=0.05, sigma=0.2
        ) == 0.0

    def test_zero_underlying_put_discounted_strike(self):
        K, T, r = 100.0, 1.0, 0.05
        expected = K * math.exp(-r * T)
        assert bs_price(
            option_type=OptionType.PUT, S=0, K=K, T=T, r=r, sigma=0.2
        ) == pytest.approx(expected, abs=1e-12)

    def test_negative_inputs_rejected(self):
        with pytest.raises(ValueError):
            bs_price(
                option_type=OptionType.CALL, S=-1, K=100, T=1, r=0, sigma=0.2
            )
        with pytest.raises(ValueError):
            bs_price(
                option_type=OptionType.CALL, S=100, K=100, T=1, r=0, sigma=0
            )


# ---------------------------------------------------------------------------
# Greeks
# ---------------------------------------------------------------------------


class TestGreeks:
    def test_call_delta_in_range(self):
        g = bs_greeks(
            option_type=OptionType.CALL,
            S=100, K=100, T=0.5, r=0.05, sigma=0.20,
        )
        assert 0.0 <= g.delta <= 1.0

    def test_put_delta_in_range(self):
        g = bs_greeks(
            option_type=OptionType.PUT,
            S=100, K=100, T=0.5, r=0.05, sigma=0.20,
        )
        assert -1.0 <= g.delta <= 0.0

    def test_call_minus_put_delta_equals_disc_q(self):
        # delta_call - delta_put = exp(-qT)
        S, K, T, r, q, sigma = 100, 100, 0.5, 0.05, 0.02, 0.20
        gc = bs_greeks(
            option_type=OptionType.CALL, S=S, K=K, T=T, r=r, sigma=sigma, q=q
        )
        gp = bs_greeks(
            option_type=OptionType.PUT, S=S, K=K, T=T, r=r, sigma=sigma, q=q
        )
        assert gc.delta - gp.delta == pytest.approx(math.exp(-q * T), abs=1e-9)

    def test_call_put_gamma_equal(self):
        # Gamma is the same for calls and puts.
        kw = dict(S=100, K=100, T=0.5, r=0.05, sigma=0.20)
        gc = bs_greeks(option_type=OptionType.CALL, **kw)
        gp = bs_greeks(option_type=OptionType.PUT, **kw)
        assert gc.gamma == pytest.approx(gp.gamma, abs=1e-12)

    def test_vega_positive(self):
        g = bs_greeks(
            option_type=OptionType.CALL,
            S=100, K=100, T=0.5, r=0.05, sigma=0.20,
        )
        assert g.vega > 0

    def test_call_rho_positive_put_rho_negative(self):
        kw = dict(S=100, K=100, T=0.5, r=0.05, sigma=0.20)
        gc = bs_greeks(option_type=OptionType.CALL, **kw)
        gp = bs_greeks(option_type=OptionType.PUT, **kw)
        assert gc.rho > 0
        assert gp.rho < 0

    def test_finite_difference_delta(self):
        # delta ~= (P(S+h) - P(S-h)) / (2h)
        kw = dict(K=100, T=0.5, r=0.05, sigma=0.20)
        S = 100.0
        h = 1e-3
        analytic = bs_greeks(option_type=OptionType.CALL, S=S, **kw).delta
        fd = (
            bs_price(option_type=OptionType.CALL, S=S + h, **kw)
            - bs_price(option_type=OptionType.CALL, S=S - h, **kw)
        ) / (2 * h)
        assert analytic == pytest.approx(fd, abs=1e-5)

    def test_finite_difference_vega(self):
        # vega ~= (P(sigma+h) - P(sigma-h)) / (2h)
        kw = dict(S=100, K=100, T=0.5, r=0.05)
        sigma = 0.20
        h = 1e-4
        analytic = bs_greeks(option_type=OptionType.CALL, sigma=sigma, **kw).vega
        fd = (
            bs_price(option_type=OptionType.CALL, sigma=sigma + h, **kw)
            - bs_price(option_type=OptionType.CALL, sigma=sigma - h, **kw)
        ) / (2 * h)
        assert analytic == pytest.approx(fd, abs=1e-3)

    def test_t_zero_greeks_collapse(self):
        g = bs_greeks(
            option_type=OptionType.CALL, S=110, K=100, T=0, r=0.05, sigma=0.2
        )
        assert isinstance(g, Greeks)
        assert g.gamma == 0.0
        assert g.vega == 0.0
        assert g.theta == 0.0
        assert g.rho == 0.0
        # ITM call → delta = 1.
        assert g.delta == 1.0


# ---------------------------------------------------------------------------
# Implied volatility
# ---------------------------------------------------------------------------


class TestImpliedVol:
    def test_round_trip_call(self):
        kw = dict(S=100, K=100, T=0.5, r=0.05, q=0.0)
        true_sigma = 0.234
        market = bs_price(option_type=OptionType.CALL, sigma=true_sigma, **kw)
        iv = implied_volatility(
            option_type=OptionType.CALL, market_price=market, **kw
        )
        assert iv == pytest.approx(true_sigma, abs=1e-6)

    def test_round_trip_put(self):
        kw = dict(S=100, K=110, T=0.25, r=0.03, q=0.01)
        true_sigma = 0.45
        market = bs_price(option_type=OptionType.PUT, sigma=true_sigma, **kw)
        iv = implied_volatility(
            option_type=OptionType.PUT, market_price=market, **kw
        )
        assert iv == pytest.approx(true_sigma, abs=1e-6)

    def test_below_intrinsic_rejected(self):
        # No vol can make a call cheaper than its intrinsic value.
        with pytest.raises(ValueError):
            implied_volatility(
                option_type=OptionType.CALL,
                market_price=0.01,
                S=120,
                K=100,
                T=0.5,
                r=0.05,
            )

    def test_negative_market_price_rejected(self):
        with pytest.raises(ValueError):
            implied_volatility(
                option_type=OptionType.CALL,
                market_price=-1.0,
                S=100,
                K=100,
                T=0.5,
                r=0.05,
            )
