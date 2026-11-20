"""Explicitly consented PAPER tools; never call a private exchange endpoint."""
from __future__ import annotations

import base64
import json
from typing import Literal
from uuid import UUID, uuid4

from mcp_types import ContentBlock, CallToolResult, ImageContent, TextContent, ToolAnnotations

from gatescope.application.paper_execution import PaperExecutionError, execution_failure
from gatescope.domain.paper_decision import PaperDecisionContext

PAPER_READ_TOOLS = (
    "paper_account_list", "paper_account_snapshot", "paper_order_list", "paper_position_list",
    "paper_fill_list", "paper_order_get", "paper_order_preview", "paper_order_chart", "paper_order_history",
)
PAPER_WRITE_TOOLS = ("paper_order_submit", "paper_order_cancel", "paper_position_close")
PAPER_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                             idempotent_hint=True, open_world_hint=False)
PAPER_PREVIEW = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                idempotent_hint=True, open_world_hint=True)
PAPER_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True,
                              idempotent_hint=True, open_world_hint=True)


def _result(payload, *, error=False, meta=None, image_data=None):
    content: list[ContentBlock] = [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, allow_nan=False))]
    if image_data is not None:
        content.append(ImageContent(type="image", mime_type="image/png", data=image_data))
    return CallToolResult(content=content,
                          structured_content=payload, is_error=error, _meta=meta)


