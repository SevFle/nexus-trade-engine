"""Tests for engine.core.walk_forward — train/test split iterator."""

from __future__ import annotations

import itertools

import numpy as np
import pytest

from engine.core.walk_forward import (
    WalkForwardError,
    WindowMode,
    WindowSplit,
    walk_forward_splits,
)


class TestRollingWindow:
    def test_basic_rolling_split(self):
        n = 100
        splits = list(
            walk_forward_splits(
                n_obs=n,
                train_size=50,
                test_size=10,
                step=10,
                mode=WindowMode.ROLLING,
            )
        )
        assert len(splits) > 0
        for s in splits:
            assert isinstance(s, WindowSplit)
            assert len(s.train_indices) == 50
            assert len(s.test_indices) == 10

    def test_rolling_window_slides_forward(self):
        splits = list(
            walk_forward_splits(
                n_obs=100,
                train_size=20,
                test_size=10,
                step=10,
                mode=WindowMode.ROLLING,
            )
        )
        for prev, nxt in itertools.pairwise(splits):
            assert nxt.train_indices[0] == prev.train_indices[0] + 10

    def test_train_then_test_no_overlap(self):
        splits = list(
            walk_forward_splits(
                n_obs=50,
                train_size=20,
                test_size=10,
                step=5,
            )
        )
        for s in splits:
            assert s.train_indices[-1] < s.test_indices[0]

    def test_count_of_splits(self):
        splits = list(
            walk_forward_splits(
                n_obs=100,
                train_size=50,
                test_size=10,
                step=10,
            )
        )
        assert len(splits) == 5


class TestExpandingWindow:
    def test_expanding_train_grows(self):
        splits = list(
            walk_forward_splits(
                n_obs=80,
                train_size=20,
                test_size=10,
                step=10,
                mode=WindowMode.EXPANDING,
            )
        )
        train_sizes = [len(s.train_indices) for s in splits]
        for prev, nxt in itertools.pairwise(train_sizes):
            assert nxt == prev + 10

    def test_expanding_test_size_constant(self):
        splits = list(
            walk_forward_splits(
                n_obs=80,
                train_size=20,
                test_size=10,
                step=10,
                mode=WindowMode.EXPANDING,
            )
        )
        for s in splits:
            assert len(s.test_indices) == 10

    def test_expanding_starts_from_origin(self):
        splits = list(
            walk_forward_splits(
                n_obs=80,
                train_size=20,
                test_size=10,
                step=10,
                mode=WindowMode.EXPANDING,
            )
        )
        for s in splits:
            assert s.train_indices[0] == 0


class TestIndexing:
    def test_indices_are_arrays(self):
        splits = list(
            walk_forward_splits(
                n_obs=50, train_size=20, test_size=5, step=5
            )
        )
        for s in splits:
            assert isinstance(s.train_indices, np.ndarray)
            assert isinstance(s.test_indices, np.ndarray)

    def test_test_window_immediately_follows_train(self):
        splits = list(
            walk_forward_splits(
                n_obs=50, train_size=20, test_size=5, step=5
            )
        )
        for s in splits:
            assert s.test_indices[0] == s.train_indices[-1] + 1


class TestValidation:
    def test_train_size_must_be_positive(self):
        with pytest.raises(WalkForwardError):
            list(
                walk_forward_splits(
                    n_obs=100, train_size=0, test_size=10, step=10
                )
            )

    def test_test_size_must_be_positive(self):
        with pytest.raises(WalkForwardError):
            list(
                walk_forward_splits(
                    n_obs=100, train_size=10, test_size=0, step=10
                )
            )

    def test_step_must_be_positive(self):
        with pytest.raises(WalkForwardError):
            list(
                walk_forward_splits(
                    n_obs=100, train_size=10, test_size=10, step=0
                )
            )

    def test_train_plus_test_larger_than_n_returns_empty(self):
        splits = list(
            walk_forward_splits(
                n_obs=20, train_size=15, test_size=10, step=5
            )
        )
        assert splits == []

    def test_n_obs_zero_raises(self):
        with pytest.raises(WalkForwardError):
            list(
                walk_forward_splits(
                    n_obs=0, train_size=10, test_size=10, step=5
                )
            )


class TestDataclass:
    def test_window_split_carries_arrays(self):
        s = WindowSplit(
            train_indices=np.array([0, 1, 2]),
            test_indices=np.array([3, 4]),
        )
        assert s.train_indices.tolist() == [0, 1, 2]
        assert s.test_indices.tolist() == [3, 4]

    def test_window_mode_enum_values(self):
        assert WindowMode.ROLLING.value == "rolling"
        assert WindowMode.EXPANDING.value == "expanding"
