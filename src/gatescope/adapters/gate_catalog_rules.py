"""Version Gate trading rules, not the live ticker embedded in its catalog.

Field contracts: https://www.gate.com/docs/developers/apiv4/en/futures/#contract
and https://www.gate.com/docs/developers/apiv4/en/spot/#currencypair.
New upstream fields require classification here before becoming durable rules.
Market prices, OI, funding observations and sequence counters belong to market
data, not to instrument rule history. Missing/null rule fields remain distinct.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


SCHEMA = "gate-catalog-rules-1"
FUTURES_RULE_FIELDS = frozenset({
    "name", "type", "quanto_multiplier", "order_price_round", "mark_price_round",
    "order_size_min", "order_size_max", "enable_decimal", "market_order_size_max",
    "market_order_slip_ratio", "order_price_deviate", "orders_limit",
    "leverage_min", "leverage_max", "maintenance_rate", "maker_fee_rate", "taker_fee_rate",
    "risk_limit_base", "risk_limit_step", "risk_limit_max", "funding_interval",
    "interest_rate", "funding_rate_limit", "funding_impact_value", "funding_cap_ratio",
    "funding_offset", "ref_discount_rate", "ref_rebate_rate", "mark_type",
    "enable_bonus", "enable_credit", "in_delisting", "status", "create_time",
    "launch_time", "delisting_time", "delisted_time", "contract_type", "enable_circuit_breaker",
    # Compatibility with explicitly supplied listing metadata. The current Gate
    # futures history boundary uses create_time, never this optional alias.
    "listing_time",
})
SPOT_RULE_FIELDS = frozenset({
    "id", "base", "base_name", "quote", "quote_name", "trade_quotes", "precision",
    "amount_precision", "min_base_amount", "min_quote_amount", "max_base_amount",
    "max_quote_amount", "trade_status", "buy_start", "sell_start", "delisting_time",
    "type", "st_tag", "up_rate", "down_rate", "slippage", "market_order_max_stock",
    "market_order_max_money", "fee", "create_time", "listing_time",
})


def stable_catalog_rules(metadata: Any, market: str) -> dict[str, Any]:
    """Preserve exact known rule values without retaining mutable quote fields."""
    fields = SPOT_RULE_FIELDS if market == "SPOT" else FUTURES_RULE_FIELDS
    if not isinstance(metadata, dict):
        return {}
    return {key: copy.deepcopy(metadata[key]) for key in sorted(fields) if key in metadata}


def catalog_rules_equal(previous: Any, metadata: Any, market: str, identity: dict) -> bool:
    """Legacy full catalog snapshots are compared through the same projection.

    The caller separately checks changes to the instruments row for legacy
    snapshots, which do not yet carry a normalized identity envelope.
    """
    if not isinstance(previous, dict):
        return False
    envelope = previous.get("_catalog")
    if isinstance(envelope, dict) and envelope.get("identity") != identity:
        return False
    return stable_catalog_rules(previous, market) == stable_catalog_rules(metadata, market)


def catalog_revision_payload(metadata: Any, market: str, identity: dict, revision: int) -> tuple[str, str]:
    """Hash the exact immutable payload, including its transition sequence.

    Existing SQL uniqueness on (instrument_id, payload_hash) must not suppress
    a real A -> B -> A rule restoration. Only the latest rules are compared;
    revision is included in this envelope, not in that comparison. No historical
    row is rewritten. Source config-change time is provenance on real changes,
    never a reason by itself to create another identical rule revision.
    """
    payload = stable_catalog_rules(metadata, market)
    envelope = {"schema": SCHEMA, "identity": copy.deepcopy(identity), "revision": revision}
    if isinstance(metadata, dict) and "config_change_time" in metadata:
        envelope["source_config_change_time"] = metadata["config_change_time"]
    payload["_catalog"] = envelope
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return encoded, hashlib.sha256(encoded.encode()).hexdigest()