def register_paper_tools(server, service, principal, audit, public_origin):
    def invoke(name, function, values, *, write=False, confirm_paper=False):
        values = {key: value for key, value in values.items() if key not in {"invoke", "service", "listing"}}
        scopes = ["paper:read", "paper:trade"] if write else ["paper:read"]
        try:
            identity = principal()
            if not set(scopes).issubset(identity.scopes):
                raise PermissionError("explicit PAPER permission is required")
        except PermissionError:
            return _result({"environment": "PAPER", "error": "PAPER_SCOPE_REQUIRED",
                "message": "请重新连接并明确授权模拟账户读取 / 模拟交易权限；现有只读连接不会自动升级。"}, error=True,
                meta={"mcp/www_authenticate": [
                    f'Bearer resource_metadata="{public_origin}/.well-known/oauth-protected-resource/mcp", '
                    f'error="insufficient_scope", scope="mcp:connect {" ".join(scopes)}"']})
        trace = str(uuid4())
        try:
            if write and confirm_paper is not True:
                raise PaperExecutionError("PAPER_CONFIRMATION_REQUIRED", "必须明确确认 PAPER 模拟交易；不会发送实盘委托", 422)
            # Persist a request audit before mutations. Ledger facts and outbox are
            # committed atomically; never turn a post-commit audit error into a retryable rejection.
            audit.audit_invocation(identity, tool_name=name, arguments=values, outcome="REQUESTED", trace_id=trace)
            payload = function(identity.tenant_id, identity.subject_user_id, **values)
            image_data = payload.pop("_image_base64", None) if name == "paper_order_chart" else None
            try:
                audit.audit_invocation(identity, tool_name=name, arguments=values, outcome="SUCCESS", trace_id=trace)
            except Exception:
                # The successful order transaction remains authoritative. Do not
                # invite a second order after a separate audit sink failure.
                payload = {**payload, "audit_warning": "COMPLETION_AUDIT_UNAVAILABLE_LEDGER_PRESERVED"}
            return _result({**payload, "trace_id": trace}, image_data=image_data)
        except Exception as exc:
            failure = execution_failure(exc)
            try:
                audit.audit_invocation(identity, tool_name=name, arguments=values, outcome="ERROR", trace_id=trace,
                                       safe_error_code=failure.code)
            except Exception:
                pass
            return _result({"environment": "PAPER", "error": failure.code,
                            "message": failure.message, "trace_id": trace}, error=True)

    @server.tool(name="paper_account_list", annotations=PAPER_READ)
    def paper_account_list() -> CallToolResult:
        """List your existing Web PAPER accounts, IDs and cash. Requires paper:read; does not create accounts.

        Select an account explicitly. Use paper_account_snapshot for available balance, reservations,
        mark freshness and unrealized PnL before suggesting or submitting any simulated order.
        """
        return invoke("paper_account_list", service.account_list, {})

    @server.tool(name="paper_account_snapshot", annotations=PAPER_READ)
    def paper_account_snapshot(account_id: str) -> CallToolResult:
        """Read shared PAPER cash, free collateral, reserved funds, exposure and marked equity.

        Requires paper:read. Unknown/stale marks are not zero profit. Includes legacy Web trades
        in budget reservations; does not change any balance or query private exchange credentials.
        """
        return invoke("paper_account_snapshot", service.account_snapshot, {"account_id": account_id})

    def listing(method, tenant, owner, account_id, status=None, limit=20, offset=0,cursor=None,pagination="OFFSET"):
        args = {"account_id": UUID(account_id), "limit": limit, "offset": offset,"cursor":cursor,"pagination":pagination}
        if status is not None:
            args["status"] = status
        return getattr(service.repository, method)(tenant, owner, **args)

    @server.tool(name="paper_order_list", annotations=PAPER_READ)
    def paper_order_list(account_id: str, status: str | None = None, limit: int = 20, offset: int = 0,
                         cursor: str | None = None,pagination: Literal["OFFSET","CURSOR"]="OFFSET") -> CallToolResult:
        """Read own PAPER open/close orders with requested, filled and remaining contracts, fees and rejection reason.

        Requires paper:read. status ACTIVE or exact stored status (FILLED, CANCELED, EXPIRED, REJECTED).
        limit 1..100. Prefer pagination CURSOR and follow next_cursor, with offset 0 and unchanged filters.
        Older OFFSET remains supported. The snapshot bounds insertions, not mutable order status.
        An accepted order is not necessarily filled.
        """
        return invoke("paper_order_list", lambda t, o, **v: listing("list_orders", t, o, **v), locals())

    @server.tool(name="paper_position_list", annotations=PAPER_READ)
    def paper_position_list(account_id: str, status: Literal["OPEN", "CLOSED"] | None = None,
                            limit: int = 20, offset: int = 0,cursor: str | None = None,
                            pagination: Literal["OFFSET","CURSOR"]="OFFSET") -> CallToolResult:
        """Read own PAPER positions, original weighted entry, stop/target, remaining contracts and closed records.

        Requires paper:read. limit 1..100; prefer pagination CURSOR, next_cursor, offset 0. Position states remain live.
        Pass position id to paper_position_close. Existing legacy signal trades remain in their original Web ledger.
        """
        return invoke("paper_position_list", lambda t, o, **v: listing("list_positions", t, o, **v), locals())

    @server.tool(name="paper_fill_list", annotations=PAPER_READ)
    def paper_fill_list(account_id: str, limit: int = 20, offset: int = 0,cursor: str | None = None,
                        pagination: Literal["OFFSET","CURSOR"]="OFFSET") -> CallToolResult:
        """Read immutable PAPER entry/exit fills with actual simulated timestamps, prices, contract units, fees and realized PnL.

        Requires paper:read. Each book level produces a fill; do not count partial fills as separate strategy trades.
        limit 1..100; prefer pagination CURSOR, next_cursor, offset 0 for stable historical pages.
        Correlate fills by order and position IDs.
        """
        return invoke("paper_fill_list", lambda t, o, **v: listing("list_fills", t, o, **v), locals())

    @server.tool(name="paper_order_history", annotations=PAPER_READ)
    def paper_order_history(order_id: str, kind: Literal["fills","events"]="fills",
                            limit: int=50,cursor: str | None=None) -> CallToolResult:
        """Read owner-scoped order fills or events in chronological cursor pages, beyond 100 records.

        Requires paper:read. limit 1..100. Follow next_cursor; keep order_id and kind unchanged.
        Can continue paper_order_get's fills_next_cursor or events_next_cursor. Never mutates a ledger.
        """
        return invoke("paper_order_history",lambda t,o,order_id,**kw: service.repository.order_history(t,o,UUID(order_id),**kw),locals())

    @server.tool(name="paper_order_get", annotations=PAPER_READ)
    def paper_order_get(order_id: str) -> CallToolResult:
        """Read your PAPER order, fill evidence and event timeline. Requires paper:read; no mutation.

        Query this after an uncertain submit/cancel response. If retrying a mutation use the SAME
        idempotency_key and identical arguments, never invent a new key because a response timed out.
        """
        return invoke("paper_order_get", lambda t, o, order_id: service.repository.get_order(t, o, UUID(order_id)),
                      {"order_id": order_id})

    @server.tool(name="paper_order_chart", annotations=PAPER_READ)
    def paper_order_chart(order_id: str, pattern_index: int = 0) -> CallToolResult:
        """Read the ORIGINAL frozen PAPER decision chart as actual PNG content, never today's replacement chart.

        Requires paper:read. Your order must have a decision_context snapshot with available original
        candles. pattern_index is zero-based among the snapshot's up-to-five exact archived transitions.
        No detector, provider, new trade or mutation runs. Missing candles or out-of-window original
        anchors are disclosed, not invented. Returns snapshot ID/digest to correlate with paper_order_get.
        """
        def chart(tenant, owner, order_id, pattern_index):
            from gatescope.application.paper_decision_chart import render_paper_decision_chart
            order = service.repository.get_order(tenant, owner, UUID(order_id))
            png, metadata = render_paper_decision_chart(order, pattern_index=pattern_index)
            return {"environment": "PAPER", "image_status": "READY", "original_decision_only": True,
                    **metadata, "_image_base64": base64.b64encode(png).decode("ascii")}
        return invoke("paper_order_chart", chart, {"order_id": order_id, "pattern_index": pattern_index})

    @server.tool(name="paper_order_preview", annotations=PAPER_PREVIEW)
    def paper_order_preview(account_id: str, instrument_id: str, side: Literal["LONG", "SHORT"], contracts: int,
                            stop_price: str, take_profit_price: str, idempotency_key: str,
                            timeframe: str = "15m", order_type: Literal["MARKET", "LIMIT"] = "MARKET",
                            time_in_force: Literal["IOC", "GTC", "FOK"] = "IOC", limit_price: str | None = None,
                            max_slippage_bps: str = "50", ttl_seconds: int = 3600,
                            decision_context: PaperDecisionContext | None = None) -> CallToolResult:
        """Estimate PAPER execution against fresh public Gate USDT futures depth WITHOUT placing/reserving an order.

        Requires paper:read. Contracts are integer contract units, NOT coin quantity or USDT; inspect multiplier.
        Price strings must align to tick. 1x fully collateralized; no leverage liquidation/funding model.
        MARKET must IOC; LIMIT supports IOC/GTC/FOK. Conservative 10% participation; no invented maker queue.
        Both stop and target required. max_slippage_bps 0..100; TTL 1..86400s. Preview is not a fill guarantee.
        idempotency_key 16..128 characters should be reused on the eventual matching submit.
        Optional decision_context is a bounded original analysis claim, with observed_at and rationale,
        exact pattern_events context_id/candidate_id/sequence, and optional timestamped Coinglass claims.
        Preview saves no snapshot. Submission verifies source availability and freezes evidence atomically.
        """
        return invoke("paper_order_preview", lambda t, o, **v: service.preview(t, o, v), locals())

    @server.tool(name="paper_order_submit", annotations=PAPER_WRITE)
    def paper_order_submit(account_id: str, instrument_id: str, side: Literal["LONG", "SHORT"], contracts: int,
                           stop_price: str, take_profit_price: str, idempotency_key: str, confirm_paper: bool,
                           timeframe: str = "15m", order_type: Literal["MARKET", "LIMIT"] = "MARKET",
                           time_in_force: Literal["IOC", "GTC", "FOK"] = "IOC", limit_price: str | None = None,
                           max_slippage_bps: str = "50", ttl_seconds: int = 3600,
                           decision_context: PaperDecisionContext | None = None) -> CallToolResult:
        """PLACE an opening PAPER order in the same account shown in Web. Never places LIVE or TESTNET orders.

        Requires explicit paper:read + paper:trade consent AND confirm_paper=true. Changes local cash,
        positions and order records; actual simulated fills enqueue Telegram notifications to your own channel.
        First read account balance/positions and preview contract units/prices. Both stop and target mandatory.
        MARKET IOC or LIMIT IOC/GTC/FOK; integer contracts, decimal price strings, max slippage 0..100bps.
        Partial/zero fills are possible. Stops execute only on available future books; gaps/outages are not guaranteed.
        Generate one unique idempotency_key per intended order; after timeout retry identical arguments/key.
        No real money, funding payments, liquidation engine or guaranteed returns are represented.
        Attach decision_context with original observed_at/rationale and exact pattern event references
        to freeze past-only candle, indicator and pattern evidence. No context means UNASSOCIATED, not
        a strategy-attributed trade. Coinglass summaries remain UNVERIFIED_CLIENT_REPORTED. Do not attach
        secrets or chain-of-thought; give a concise decision summary. Read it via paper_order_get later.
        """
        values = dict(locals())
        values.pop("confirm_paper")
        return invoke("paper_order_submit", lambda t, o, **v: service.submit(t, o, v, source="MCP"), values,
                      write=True, confirm_paper=confirm_paper)

    @server.tool(name="paper_order_cancel", annotations=PAPER_WRITE)
    def paper_order_cancel(order_id: str, idempotency_key: str, confirm_paper: bool) -> CallToolResult:
        """CANCEL remaining quantity of your PAPER order, preserving already filled quantities and immutable records.

        Requires paper:read + paper:trade and confirm_paper=true. Does NOT close an existing position;
        use paper_position_close. Unique idempotency key 16..128 chars; retry SAME key after a timeout.
        """
        return invoke("paper_order_cancel", lambda t, o, order_id, idempotency_key:
            service.cancel(t, o, order_id, {"idempotency_key": idempotency_key}),
            {"order_id": order_id, "idempotency_key": idempotency_key}, write=True, confirm_paper=confirm_paper)

    @server.tool(name="paper_position_close", annotations=PAPER_WRITE)
    def paper_position_close(position_id: str, idempotency_key: str, confirm_paper: bool,
                             contracts: int | None = None, order_type: Literal["MARKET", "LIMIT"] = "MARKET",
                             time_in_force: Literal["IOC", "GTC", "FOK"] = "IOC", limit_price: str | None = None,
                             max_slippage_bps: str = "50", ttl_seconds: int = 3600) -> CallToolResult:
        """PLACE a reduce-only PAPER close order for your position, partially or fully, using fresh executable depth.

        Requires paper:read + paper:trade and confirm_paper=true. Omit contracts to close currently available
        unreserved quantity; explicit integer contracts must not exceed it. Concurrent closes cannot oversell.
        MARKET IOC or LIMIT IOC/GTC/FOK; pending/partial exits remain positions, not fabricated closed trades.
        Fills persist realized PnL/fees and Telegram notifications. Retry identical arguments/key after uncertainty.
        """
        values = dict(locals())
        values.pop("confirm_paper")
        return invoke("paper_position_close", lambda t, o, position_id, **v:
            service.close(t, o, position_id, v, source="MCP"), values, write=True, confirm_paper=confirm_paper)
