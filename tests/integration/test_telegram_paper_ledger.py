"""Real notification DB role, fake transport; no live Telegram or exchange calls."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_paper import PaperNotFound
from gatescope.adapters.postgres_telegram_market import TelegramMarketRepository
from gatescope.application.notifications import DeliveryOutcome
from gatescope.application.telegram_market_runtime import TelegramMarketRuntime
from tests.integration.test_paper_orders import paperdb, opening, quote  # noqa: F401
from tests.integration.test_paper_order_notifications import seed_channels
from tests.integration.test_postgres_rls import TENANT_A, USER_A, USER_B


def test_notification_role_reads_only_bound_owner_and_cannot_change_ledger(paperdb):  # noqa: F811
    database, orders, _, account, other = paperdb
    seed_channels(database)
    with psycopg.connect(database) as connection:
        # The shared fixture gives B a membership only in tenant B. This test
        # also exercises a legitimately bound different owner inside tenant A.
        connection.execute("""INSERT INTO memberships(id,tenant_id,user_id,role,status,created_at,updated_at)
          VALUES(%s,%s,%s,'VIEWER','ACTIVE',now(),now())""", (uuid4(), TENANT_A, USER_B))
    at = datetime.now(timezone.utc)
    order = orders.submit(TENANT_A, USER_A, opening(account, contracts=1), quote(at), now=at)["order"]
    api = PostgresPool(make_conninfo(database, options="-c role=gatescope_api"), min_size=1, max_size=2)
    notification = PostgresPool(make_conninfo(database, options="-c role=gatescope_notification"), min_size=1, max_size=2)
    api.open(); notification.open()
    try:
        repository = TelegramMarketRepository(notification)
        assert [a["id"] for a in repository.paper_read(TENANT_A, USER_A, {"action": "paper_accounts"})["items"]] == [str(account)]
        assert repository.paper_read(TENANT_A, USER_A, {"action": "paper_account", "account_id": str(account)})["balance"]["cash_balance"]
        result = repository.paper_read(TENANT_A, USER_A, {"action": "paper_order", "order_id": order["id"]})
        assert result["filled_contracts"] == 1 and len(result["fills"]) == 1 and result["events"]
        with pytest.raises(PaperNotFound):
            repository.paper_read(TENANT_A, USER_B, {"action": "paper_order", "order_id": order["id"]})
        with pytest.raises(PaperNotFound):
            repository.paper_read(TENANT_A, USER_A, {"action": "paper_account", "account_id": str(other)})
        with notification.read_transaction(TENANT_A, USER_B) as connection:
            assert connection.execute("SELECT count(*) FROM paper_order_fills").fetchone() == (0,)
        with notification.transaction(TENANT_A, USER_A) as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
                connection.execute("UPDATE paper_accounts SET realized_pnl=1000000 WHERE id=%s", (account,))
        queue = TelegramMarketRepository(api)
        owner = SimpleNamespace(tenant_id=TENANT_A, user_id=USER_A)
        assert queue.enqueue(owner, 6001, 41, {"action": "paper_orders"}) == "QUEUED"
        sent = []
        sender = SimpleNamespace(send_market=lambda chat, text, markup, photo: sent.append((chat, text, markup)) or DeliveryOutcome(True, None, None, 1))
        assert TelegramMarketRuntime(repository, lambda _: sender, "paper-read-test", uuid4()).run_once() == 1
        assert sent[0][0] == 41 and "BTC_USDT" in sent[0][1] and "来源 MCP" in sent[0][1]
        # Forwarded notification IDs are accepted as read jobs, not authority;
        # the executing owner lookup returns no private details.
        other_owner = SimpleNamespace(tenant_id=TENANT_A, user_id=USER_B)
        assert queue.enqueue(other_owner, 6002, 42, callback="gp:order:" + UUID(order["id"]).hex) == "QUEUED"
        assert TelegramMarketRuntime(repository, lambda _: sender, "paper-read-test", uuid4()).run_once() == 1
        assert sent[-1][0] == 42 and "不属于当前绑定用户" in sent[-1][1] and "BTC_USDT" not in sent[-1][1]
    finally:
        api.close(); notification.close()
