import pytest

from hfox.cli.cf import coerce_value, parse_asset_cf, parse_cf_json, parse_cf_options
from hfox.core.errors import ValidationError


def test_coerce_value_scalar_types():
    assert coerce_value("42") == 42
    assert coerce_value("3.5") == 3.5
    assert coerce_value("hello") == "hello"


def test_coerce_value_multi_option():
    assert coerce_value("3,4,5") == [3, 4, 5]
    assert coerce_value("a, b") == ["a", "b"]


def test_coerce_value_non_finite_floats_stay_strings():
    for token in ("inf", "-inf", "nan", "Infinity", "-Infinity", "NaN"):
        assert coerce_value(token) == token


def test_parse_cf_options_prefixes_bare_ids():
    assert parse_cf_options(["7=Urgent"], prefix="t-cf-") == {"t-cf-7": "Urgent"}
    assert parse_cf_options(["4=VIP"], prefix="c-cf-") == {"c-cf-4": "VIP"}


def test_parse_cf_options_keeps_explicit_keys():
    assert parse_cf_options(["t-cf-3=x"]) == {"t-cf-3": "x"}
    assert parse_cf_options(["ccf-9=y"]) == {"ccf-9": "y"}
    assert parse_cf_options(["c-cf-2=z"]) == {"c-cf-2": "z"}


def test_parse_asset_cf_keys_by_bare_id():
    assert parse_asset_cf(["5=4", "6=3,4"]) == {"5": 4, "6": [3, 4]}


def test_parse_cf_options_rejects_missing_equals():
    with pytest.raises(ValidationError):
        parse_cf_options(["nope"])


def test_parse_cf_options_rejects_blank_value():
    with pytest.raises(ValidationError):
        parse_cf_options(["7="])
    with pytest.raises(ValidationError):
        parse_cf_options(["7=   "])


def test_parse_asset_cf_rejects_blank_value():
    with pytest.raises(ValidationError):
        parse_asset_cf(["5="])


def test_parse_cf_json_empty_inputs():
    assert parse_cf_json(None) == {}
    assert parse_cf_json("") == {}
    assert parse_cf_json("   ") == {}


def test_parse_cf_json_prefixes_and_preserves_types():
    out = parse_cf_json('{"7": "", "8": 0, "9": [1, 2], "10": null}')
    assert out == {"t-cf-7": "", "t-cf-8": 0, "t-cf-9": [1, 2], "t-cf-10": None}


def test_parse_cf_json_bare_prefix_for_assets():
    assert parse_cf_json('{"5": 4}', prefix="") == {"5": 4}


def test_parse_cf_json_no_coercion():
    # A numeric-looking string stays a string; types come straight from JSON.
    assert parse_cf_json('{"7": "42"}') == {"t-cf-7": "42"}


def test_parse_cf_json_rejects_invalid_json():
    with pytest.raises(ValidationError):
        parse_cf_json("{not json}")


def test_parse_cf_json_rejects_non_object():
    with pytest.raises(ValidationError):
        parse_cf_json("[1, 2, 3]")
    with pytest.raises(ValidationError):
        parse_cf_json('"a string"')
