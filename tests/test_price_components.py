"""Tests for price_components.py, loaded by path so Home Assistant is not needed.

Fixtures are Pstryk pricing responses (48 hours from local midnight):
- api_pricing_20261002T120545.json: 2026-10-02 and 10-03, fetched 2026-10-02
  12:05 CEST, both days published.
- api_pricing_20261004T013435.json: 2026-10-04 and 10-05, fetched 2026-10-04
  01:34 CEST, 10-05 not published yet.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"

_spec = importlib.util.spec_from_file_location(
    "price_components", ROOT / "custom_components" / "pstryk" / "price_components.py"
)
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)


def _pricings(name):
    frames = json.loads((FIXTURES / name).read_text())["frames"]
    return [f["metrics"]["pricing"] for f in frames]


PRICINGS = _pricings("api_pricing_20261002T120545.json")
EARLY = _pricings("api_pricing_20261004T013435.json")
PUBLISHED = PRICINGS + EARLY[:24]
UNPUBLISHED_HOURS = EARLY[24:]


def test_fixture_has_48_published_hours():
    assert len(PRICINGS) == 48
    assert all(p["tge_price"] is not None for p in PRICINGS)


def test_buy_keys():
    assert pc.entry_keys("buy") == (
        "tge_price", "dist_price", "service_price", "vat_component", "excise_component",
        "is_cheap", "is_expensive",
    )


def test_sell_keys():
    assert pc.entry_keys("sell") == ("tge_price",)


def test_early_fixture_shape():
    assert len(EARLY) == 48
    assert all(p["tge_price"] is not None for p in EARLY[:24])
    assert all(p["tge_price"] is None for p in UNPUBLISHED_HOURS)
    # What makes the tge_price check necessary: other fields are already set.
    assert {p["dist_price"] for p in UNPUBLISHED_HOURS} == {0.0}
    assert {p["service_price"] for p in UNPUBLISHED_HOURS} == {0.08}
    assert {p["excise_component"] for p in UNPUBLISHED_HOURS} == {0.005}


@pytest.mark.parametrize("pricing", PUBLISHED)
def test_buy_components_sum_to_gross(pricing):
    c = pc.extract_components(pricing, "buy")
    assert set(c) == set(pc.BUY_COMPONENTS)
    total = sum(c.values())
    assert total == pytest.approx(pricing["price_gross"], abs=1e-9)


@pytest.mark.parametrize("pricing", PUBLISHED)
def test_sell_tge_gives_prosumer_gross(pricing):
    c = pc.extract_components(pricing, "sell")
    assert set(c) == {"tge_price"}
    assert c["tge_price"] * 1.23 == pytest.approx(pricing["price_prosumer_gross"], abs=1e-9)


def test_float_noise_dropped_and_digits_kept():
    first = pc.extract_components(PRICINGS[0], "buy")
    # API sends 0.6970000000000001 for this hour.
    assert PRICINGS[0]["tge_price"] == 0.6970000000000001
    assert first["tge_price"] == 0.697
    assert first["dist_price"] == 0.1348
    assert first["vat_component"] == 0.209714


def test_seven_decimals_kept():
    c = pc.extract_components({"tge_price": 0.70043, "vat_component": 0.2534209}, "buy")
    assert c["vat_component"] == 0.2534209


@pytest.mark.parametrize("price_type", ["buy", "sell"])
@pytest.mark.parametrize("pricing", UNPUBLISHED_HOURS)
def test_unpublished_hour_is_all_none(pricing, price_type):
    c = pc.extract_components(pricing, price_type)
    assert set(c) == set(pc.component_keys(price_type))
    assert all(v is None for v in c.values())


def test_unpublished_hour_does_not_report_free_distribution():
    c = pc.extract_components(UNPUBLISHED_HOURS[0], "buy")
    assert c["dist_price"] is None
    assert c["excise_component"] is None


def test_missing_component_is_none():
    c = pc.extract_components({"tge_price": 0.5}, "buy")
    assert c == {
        "tge_price": 0.5,
        "dist_price": None,
        "service_price": None,
        "vat_component": None,
        "excise_component": None,
    }


@pytest.mark.parametrize("bad", ["abc", True, [], {}])
def test_unusable_tge_counts_as_unpublished(bad):
    c = pc.extract_components({"tge_price": bad, "dist_price": 0.1348}, "buy")
    assert all(v is None for v in c.values())


@pytest.mark.parametrize("tge", [0.0, -0.12345])
def test_zero_and_negative_tge_are_published(tge):
    c = pc.extract_components({"tge_price": tge, "dist_price": 0.1348}, "buy")
    assert c["tge_price"] == tge
    assert c["dist_price"] == 0.1348


@pytest.mark.parametrize("raw", ["0.5", "0,5", " 0.5 "])
def test_numeric_string_accepted(raw):
    assert pc.extract_components({"tge_price": raw}, "sell") == {"tge_price": 0.5}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "nan", "-inf"])
def test_non_finite_counts_as_unpublished(bad):
    assert pc.extract_components({"tge_price": bad}, "sell") == {"tge_price": None}


def test_empty_pricing():
    assert pc.extract_components({}, "sell") == {"tge_price": None}
