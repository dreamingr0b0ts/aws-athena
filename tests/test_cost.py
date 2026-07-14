from athena_toolkit.cost import estimate_cost, human_bytes, parse_size


def test_zero_bytes_is_free():
    est = estimate_cost(0)
    assert est.cost_usd == 0.0
    assert est.billed_bytes == 0


def test_none_bytes_is_free():
    est = estimate_cost(None)
    assert est.cost_usd == 0.0


def test_minimum_10mb_billing_applied():
    # 1 MB scanned is billed as 10 MB.
    est = estimate_cost(1_000_000)
    assert est.billed_bytes == 10_000_000
    assert est.cost_usd == 10_000_000 / 1_000_000_000_000 * 5.0


def test_minimum_can_be_disabled():
    est = estimate_cost(1_000_000, apply_minimum=False)
    assert est.billed_bytes == 1_000_000


def test_one_tb_costs_price_per_tb():
    est = estimate_cost(1_000_000_000_000, price_per_tb=5.0)
    assert round(est.cost_usd, 6) == 5.0


def test_custom_price():
    est = estimate_cost(1_000_000_000_000, price_per_tb=6.25)
    assert round(est.cost_usd, 6) == 6.25


def test_human_bytes_units():
    assert human_bytes(0) == "0 B"
    assert human_bytes(999) == "999 B"
    assert human_bytes(1_500) == "1.50 KB"
    assert human_bytes(2_500_000) == "2.50 MB"
    assert human_bytes(3_000_000_000) == "3.00 GB"
    assert human_bytes(1_000_000_000_000) == "1.00 TB"


def test_parse_size_bare_number_is_bytes():
    assert parse_size("1048576") == 1048576


def test_parse_size_units():
    assert parse_size("500MB") == 500_000_000
    assert parse_size("1.5 GB") == 1_500_000_000
    assert parse_size("2tb") == 2_000_000_000_000
    assert parse_size("10KB") == 10_000


def test_parse_size_invalid_raises():
    import pytest

    with pytest.raises(ValueError):
        parse_size("lots")
    with pytest.raises(ValueError):
        parse_size("")
