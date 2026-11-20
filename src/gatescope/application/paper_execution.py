"""Shared Web/MCP PAPER execution facade; never owns exchange credentials."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gatescope.adapters.postgres_paper import PaperConflict, PaperNotFound, PaperPreconditionFailed
from gatescope.domain.paper_decision import PaperDecisionContext


class PaperExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status, self.message = code, status, message


class OrderControls(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    time_in_force: Literal["IOC", "GTC", "FOK"] = "IOC"
    limit_price: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_slippage_bps: Decimal = Field(default=Decimal("50"), ge=0, le=100, allow_inf_nan=False)
    ttl_seconds: int = Field(default=3600, ge=1, le=86400, strict=True)
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")

    @model_validator(mode="after")
    def execution_controls(self):
        if self.order_type == "MARKET" and (self.time_in_force != "IOC" or self.limit_price is not None):
            raise ValueError("market orders require IOC and no limit price")
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("limit price is required")
        return self


class PaperOrderRequest(OrderControls):
    account_id: UUID
    instrument_id: UUID
    timeframe: Literal["1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"] = "15m"
    side: Literal["LONG", "SHORT"]
    contracts: int = Field(gt=0, le=1_000_000_000, strict=True)
    stop_price: Decimal = Field(gt=0, allow_inf_nan=False)
    take_profit_price: Decimal = Field(gt=0, allow_inf_nan=False)
    decision_context: PaperDecisionContext | None = None


class PaperCloseRequest(OrderControls):
    contracts: int | None = Field(default=None, gt=0, le=1_000_000_000, strict=True)


class PaperCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


def execution_failure(exc: Exception) -> PaperExecutionError:
    if isinstance(exc, PaperExecutionError):
        return exc
    if isinstance(exc, PaperNotFound):
        return PaperExecutionError("PAPER_NOT_FOUND", "模拟账户、委托或仓位不存在，或不属于当前用户", 404)
    if isinstance(exc, PermissionError):
        return PaperExecutionError("PAPER_ROLE_FORBIDDEN", "当前用户角色或账户状态不允许模拟交易", 403)
    if isinstance(exc, (PaperConflict, PaperPreconditionFailed)):
        # Repository messages are codes, never return a raw SQL/upstream error.
        code = str(exc)
        known = code if code.isascii() and code.replace("_", "").isalnum() and code.isupper() and len(code) <= 80 else "PAPER_CONFLICT"
        messages = {
            "INSUFFICIENT_PAPER_BALANCE": "可用模拟余额不足；挂单和现有仓位已占用的资金不可重复使用",
            "ACCOUNT_NOTIONAL_LIMIT": "委托超过账户名义仓位上限，请减少张数或释放已有仓位/挂单",
            "ACCOUNT_RISK_LIMIT": "委托超过账户风险预算，请减少张数并核对止损距离与手续费",
            "REDUCE_ONLY_QUANTITY_UNAVAILABLE": "可平张数不足，可能已有平仓挂单占用；请刷新持仓核对",
            "PROTECTIVE_EXIT_ALREADY_TRIGGERED": "该仓位已触发止盈止损，后台正在按可用盘口退出，请查看成交记录",
            "PAPER_ACCOUNT_UNAVAILABLE": "当前模拟账户不可开仓，请选择有效的 USDT 模拟账户",
            "DECISION_TIME_OUT_OF_RANGE": "分析时间必须在过去七天内，且不能晚于当前时间",
            "DECISION_PATTERN_NOT_AVAILABLE_AT_OBSERVATION": "所选形态证据不属于该币种周期，或在分析时尚未记录；请核对原始事件引用",
            "DECISION_EVIDENCE_TOO_LARGE": "分析证据超过单笔保存上限，请减少关联形态数量；原始交易依据不会被截断",
        }
        return PaperExecutionError(known, messages.get(known, "模拟委托状态或幂等请求冲突，请先查询最新记录"), 409)
    if isinstance(exc, (ValueError, TypeError)):
        return PaperExecutionError("INVALID_PAPER_ORDER", "模拟委托参数、合约张数或价格精度无效", 422)
    return PaperExecutionError("PAPER_OUTCOME_UNKNOWN", "结果暂不能确认，请查询委托；重试必须使用同一幂等键", 503)


class PaperExecutionService:
    def __init__(self, repository, accounts, market, quotes):
        self.repository, self.accounts, self.market, self.quotes = repository, accounts, market, quotes

    def account_list(self, tenant, owner):
        items = [{"id": str(a.id), "name": a.name, "base_currency": a.base_currency,
                  "initial_equity": str(a.initial_equity), "realized_pnl": str(a.realized_pnl),
                  "cash_balance": str(a.equity), "status": a.status, "environment": "PAPER"}
                 for a in self.accounts.list_accounts(tenant, owner)]
        return {"environment": "PAPER", "items": items, "has_more": False}

    def account_snapshot(self, tenant, owner, account_id):
        return self.repository.account_snapshot(tenant, owner, UUID(str(account_id)))

    def instruments(self, *, instrument_id=None, symbol=None, market=None, require_active=True):
        if instrument_id is not None:
            if symbol is not None or market is not None:
                raise ValueError("use instrument id or exact symbol and market")
            found = self.market.get_instrument(str(UUID(str(instrument_id))))
        else:
            if (not isinstance(symbol, str) or not 1 <= len(symbol) <= 100 or symbol != symbol.strip()
                    or market != "futures:usdt"):
                raise ValueError("exact USDT futures symbol is required")
            matches = [i for i in self.market.search_instruments(symbol, market, 100, 0)
                       if i["symbol"].casefold() == symbol.casefold() and i["market"] == "futures_usdt"]
            found = matches[0] if len(matches) == 1 else None
        if not found or found["market"] != "futures_usdt" or (require_active and found.get("status") != "ACTIVE"):
            raise PaperExecutionError("PAPER_INSTRUMENT_UNSUPPORTED", "当前撮合模型仅支持已启用的 Gate USDT 线性合约", 422)
        return found

    def _quote(self, symbol):
        try:
            return self.quotes.get(symbol)
        except Exception:
            raise PaperExecutionError("PAPER_MARKET_UNAVAILABLE", "最新盘口暂不可用，未凭旧K线模拟成交；请稍后用同一幂等键重试", 503) from None

    def prepare(self, tenant, owner, request: dict, *, source: str, require_active=True):
        validated = PaperOrderRequest.model_validate(request)
        # Ownership checks precede any external market request.
        self.accounts.get_account(tenant, owner, validated.account_id)
        instrument = self.instruments(instrument_id=validated.instrument_id, require_active=require_active)
        payload = {**validated.model_dump(mode="json"), "symbol": instrument["symbol"], "source": source}
        return payload

    def submit(self, tenant, owner, request: dict, *, source: str = "WEB"):
        if source not in {"WEB", "MCP"}:
            raise ValueError("unsupported order source")
        payload = self.prepare(tenant, owner, request, source=source, require_active=False)
        previous = self.repository.replay_request(tenant, owner, payload)
        if previous is not None:
            return previous
        self.instruments(instrument_id=payload["instrument_id"])
        quote = self._quote(payload["symbol"])
        return self.repository.submit(tenant, owner, payload, quote)

    def preview(self, tenant, owner, request: dict):
        from decimal import localcontext, ROUND_FLOOR
        from gatescope.domain.paper_matching import match_order, validate_protection
        payload = self.prepare(tenant, owner, request, source="MCP")
        account = self.accounts.get_account(tenant, owner, UUID(payload["account_id"]))
        quote = self._quote(payload["symbol"])
        side = "BUY" if payload["side"] == "LONG" else "SELL"
        reference = quote.book.asks[0].price if side == "BUY" else quote.book.bids[0].price
        if payload["order_type"] == "LIMIT":
            reference = Decimal(payload["limit_price"])
        validate_protection(payload["side"], reference, Decimal(payload["stop_price"]),
                            Decimal(payload["take_profit_price"]), quote.spec.price_tick)
        matched = match_order(quote.spec, quote.book, side=side, contracts=payload["contracts"],
            order_type=payload["order_type"], time_in_force=payload["time_in_force"],
            limit_price=Decimal(payload["limit_price"]) if payload["limit_price"] is not None else None,
            max_slippage_bps=Decimal(payload["max_slippage_bps"]))
        snapshot = self.account_snapshot(tenant, owner, payload["account_id"])
        balance = snapshot["balance"]
        with localcontext() as context:
            context.prec = 60
            # Mirror the repository's conservative OPEN reservation, including
            # the higher configured fee. Neither this budget nor depth is held.
            fee_rate = max(quote.spec.taker_fee_rate, account.fee_bps_per_side / 10000)
            slippage = Decimal(payload["max_slippage_bps"]) / 10000
            bound = reference * (1 + slippage) if payload["order_type"] == "MARKET" else reference
            stop = Decimal(payload["stop_price"])
            base = Decimal(payload["contracts"]) * quote.spec.multiplier
            margin, fees = bound * base, bound * base * fee_rate
            risk = max(abs(bound-stop), abs(reference*(1-slippage)-stop))*base + fees + stop*base*fee_rate
            actual_base = sum((fill.base_quantity for fill in matched.fills), Decimal(0))
            actual_fees = matched.notional * fee_rate
            stop_loss = sum((abs(fill.price-stop)*fill.base_quantity for fill in matched.fills), Decimal(0)) + actual_fees + stop*actual_base*fee_rate
            available = Decimal(balance["available_balance"])
            notional_left = Decimal(balance["notional_limit"]) - Decimal(balance["notional_used"])
            risk_left = Decimal(balance["risk_limit"]) - Decimal(balance["risk_used"])
            limits = [available / ((margin+fees)/payload["contracts"]),
                      notional_left / (margin/payload["contracts"]),
                      risk_left / (risk/payload["contracts"]), quote.spec.max_contracts]
            if payload["order_type"] == "MARKET" and quote.spec.market_max_contracts is not None:
                limits.append(quote.spec.market_max_contracts)
            maximum = max(0, int(min(limits).to_integral_value(rounding=ROUND_FLOOR)))
            reasons = []
            for passed, reason in (
                (account.status == "ACTIVE" and account.base_currency == "USDT", "PAPER_ACCOUNT_UNAVAILABLE"),
                (quote.spec.min_contracts <= 1, "UNSUPPORTED_MINIMUM_CONTRACTS_FOR_SAFE_PARTIAL_EXITS"),
                (fee_rate <= Decimal(".01"), "UNSUPPORTED_FEE_RATE"),
                (margin + fees <= available, "INSUFFICIENT_PAPER_BALANCE"),
                (margin <= notional_left, "ACCOUNT_NOTIONAL_LIMIT"),
                (risk <= risk_left, "ACCOUNT_RISK_LIMIT"),
            ):
                if not passed:
                    reasons.append(reason)
            sizing = {"requested_base_quantity": str(base), "estimated_base_quantity": str(actual_base),
                "reference_price": str(reference), "reference_notional": str(reference*base),
                "required_margin": str(margin), "required_fees": str(fees), "reserved_risk": str(risk),
                "estimated_fill_fees": str(actual_fees), "estimated_stop_loss": str(stop_loss) if matched.filled_contracts else None,
                "effective_fee_rate": str(fee_rate), "max_contracts_by_budget": maximum,
                "stop_loss_is_guaranteed": False}
            estimate = matched.to_dict()
            estimate["fee"] = str(actual_fees)
            for fill in estimate["fills"]:
                fill["fee"] = str(Decimal(fill["notional"]) * fee_rate)
        return {"environment": "PAPER", "preview_only": True, "order_created": False,
                "account": snapshot, "sizing": sizing,
                "checks": {"budget_available": not reasons, "reasons": reasons,
                           "balance_reserved": False, "depth_reserved": False,
                           "shared_quote_consumption_included": False},
                "market": quote.to_dict(), "estimate": estimate,
                "notice": "仅为当前盘口估算，未锁定余额或成交；提交时重新检查资金与盘口"}

    def close(self, tenant, owner, position_id, request, *, source="WEB"):
        if source not in {"WEB", "MCP"}:
            raise ValueError("unsupported order source")
        validated = PaperCloseRequest.model_validate(request)
        position_id = UUID(str(position_id))
        position = self.repository.get_position(tenant, owner, position_id)
        payload = {**validated.model_dump(mode="json"), "source": source}
        previous = self.repository.replay_request(tenant, owner, payload, effect="CLOSE", position_id=position_id)
        if previous is not None:
            return previous
        quote = self._quote(position["symbol"])
        return self.repository.close(tenant, owner, position_id, payload, quote)

    def cancel(self, tenant, owner, order_id, request):
        validated = PaperCancelRequest.model_validate(request)
        return self.repository.cancel(tenant, owner, UUID(str(order_id)), validated.idempotency_key)


def build_paper_execution(pool):
    from gatescope.adapters.gate_paper_market import GatePaperMarket
    from gatescope.adapters.postgres_market import PostgresMarketReadDatabase
    from gatescope.adapters.postgres_paper import PostgresPaperRepository
    from gatescope.adapters.postgres_paper_orders import PostgresPaperOrderRepository
    return PaperExecutionService(PostgresPaperOrderRepository(pool), PostgresPaperRepository(pool),
                                 PostgresMarketReadDatabase(pool), GatePaperMarket())
