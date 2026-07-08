"""Tests for CORS origin normalisation and allowlist matching.

These cover the three classic CORS-origin bypass / misconfiguration vectors:

  * **trailing slash** — ``https://example.com/`` vs ``https://example.com``
  * **upper-case scheme** — ``HTTPS://example.com`` vs ``https://example.com``
  * **mixed-case host** — ``https://Example.com`` vs ``https://example.com``

plus the case-insensitive HTTP-header lookup that replaces the fragile
``headers.get('origin') or headers.get('Origin')`` pattern.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from engine.api.cors import (
    get_header_case_insensitive,
    is_origin_allowed,
    is_origin_header_allowed,
    normalize_origin,
    normalize_origin_allowlist,
)
from engine.config import Settings

# --------------------------------------------------------------------------- #
# normalize_origin
# --------------------------------------------------------------------------- #


class TestNormalizeOrigin:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # The three bypass vectors all collapse to the same canonical form.
            ("https://example.com", "https://example.com"),
            ("https://example.com/", "https://example.com"),  # trailing slash
            ("HTTPS://example.com", "https://example.com"),  # upper-case scheme
            ("https://Example.com", "https://example.com"),  # mixed-case host
            ("HTTPS://Example.COM/", "https://example.com"),  # all three at once
            ("  https://Example.com/  ", "https://example.com"),  # surrounding ws
        ],
    )
    def test_canonicalises_bypass_vectors(self, raw, expected):
        assert normalize_origin(raw) == expected

    def test_preserves_explicit_port(self):
        assert normalize_origin("http://localhost:3000") == "http://localhost:3000"
        assert normalize_origin("HTTP://LocalHost:3000/") == "http://localhost:3000"

    def test_strips_path_component(self):
        # An origin never carries a path; any path is dropped.
        assert normalize_origin("https://example.com/some/path") == "https://example.com"
        assert normalize_origin("https://example.com/v1?x=1#frag") == "https://example.com"

    def test_invalid_origins_return_none(self):
        for bad in ("null", "NULL", "", "   ", "not-a-url", "://no-scheme", "file:///etc"):
            assert normalize_origin(bad) is None, f"expected None for {bad!r}"

    def test_non_string_returns_none(self):
        assert normalize_origin(None) is None
        assert normalize_origin(123) is None  # type: ignore[arg-type]

    def test_non_numeric_port_returns_none(self):
        # urlparse raises ValueError on a non-numeric port; we swallow it.
        assert normalize_origin("https://example.com:abc") is None

    def test_scheme_only_or_host_only_return_none(self):
        assert normalize_origin("https://") is None
        # Bare hostname (no scheme) is not a valid absolute origin.
        assert normalize_origin("example.com") is None


# --------------------------------------------------------------------------- #
# IDN (Punycode) canonicalisation and scheme-default port handling
# --------------------------------------------------------------------------- #


class TestIdnAndDefaultPortNormalisation:
    """Unicode hostnames and explicit default ports must canonicalise so that
    semantically-equivalent origins compare equal regardless of which surface
    form (browser header vs. operator allowlist) they arrive in."""

    def test_idn_origin_canonicalises_to_punycode(self):
        # A Unicode IDN origin becomes its ASCII Punycode form.
        assert normalize_origin("https://münchen.de") == "https://xn--mnchen-3ya.de"
        # …and therefore matches a Punycode allowlist entry.
        assert is_origin_allowed("https://münchen.de", ["https://xn--mnchen-3ya.de"])

    def test_punycode_origin_matches_unicode_allowlist_entry(self):
        # The match is symmetric: whichever side carries the Unicode form,
        # both canonicalise to the same Punycode string.
        assert is_origin_allowed("https://xn--mnchen-3ya.de", ["https://münchen.de"])

    def test_explicit_https_default_port_matches_bare_origin(self):
        # An explicit :443 over https must match a bare https allowlist entry.
        assert normalize_origin("https://example.com:443") == "https://example.com"
        assert is_origin_allowed("https://example.com:443", ["https://example.com"])

    def test_explicit_http_default_port_matches_bare_origin(self):
        # An explicit :80 over http must match a bare http allowlist entry.
        assert normalize_origin("http://example.com:80") == "http://example.com"
        assert is_origin_allowed("http://example.com:80", ["http://example.com"])

    def test_non_default_ports_are_preserved(self):
        # Real (non-default) ports are kept verbatim …
        assert normalize_origin("http://example.com:8080") == "http://example.com:8080"
        assert normalize_origin("https://example.com:8443") == "https://example.com:8443"
        # … and a port that is only the default for the *other* scheme is
        # NOT dropped.
        assert normalize_origin("https://example.com:80") == "https://example.com:80"
        assert normalize_origin("http://example.com:443") == "http://example.com:443"

    def test_homograph_origin_does_not_match(self):
        # ``exámple.com`` (with á) encodes to a distinct Punycode label and
        # must NOT be conflated with the ASCII ``example.com``.
        assert normalize_origin("https://exámple.com") != "https://example.com"
        assert (
            is_origin_allowed("https://exámple.com", ["https://example.com"])
            is False
        )


# --------------------------------------------------------------------------- #
# normalize_origin_allowlist
# --------------------------------------------------------------------------- #


class TestNormalizeOriginAllowlist:
    def test_deduplicates_after_normalisation(self):
        # Three superficially-different entries canonicalise to one.
        result = normalize_origin_allowlist(
            [
                "https://example.com",
                "https://example.com/",
                "HTTPS://Example.com/",
            ]
        )
        assert result == ["https://example.com"]

    def test_drops_invalid_entries(self):
        result = normalize_origin_allowlist(
            ["https://example.com/", "not-a-url", "null", "", "https://app.test"]
        )
        assert result == ["https://example.com", "https://app.test"]

    def test_preserves_order_first_occurrence_wins(self):
        result = normalize_origin_allowlist(
            ["HTTPS://Second.com/", "https://first.com"]
        )
        assert result == ["https://second.com", "https://first.com"]

    def test_none_or_empty_returns_empty_list(self):
        assert normalize_origin_allowlist(None) == []
        assert normalize_origin_allowlist([]) == []


# --------------------------------------------------------------------------- #
# get_header_case_insensitive
# --------------------------------------------------------------------------- #


class TestGetHeaderCaseInsensitive:
    @pytest.mark.parametrize(
        ("headers", "key", "expected"),
        [
            ({"origin": "https://a.com"}, "origin", "https://a.com"),
            ({"Origin": "https://a.com"}, "origin", "https://a.com"),
            ({"ORIGIN": "https://a.com"}, "origin", "https://a.com"),
            ({"OrIgIn": "https://a.com"}, "Origin", "https://a.com"),
            ({"x-foo": "bar"}, "origin", None),
        ],
    )
    def test_finds_value_regardless_of_casing(self, headers, key, expected):
        assert get_header_case_insensitive(headers, key) == expected

    def test_replaces_fragile_or_pattern(self):
        """The ``headers.get('origin') or headers.get('Origin')`` pattern is
        defeated by any casing the server did not enumerate.  Our helper
        resolves every casing to the single underlying value."""
        for key in ("origin", "Origin", "ORIGIN", "oRiGiN"):
            headers = {key: "https://matched.com"}
            assert get_header_case_insensitive(headers, "origin") == "https://matched.com"


# --------------------------------------------------------------------------- #
# is_origin_allowed
# --------------------------------------------------------------------------- #


class TestIsOriginAllowed:
    allowlist: ClassVar[list[str]] = [
        "https://example.com/",
        "HTTP://LocalHost:3000",
        "https://app.example.com",
    ]

    @pytest.mark.parametrize(
        "origin",
        [
            "https://example.com",  # exact
            "https://example.com/",  # trailing slash
            "HTTPS://example.com",  # upper scheme
            "https://Example.com",  # mixed host
            "HTTPS://Example.COM/",  # combined
            "http://localhost:3000",
            "http://LocalHost:3000/",
            "https://app.example.com",
        ],
    )
    def test_bypass_vectors_match(self, origin):
        assert is_origin_allowed(origin, self.allowlist) is True

    @pytest.mark.parametrize(
        "origin",
        [
            "https://evil.com",
            "https://example.com.attacker.io",  # suffix/prefix trick must NOT match
            "https://notexample.com",
            "http://localhost:3001",  # wrong port
            "https://sub.app.example.com",  # origin is exact-match only (no subdomain wildcard)
        ],
    )
    def test_non_allowlisted_denied(self, origin):
        assert is_origin_allowed(origin, self.allowlist) is False

    def test_invalid_origin_denied(self):
        assert is_origin_allowed(None, self.allowlist) is False
        assert is_origin_allowed("", self.allowlist) is False
        assert is_origin_allowed("null", self.allowlist) is False

    def test_empty_allowlist_denies_everything(self):
        assert is_origin_allowed("https://example.com", []) is False
        assert is_origin_allowed("https://example.com", None) is False


# --------------------------------------------------------------------------- #
# is_origin_header_allowed (header lookup + match combined)
# --------------------------------------------------------------------------- #


class TestIsOriginHeaderAllowed:
    allowlist: ClassVar[list[str]] = ["https://example.com/"]

    @pytest.mark.parametrize("header_key", ["origin", "Origin", "ORIGIN", "oRiGiN"])
    def test_case_insensitive_header_match(self, header_key):
        headers = {header_key: "https://example.com"}
        assert is_origin_header_allowed(headers, self.allowlist) is True

    def test_case_insensitive_header_with_bypass_value(self):
        # Oddly-cased header *and* a bypass-vector value both resolve.
        headers = {"ORIGIN": "HTTPS://Example.com/"}
        assert is_origin_header_allowed(headers, self.allowlist) is True

    def test_absent_header_denied(self):
        assert is_origin_header_allowed({"x-other": "1"}, self.allowlist) is False
        assert is_origin_header_allowed({}, self.allowlist) is False

    def test_denied_origin_denied(self):
        headers = {"origin": "https://evil.com"}
        assert is_origin_header_allowed(headers, self.allowlist) is False


# --------------------------------------------------------------------------- #
# Config integration: cors_origins pre-normalised at load time
# --------------------------------------------------------------------------- #


class TestConfigCorsOriginsNormalised:
    def test_default_is_canonical(self):
        assert Settings().cors_origins == ["http://localhost:3000"]

    def test_operator_entries_are_normalised(self):
        settings = Settings(
            cors_origins=[
                "https://Example.com/",
                "HTTP://LocalHost:5173",
                "https://example.com",  # duplicate after normalisation
                "not-a-url",  # dropped
            ]
        )
        assert settings.cors_origins == [
            "https://example.com",
            "http://localhost:5173",
        ]

    def test_normalised_allowlist_matches_bypass_vector_origin(self):
        settings = Settings(cors_origins=["HTTPS://Example.COM/"])
        # A browser-origin that differs only by casing/slash must match.
        assert is_origin_allowed("https://example.com", settings.cors_origins)
