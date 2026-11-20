import importlib.util
from pathlib import Path


def test_notification_configuration_is_bounded_and_not_auto_trading(monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("configure_pattern_notifications", scripts / "configure_pattern_notifications.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    watched = [{"instrument_id": "btc", "symbol": "BTC_USDT"},
               {"instrument_id": "btc", "symbol": "BTC_USDT"},
               {"instrument_id": "eth", "symbol": "ETH_USDT"}]
    planned = module.planned_rules(watched, ["double_top", "double_bottom"], "channel")
    assert len(planned) == 8 and len({r["name"] for r in planned}) == 8
    for rule in planned:
        assert rule["condition"]["confirmed_only"] is True
        assert rule["condition"]["cooldown_seconds"] == 0
        assert rule["condition"]["minimum_confidence"] == 0
        assert rule["routing"]["channel_ids"] == ["channel"]
        assert set(rule) == {"name", "condition", "routing"}
