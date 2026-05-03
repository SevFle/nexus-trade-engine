"""Tests for engine.core.tax.reports.__init__ — import completeness and re-export integrity."""

from __future__ import annotations

import importlib
from decimal import Decimal

import pytest

import engine.core.tax.reports as reports_pkg


class TestAllExportsImportable:
    def test_every_name_in_all_is_importable(self):
        for name in reports_pkg.__all__:
            assert hasattr(reports_pkg, name), (
                f"{name!r} in __all__ but not importable from package"
            )

    def test_no_extra_public_attrs_outside_all(self):
        public = {name for name in dir(reports_pkg) if not name.startswith("_")}
        all_set = set(reports_pkg.__all__)
        extras = public - all_set
        assert not extras, f"Public names not in __all__: {sorted(extras)}"


class TestModuleReimport:
    def test_reimport_does_not_raise(self):
        importlib.reload(reports_pkg)

    def test_all_is_sorted(self):
        assert reports_pkg.__all__ == sorted(reports_pkg.__all__)

    def test_all_contains_no_duplicates(self):
        assert len(reports_pkg.__all__) == len(set(reports_pkg.__all__))


class TestSpecificNewExports:
    @pytest.mark.parametrize(
        "name",
        [
            "Form6781Summary",
            "Form6781PartIISummary",
            "Form6781PartIIISummary",
            "Section1256Contract",
            "StraddleLeg",
            "YearEndPosition",
            "ScheduleDPartTotal",
            "ScheduleDSummary",
            "CarrybackAbsorption",
            "PriorYearNetGain",
            "Section1256Carryback",
        ],
    )
    def test_new_types_are_exported(self, name):
        assert hasattr(reports_pkg, name)

    @pytest.mark.parametrize(
        "name",
        [
            "generate_1099b_rows",
            "rows_to_csv",
            "summarize_form6781",
            "summarize_form6781_part_ii",
            "summarize_form6781_part_iii",
            "summarize_schedule_d",
            "apply_section_1256_carryback",
            "report_for_jurisdiction",
            "carryover_for_jurisdiction",
            "flatten_summary_to_csv",
        ],
    )
    def test_new_functions_are_exported(self, name):
        assert callable(getattr(reports_pkg, name))


class TestConstantsExported:
    @pytest.mark.parametrize(
        "name,expected_type",
        [
            ("LONG_TERM_PCT", (float, Decimal)),
            ("SHORT_TERM_PCT", (float, Decimal)),
            ("DEDUCTIBLE_CAP_DEFAULT", (int, Decimal)),
            ("DEDUCTIBLE_CAP_MFS", (int, Decimal)),
            ("CARRYBACK_YEARS", int),
            ("KEST_RATE", (float, Decimal)),
            ("SOLZ_RATE", (float, Decimal)),
            ("PFU_TOTAL_RATE", (float, Decimal)),
            ("ANNUAL_EXEMPT_AMOUNT_2024_25", (int, float, Decimal)),
        ],
    )
    def test_constant_type(self, name, expected_type):
        val = getattr(reports_pkg, name)
        assert isinstance(val, expected_type)
