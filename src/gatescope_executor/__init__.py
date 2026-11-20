"""Private Gate TESTNET execution boundary; LIVE adapters remain unavailable."""


class LiveTradingDisabled(RuntimeError):
    pass


def build_private_adapter() -> None:
    raise LiveTradingDisabled("LIVE private exchange adapters are unavailable")
