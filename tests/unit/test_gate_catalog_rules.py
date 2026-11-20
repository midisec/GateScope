import copy
import hashlib
import json

import pytest

from gatescope.adapters.gate_catalog_rules import (
    FUTURES_RULE_FIELDS, SPOT_RULE_FIELDS, catalog_revision_payload,
    catalog_rules_equal, stable_catalog_rules,
)


IDENTITY = {"venue": "GATE", "market": "FUTURES_USDT", "symbol": "BTC_USDT",
            "base_asset": "BTC", "quote_asset": "USDT", "settle_asset": "USDT", "status": "ACTIVE"}
RULES = {"name": "BTC_USDT", "type": "direct", "quanto_multiplier": "0.0001",
         "order_price_round": "0.1", "order_size_min": "0.1", "order_size_max": "1000000",
         "enable_decimal": True, "create_time": 1669688556, "status": "trading",
         "funding_interval": 28800, "funding_rate_limit": "0.003", "taker_fee_rate": "0.0005"}
DYNAMIC = {"mark_price": "66000", "index_price": "66001", "last_price": "66002",
           "funding_rate": "0.0001", "funding_rate_indicative": "0.0002",
           "funding_next_apply": 1880000000, "orderbook_id": 12, "trade_id": 23,
           "trade_size": "100000", "position_size": "250000", "long_users": 500, "short_users": 600}


@pytest.mark.parametrize("field", sorted(FUTURES_RULE_FIELDS))
def test_every_futures_rule_is_retained_and_changes_comparison(field):
    old = {**RULES, field: "old"}
    new = {**old, field: "new"}
    assert stable_catalog_rules(new, "FUTURES_USDT")[field] == "new"
    assert not catalog_rules_equal(old, new, "FUTURES_USDT", IDENTITY)


@pytest.mark.parametrize("field", sorted(SPOT_RULE_FIELDS))
def test_every_spot_rule_is_retained_and_changes_comparison(field):
    assert stable_catalog_rules({field: "value"}, "SPOT") == {field: "value"}
    assert not catalog_rules_equal({field: "old"}, {field: "new"}, "SPOT", IDENTITY)


def test_dynamic_fields_unknown_fields_and_source_clock_do_not_generate_rules():
    old = {**RULES, **DYNAMIC, "config_change_time": 100, "new_ticker_field": "one"}
    new = {**RULES, **{key: "changed" for key in DYNAMIC}, "config_change_time": 200,
           "new_ticker_field": "two"}
    before = copy.deepcopy(old)
    assert catalog_rules_equal(old, new, "FUTURES_USDT", IDENTITY)
    assert stable_catalog_rules(new, "FUTURES_USDT") == RULES
    assert old == before


def test_transition_hash_is_actual_canonical_payload_and_preserves_restoration():
    encoded_a, hash_a = catalog_revision_payload(RULES, "FUTURES_USDT", IDENTITY, 1)
    encoded_b, hash_b = catalog_revision_payload({**RULES, "order_price_round": "0.01"}, "FUTURES_USDT", IDENTITY, 2)
    encoded_c, hash_c = catalog_revision_payload(RULES, "FUTURES_USDT", IDENTITY, 3)
    assert len({hash_a, hash_b, hash_c}) == 3
    for encoded, digest in [(encoded_a, hash_a), (encoded_b, hash_b), (encoded_c, hash_c)]:
        assert hashlib.sha256(encoded.encode()).hexdigest() == digest
    assert catalog_rules_equal(json.loads(encoded_a), json.loads(encoded_c), "FUTURES_USDT", IDENTITY)
    assert not catalog_rules_equal(json.loads(encoded_b), json.loads(encoded_c), "FUTURES_USDT", IDENTITY)


def test_legacy_metadata_matches_without_one_time_rewrite_and_absence_is_not_backfilled():
    encoded, _ = catalog_revision_payload(RULES, "FUTURES_USDT", IDENTITY, 8)
    assert catalog_rules_equal({**RULES, **DYNAMIC}, json.loads(encoded), "FUTURES_USDT", IDENTITY)
    missing = {key: value for key, value in RULES.items() if key != "create_time"}
    assert not catalog_rules_equal(RULES, missing, "FUTURES_USDT", IDENTITY)
    assert not catalog_rules_equal(missing, {**missing, "create_time": None}, "FUTURES_USDT", IDENTITY)
    assert not catalog_rules_equal(json.loads(encoded), RULES, "FUTURES_USDT", {**IDENTITY, "status": "DISABLED"})


def test_rule_values_keep_exact_units_nulls_decimal_sizes_and_zero_precision():
    metadata = {"id": "BTC_USDT", "precision": 0, "min_base_amount": None,
                "amount_precision": 8, "trade_quotes": ["USDT", "USDC"]}
    result = stable_catalog_rules(metadata, "SPOT")
    result["trade_quotes"].append("EUR")
    assert metadata["trade_quotes"] == ["USDT", "USDC"]
    assert result["precision"] == 0 and result["min_base_amount"] is None
    assert stable_catalog_rules(RULES, "FUTURES_BTC")["order_size_min"] == "0.1"


def test_config_time_is_provenance_only_and_never_copied_from_prior():
    encoded, _ = catalog_revision_payload({**RULES, "config_change_time": 123}, "FUTURES_USDT", IDENTITY, 2)
    value = json.loads(encoded)
    assert value["_catalog"]["source_config_change_time"] == 123
    assert "config_change_time" not in value
    assert catalog_rules_equal(value, {**RULES, "config_change_time": 456}, "FUTURES_USDT", IDENTITY)
