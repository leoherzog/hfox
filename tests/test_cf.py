import pytest

from hfox.cli.cf import coerce_value, parse_asset_cf, parse_cf_json, parse_cf_options
from hfox.core.errors import ValidationError


def test_coerce_value_canonical_decimals_become_numbers():
    assert coerce_value("42") == 42
    assert coerce_value("0") == 0
    assert coerce_value("-7") == -7
    assert coerce_value("3.5") == 3.5
    assert isinstance(coerce_value("2.0"), float)


@pytest.mark.parametrize(
    "raw",
    [
        "hello", "007", "02134", "1_000", "1e5", "+42", "1.", ".5", "-", " 42", "42 ",
        "１２３", "inf", "-inf", "nan", "Infinity", "NaN", "0x10",
        "1.10", "10.50", "0.30", "-0", "123456789.123456789",
    ],
)
def test_coerce_value_non_canonical_stays_exact_string(raw):
    assert coerce_value(raw) == raw


def test_coerce_value_non_finite_decimal_stays_string():
    huge = "9" * 400 + ".5"
    assert coerce_value(huge) == huge


def test_coerce_value_long_int_is_a_number():
    assert coerce_value("9" * 400) == int("9" * 400)


def test_coerce_value_int_beyond_digit_limit_stays_string():
    huge = "9" * 5000
    assert coerce_value(huge) == huge


def test_coerce_value_bare_comma_is_literal_text():
    assert coerce_value("Acme, Inc.") == "Acme, Inc."
    assert coerce_value("3,4,5") == "3,4,5"


def test_coerce_value_bracket_syntax_builds_list():
    assert coerce_value("[4]") == [4]
    assert coerce_value("[3, 4,5]") == [3, 4, 5]
    assert coerce_value("[a, 007]") == ["a", "007"]
    assert coerce_value("[]") == []


def test_coerce_value_bracket_list_rejects_blank_item():
    with pytest.raises(ValidationError):
        coerce_value("[4,]")
    with pytest.raises(ValidationError):
        coerce_value("[1,,2]")


def test_parse_cf_options_prefixes_bare_ids():
    assert parse_cf_options(["7=Urgent"], prefix="t-cf-") == {"t-cf-7": "Urgent"}
    assert parse_cf_options(["4=VIP"], prefix="c-cf-") == {"c-cf-4": "VIP"}
    assert parse_cf_options([" 5 =x"]) == {"t-cf-5": "x"}


def test_parse_cf_options_value_keeps_everything_after_first_equals():
    assert parse_cf_options(["7=a=b"]) == {"t-cf-7": "a=b"}


def test_parse_cf_options_passes_through_own_prefix():
    assert parse_cf_options(["t-cf-3=x"]) == {"t-cf-3": "x"}
    assert parse_cf_options(["ccf-9=y"], prefix="ccf-") == {"ccf-9": "y"}


def test_parse_cf_options_passes_through_allowed_prefixes():
    allowed = ("t-cf-", "c-cf-")
    out = parse_cf_options(["c-cf-2=z", "t-cf-3=x"], prefix="t-cf-", allowed=allowed)
    assert out == {"c-cf-2": "z", "t-cf-3": "x"}


@pytest.mark.parametrize("item", ["ccf-4=y", "c-cf-2=z", "foo=bar", "=x", "t-cf-=x", "t-cf-a=x"])
def test_parse_cf_options_rejects_disallowed_or_non_numeric_keys(item):
    with pytest.raises(ValidationError):
        parse_cf_options([item], prefix="t-cf-")


def test_parse_cf_options_rejects_missing_equals():
    with pytest.raises(ValidationError):
        parse_cf_options(["nope"])


def test_parse_cf_options_rejects_blank_value():
    with pytest.raises(ValidationError):
        parse_cf_options(["7="])
    with pytest.raises(ValidationError):
        parse_cf_options(["7=   "])


def test_parse_asset_cf_keys_by_bare_id():
    assert parse_asset_cf(["5=4", "6=[3,4]", "7=a,b"]) == {"5": 4, "6": [3, 4], "7": "a,b"}


@pytest.mark.parametrize("item", ["Serial=x", "t-cf-5=4"])
def test_parse_asset_cf_rejects_non_numeric_keys(item):
    with pytest.raises(ValidationError):
        parse_asset_cf([item])


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
    assert parse_cf_json('{"7": "42"}') == {"t-cf-7": "42"}


def test_parse_cf_json_passes_through_allowed_prefix():
    assert parse_cf_json('{"c-cf-3": "Acme, Inc."}', "t-cf-", ("t-cf-", "c-cf-")) == {
        "c-cf-3": "Acme, Inc."
    }
    assert parse_cf_json('{"ccf-3": 1}', "ccf-") == {"ccf-3": 1}


@pytest.mark.parametrize("raw", ['{"c-cf-3": 1}', '{"foo": 1}', '{"": 1}'])
def test_parse_cf_json_rejects_disallowed_or_non_numeric_keys(raw):
    with pytest.raises(ValidationError):
        parse_cf_json(raw)


def test_parse_cf_json_asset_rejects_prefixed_key():
    with pytest.raises(ValidationError):
        parse_cf_json('{"t-cf-5": 4}', prefix="")


@pytest.mark.parametrize(
    "raw", ['{"7": NaN}', '{"7": Infinity}', '{"7": -Infinity}', '{"7": 1e400}']
)
def test_parse_cf_json_rejects_non_finite_numbers(raw):
    with pytest.raises(ValidationError):
        parse_cf_json(raw)


def test_parse_cf_json_rejects_invalid_json():
    with pytest.raises(ValidationError):
        parse_cf_json("{not json}")


def test_parse_cf_json_rejects_non_object():
    with pytest.raises(ValidationError):
        parse_cf_json("[1, 2, 3]")
    with pytest.raises(ValidationError):
        parse_cf_json('"a string"')
