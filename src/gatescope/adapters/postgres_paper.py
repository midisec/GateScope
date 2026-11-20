"""PostgreSQL repository for frozen evidence and PAPER-only trade facts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.domain.market import TIMEFRAME_SECONDS
from gatescope.domain.paper_trading import (
    PaperBar,
    PaperExactTerms,
    PaperRiskPolicy,
    PaperSide,
    SignalTerms,
    apply_entry_slippage,
    entry_drift_bps,
    evaluate_paper_exit,
    paper_pnl,
    size_paper_terms,
)
from gatescope.quant.evidence import GodDecision


class PaperNotFound(LookupError):
    pass


class PaperConflict(RuntimeError):
    pass


class PaperPreconditionFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class PaperAccount:
    id: UUID
    name: str
    base_currency: str
    initial_equity: Decimal
    realized_pnl: Decimal
    risk_fraction: Decimal
    max_notional_fraction: Decimal
    fee_bps_per_side: Decimal
    market_slippage_bps: Decimal
    max_entry_drift_bps: Decimal
    status: str
    etag: int
    created_at: datetime
    updated_at: datetime

    @property
    def equity(self) -> Decimal:
        return self.initial_equity + self.realized_pnl


@dataclass(frozen=True)
class SignalEvidence:
    id: UUID
    instrument_id: UUID
    symbol: str
    timeframe: str
    strategy_version_ref: str
    policy_version: str
    evidence_tier: str
    eligibility_status: str
    side: str
    entry_reference: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    time_stop_bars: int
    decision_at: datetime
    data_watermark: datetime
    metrics: Mapping[str, Any]
    assumptions: tuple[str, ...]
    reason_codes: tuple[str, ...]
    indicator_snapshot: Mapping[str, Any]
    chart_spec: Mapping[str, Any]
    evidence_digest: str
    created_at: datetime


@dataclass(frozen=True)
class ExecutionDecision:
    id: UUID
    signal_evidence_snapshot_id: UUID
    paper_account_id: UUID
    decision: str
    environment: str
    authorization_mode: str
    exact_terms: Mapping[str, Any]
    exact_terms_digest: str
    runtime_gates: tuple[Mapping[str, Any], ...]
    risk_snapshot: Mapping[str, Any]
    expires_at: datetime
    created_at: datetime


@dataclass(frozen=True)
class PaperProposal:
    id: UUID
    execution_decision_id: UUID
    signal_evidence_snapshot_id: UUID
    symbol: str
    timeframe: str
    environment: str
    authorization_mode: str
    exact_terms: Mapping[str, Any]
    exact_terms_digest: str
    expires_at: datetime
    proposal_digest: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class PaperTrade:
    id: UUID
    public_trade_id: str
    proposal_id: UUID
    signal_evidence_snapshot_id: UUID
    paper_account_id: UUID
    instrument_id: UUID
    symbol: str
    timeframe: str
    environment: str
    side: str
    exact_terms: Mapping[str, Any]
    exact_terms_digest: str
    state: str
    close_reason: str | None
    entry_fill_price: Decimal
    exit_fill_price: Decimal | None
    quantity: Decimal
    gross_pnl: Decimal | None
    fees: Decimal
    net_pnl: Decimal | None
    entry_available_at: datetime
    closed_at: datetime | None
    evaluated_bars: int
    last_evaluated_available_at: datetime
    protection_status: str
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ApprovalResult:
    outcome: str
    proposal_status: str
    proposal_version: int
    trade: PaperTrade | None
    gate_codes: tuple[str, ...]


@dataclass(frozen=True)
class OpenPaperTrade:
    tenant_id: UUID
    owner_user_id: UUID
    public_trade_id: str


class PostgresPaperRepository:
    """Trading-control repository whose write path is hard-coded to PAPER."""

    SIGNAL_BAR_LIMIT = 250
    DECISION_TTL = timedelta(minutes=5)

    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def create_account(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        *,
        name: str,
        base_currency: str,
        initial_equity: Decimal,
        risk_fraction: Decimal = Decimal("0.0025"),
        max_notional_fraction: Decimal = Decimal("0.10"),
        fee_bps_per_side: Decimal = Decimal("5"),
        market_slippage_bps: Decimal = Decimal("3"),
        max_entry_drift_bps: Decimal = Decimal("50"),
    ) -> PaperAccount:
        account_id = uuid4()
        clean_name = name.strip()
        currency = base_currency.strip().upper()
        PaperRiskPolicy(
            "paper-account-v1",
            initial_equity,
            risk_fraction,
            max_notional_fraction,
            fee_bps_per_side,
            market_slippage_bps,
            max_entry_drift_bps,
        )
        if not 1 <= len(clean_name) <= 120:
            raise ValueError("paper account name must contain 1-120 characters")
        if re.fullmatch(r"[A-Z0-9]{2,12}", currency) is None:
            raise ValueError("paper account base currency is invalid")
        try:
            with self.pool.transaction(tenant_id, owner_id) as connection:
                connection.execute(
                    """INSERT INTO paper_accounts
                         (id, tenant_id, owner_user_id, name, base_currency, initial_equity,
                          realized_pnl, risk_fraction, max_notional_fraction, fee_bps_per_side,
                          market_slippage_bps, max_entry_drift_bps, status, etag, created_at, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s,'ACTIVE',1,now(),now())""",
                    (
                        account_id,
                        tenant_id,
                        owner_id,
                        clean_name,
                        currency,
                        initial_equity,
                        risk_fraction,
                        max_notional_fraction,
                        fee_bps_per_side,
                        market_slippage_bps,
                        max_entry_drift_bps,
                    ),
                )
        except UniqueViolation as exc:
            raise PaperConflict("paper account name already exists") from exc
        return self.get_account(tenant_id, owner_id, account_id)

    def list_accounts(self, tenant_id: UUID, owner_id: UUID) -> list[PaperAccount]:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            rows = connection.execute(
                self._account_select()
                + " WHERE owner_user_id=%s ORDER BY created_at DESC, id DESC LIMIT 100",
                (owner_id,),
            ).fetchall()
        return [self._account(row) for row in rows]

    def get_account(self, tenant_id: UUID, owner_id: UUID, account_id: UUID) -> PaperAccount:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            row = connection.execute(
                self._account_select() + " WHERE id=%s AND owner_user_id=%s",
                (account_id, owner_id),
            ).fetchone()
        if row is None:
            raise PaperNotFound("paper account not found")
        return self._account(row)

    def record_god_signal(
        self,
        tenant_id: UUID,
        strategy_subject_id: UUID,
        *,
        instrument_id: UUID,
        timeframe: str,
        strategy_version_ref: str,
        god_decision: GodDecision,
        terms: SignalTerms,
        indicator_snapshot: Mapping[str, Any],
        source_alert_event_id: UUID | None = None,
    ) -> SignalEvidence:
        """Strategy-worker-only admission for a frozen GOD evidence package."""

        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError("unsupported signal timeframe")
        if not 1 <= len(strategy_version_ref) <= 120:
            raise ValueError("strategy_version_ref must contain 1-120 characters")
        expected_tier = "PAPER" if god_decision.decision == "PAPER_ELIGIBLE" else "RESEARCH"
        if god_decision.evidence_tier != expected_tier:
            raise ValueError("GOD decision and evidence tier disagree")
        with self.pool.transaction(tenant_id, strategy_subject_id) as connection:
            if source_alert_event_id is not None:
                source = connection.execute(
                    """SELECT instrument_id, timeframe, available_at FROM alert_event_facts
                       WHERE id=%s""",
                    (source_alert_event_id,),
                ).fetchone()
                if source is None:
                    raise PaperNotFound("source alert fact not found")
                if source[0] != instrument_id or str(source[1]) != timeframe:
                    raise PaperConflict("source alert does not match the signal market")
                if source[2] > god_decision.decision_at:
                    raise PaperConflict("source alert was not available at signal decision time")
            bars = self._frozen_bars(
                connection,
                instrument_id,
                timeframe,
                god_decision.decision_at,
                self.SIGNAL_BAR_LIMIT,
            )
            if not bars:
                raise PaperConflict("no final point-in-time bars are available for signal evidence")
            latest_close = Decimal(str(bars[-1][6]))
            if latest_close != terms.entry_reference:
                raise PaperConflict("entry reference must equal the latest frozen final close")
            metrics = {
                "independent_blocks": god_decision.independent_blocks,
                "win_probability_estimate": god_decision.win_probability_estimate,
                "win_probability_lcb": god_decision.win_probability_lcb,
                "mean_net_return": god_decision.mean_net_return,
                "expected_net_return_lcb": god_decision.expected_net_return_lcb,
                "alpha_per_hypothesis": god_decision.alpha_per_hypothesis,
                "god_evidence_digest": god_decision.evidence_digest,
            }
            manifest = [
                {
                    "venue": row[0],
                    "market": row[1],
                    "instrument_id": str(row[2]),
                    "timeframe": row[3],
                    "open_time": row[4],
                    "revision": row[10],
                    "payload_hash": row[12],
                    "available_at": row[11],
                }
                for row in bars
            ]
            chart_spec = {
                "schema_version": 1,
                "renderer": "gatescope-web/lightweight-charts",
                "view": "ORIGINAL_EVIDENCE",
                "frozen_bar_count": len(manifest),
                "indicator_snapshot_version": 1,
                "entry_overlay": {
                    "side": terms.side.value,
                    "entry": str(terms.entry_reference),
                    "stop": str(terms.stop_price),
                    "take_profit": str(terms.take_profit_price),
                },
            }
            canonical = {
                "chart_spec": chart_spec,
                "god_evidence_digest": god_decision.evidence_digest,
                "indicator_snapshot": indicator_snapshot,
                "instrument_id": instrument_id,
                "manifest": manifest,
                "metrics": metrics,
                "policy_version": god_decision.policy_version,
                "strategy_version_ref": strategy_version_ref,
                "terms": _signal_terms_json(terms),
                "timeframe": timeframe,
            }
            evidence_digest = _digest(canonical)
            existing = connection.execute(
                "SELECT id FROM signal_evidence_snapshots WHERE evidence_digest=%s",
                (evidence_digest,),
            ).fetchone()
            if existing is not None:
                signal_id = existing[0]
            else:
                signal_id = uuid4()
                connection.execute(
                    """INSERT INTO signal_evidence_snapshots
                         (id, tenant_id, created_by, source_alert_event_id, instrument_id, timeframe,
                          strategy_version_ref, policy_version, evidence_tier, eligibility_status,
                          side, entry_reference, stop_price, take_profit_price, time_stop_bars,
                          decision_at, data_watermark, metrics_json, assumptions_json, reason_codes,
                          indicator_snapshot_json, chart_spec_json, evidence_digest, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())""",
                    (
                        signal_id,
                        tenant_id,
                        strategy_subject_id,
                        source_alert_event_id,
                        instrument_id,
                        timeframe,
                        strategy_version_ref,
                        god_decision.policy_version,
                        expected_tier,
                        god_decision.decision,
                        terms.side.value,
                        terms.entry_reference,
                        terms.stop_price,
                        terms.take_profit_price,
                        terms.time_stop_bars,
                        god_decision.decision_at,
                        bars[-1][11],
                        Jsonb(_jsonable(metrics)),
                        Jsonb(list(god_decision.assumptions)),
                        list(god_decision.reason_codes),
                        Jsonb(_jsonable(dict(indicator_snapshot))),
                        Jsonb(_jsonable(chart_spec)),
                        evidence_digest,
                    ),
                )
                for sequence, row in enumerate(bars):
                    connection.execute(
                        """INSERT INTO signal_bar_manifest_items
                             (id,tenant_id,signal_evidence_snapshot_id,sequence,venue,market,
                              instrument_id,timeframe,open_time,revision,payload_hash,available_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            uuid4(), tenant_id, signal_id, sequence, row[0], row[1], row[2],
                            row[3], row[4], row[10], row[12], row[11],
                        ),
                    )
                for gate_code, result, value, reason in _god_gates(god_decision):
                    connection.execute(
                        """INSERT INTO signal_gate_results
                             (id,tenant_id,signal_evidence_snapshot_id,gate_code,result,value_json,
                              reason_code,method_version,created_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now())""",
                        (
                            uuid4(), tenant_id, signal_id, gate_code, result, Jsonb(value),
                            reason, god_decision.policy_version,
                        ),
                    )
                self._outbox(
                    connection,
                    tenant_id,
                    "signal_evidence",
                    signal_id,
                    "signal.evidence.created.v1",
                    {"signal_evidence_snapshot_id": signal_id, "evidence_digest": evidence_digest},
                )
        return self.get_signal(tenant_id, strategy_subject_id, signal_id)

    def list_signals(
        self,
        tenant_id: UUID,
        subject_id: UUID,
        *,
        limit: int = 100,
    ) -> list[SignalEvidence]:
        with self.pool.transaction(tenant_id, subject_id) as connection:
            rows = connection.execute(
                self._signal_select() + " ORDER BY s.decision_at DESC, s.id DESC LIMIT %s",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [self._signal(row) for row in rows]

    def get_signal(self, tenant_id: UUID, subject_id: UUID, signal_id: UUID) -> SignalEvidence:
        with self.pool.transaction(tenant_id, subject_id) as connection:
            row = connection.execute(
                self._signal_select() + " WHERE s.id=%s",
                (signal_id,),
            ).fetchone()
        if row is None:
            raise PaperNotFound("signal evidence not found")
        return self._signal(row)

    def signal_gates(
        self, tenant_id: UUID, subject_id: UUID, signal_id: UUID,
    ) -> list[dict[str, Any]]:
        with self.pool.transaction(tenant_id, subject_id) as connection:
            exists = connection.execute(
                "SELECT 1 FROM signal_evidence_snapshots WHERE id=%s", (signal_id,),
            ).fetchone()
            if exists is None:
                raise PaperNotFound("signal evidence not found")
            rows = connection.execute(
                """SELECT id,gate_code,result,value_json,reason_code,method_version,created_at
                   FROM signal_gate_results WHERE signal_evidence_snapshot_id=%s
                   ORDER BY gate_code""",
                (signal_id,),
            ).fetchall()
        return [
            {
                "id": row[0], "gate_code": row[1], "result": row[2], "value": dict(row[3]),
                "reason_code": row[4], "method_version": row[5], "created_at": row[6],
            }
            for row in rows
        ]

    def signal_chart(
        self, tenant_id: UUID, subject_id: UUID, signal_id: UUID,
    ) -> dict[str, Any]:
        with self.pool.transaction(tenant_id, subject_id) as connection:
            signal_row = connection.execute(
                self._signal_select() + " WHERE s.id=%s", (signal_id,),
            ).fetchone()
            if signal_row is None:
                raise PaperNotFound("signal evidence not found")
            rows = connection.execute(
                """SELECT b.open_time,b.close_time,b.open,b.high,b.low,b.close,b.volume,
                          b.available_at,b.revision,b.payload_hash,b.quality_flags
                   FROM signal_bar_manifest_items m
                   JOIN bars b ON b.venue=m.venue AND b.market=m.market
                    AND b.instrument_id=m.instrument_id AND b.timeframe=m.timeframe
                    AND b.open_time=m.open_time AND b.revision=m.revision
                    AND b.payload_hash=m.payload_hash
                   WHERE m.signal_evidence_snapshot_id=%s ORDER BY m.sequence""",
                (signal_id,),
            ).fetchall()
        signal = self._signal(signal_row)
        return {
            "view": "original",
            "frozen": True,
            "signal": signal,
            "bars": [_bar_payload(row) for row in rows],
            "evidence_boundary": signal.data_watermark,
            "indicator_snapshot": signal.indicator_snapshot,
            "chart_spec": signal.chart_spec,
            "quality_flags": sorted({flag for row in rows for flag in row[10]}),
        }

    def create_execution_decision(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        signal_id: UUID,
        *,
        paper_account_id: UUID,
        client_request_id: str,
        now: datetime | None = None,
    ) -> ExecutionDecision:
        created_at = _utc(now or datetime.now(timezone.utc))
        request_hash = _request_hash(tenant_id, owner_id, client_request_id)
        decision_id = uuid4()
        with self.pool.transaction(tenant_id, owner_id) as connection:
            replay = connection.execute(
                self._decision_select() + " WHERE d.owner_user_id=%s AND d.request_id_hash=%s",
                (owner_id, request_hash),
            ).fetchone()
            if replay is not None:
                existing = self._decision(replay)
                if (
                    existing.signal_evidence_snapshot_id != signal_id
                    or existing.paper_account_id != paper_account_id
                ):
                    raise PaperConflict("client request id was already used for different inputs")
                return existing
            signal_row = connection.execute(
                self._signal_select() + " WHERE s.id=%s", (signal_id,),
            ).fetchone()
            account_row = connection.execute(
                self._account_select() + " WHERE id=%s AND owner_user_id=%s FOR UPDATE",
                (paper_account_id, owner_id),
            ).fetchone()
            if signal_row is None:
                raise PaperNotFound("signal evidence not found")
            if account_row is None:
                raise PaperNotFound("paper account not found")
            signal, account = self._signal(signal_row), self._account(account_row)
            latest = self._latest_bar(connection, signal.instrument_id, signal.timeframe, created_at)
            gates: list[dict[str, Any]] = []
            _gate(gates, "PAPER_EVIDENCE", signal.evidence_tier == "PAPER")
            _gate(gates, "GOD_ELIGIBILITY", signal.eligibility_status == "PAPER_ELIGIBLE")
            _gate(gates, "PAPER_ACCOUNT_ACTIVE", account.status == "ACTIVE")
            _gate(gates, "PAPER_EQUITY_POSITIVE", account.equity > 0)
            observed_price: Decimal | None = None
            if latest is None:
                _gate(gates, "MARKET_BAR_AVAILABLE", False, "NO_FINAL_BAR")
                _gate(gates, "MARKET_FRESH", False, "NO_FINAL_BAR")
                _gate(gates, "ENTRY_DRIFT", False, "NO_FINAL_BAR")
            else:
                observed_price = Decimal(str(latest[6]))
                _gate(gates, "MARKET_BAR_AVAILABLE", True)
                freshness = timedelta(seconds=max(120, TIMEFRAME_SECONDS[signal.timeframe] * 2 + 60))
                _gate(
                    gates,
                    "MARKET_FRESH",
                    created_at - latest[11] <= freshness,
                    "STALE_FINAL_BAR",
                    {"available_at": latest[11], "maximum_age_seconds": int(freshness.total_seconds())},
                )
                drift = entry_drift_bps(signal.entry_reference, observed_price)
                _gate(
                    gates,
                    "ENTRY_DRIFT",
                    drift <= account.max_entry_drift_bps,
                    "ENTRY_REFERENCE_MOVED",
                    {"observed_bps": str(drift), "maximum_bps": str(account.max_entry_drift_bps)},
                )
            eligible = all(item["result"] == "PASS" for item in gates)
            risk_snapshot: dict[str, Any] = {
                "paper_account_id": paper_account_id,
                "paper_account_etag": account.etag,
                "equity": str(account.equity),
                "base_currency": account.base_currency,
                "observed_price": str(observed_price) if observed_price is not None else None,
                "as_of": created_at,
            }
            if eligible:
                policy = _risk_policy(account)
                exact = size_paper_terms(_terms_from_signal(signal), policy)
                exact_json = _exact_terms_json(exact)
                exact_digest = exact.digest
                decision, authorization = "ELIGIBLE", "MANUAL"
            else:
                exact_json = {}
                exact_digest = _digest(exact_json)
                decision, authorization = "ABSTAIN", "NONE"
            try:
                connection.execute(
                    """INSERT INTO execution_decisions
                         (id,tenant_id,owner_user_id,signal_evidence_snapshot_id,signal_evidence_digest,
                          paper_account_id,decision,environment,authorization_mode,exact_terms_json,
                          exact_terms_digest,runtime_gates_json,risk_snapshot_json,request_id_hash,
                          expires_at,created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,'PAPER',%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        decision_id, tenant_id, owner_id, signal_id, signal.evidence_digest,
                        paper_account_id, decision, authorization, Jsonb(_jsonable(exact_json)),
                        exact_digest, Jsonb(_jsonable(gates)), Jsonb(_jsonable(risk_snapshot)),
                        request_hash, created_at + self.DECISION_TTL, created_at,
                    ),
                )
                self._outbox(
                    connection,
                    tenant_id,
                    "execution_decision",
                    decision_id,
                    "execution.decision.created.v1" if eligible else "execution.decision.rejected.v1",
                    {"execution_decision_id": decision_id, "decision": decision},
                )
            except UniqueViolation as exc:
                raise PaperConflict("client request id already exists") from exc
        return self.get_execution_decision(tenant_id, owner_id, decision_id)

    def get_execution_decision(
        self, tenant_id: UUID, owner_id: UUID, decision_id: UUID,
    ) -> ExecutionDecision:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            row = connection.execute(
                self._decision_select() + " WHERE d.id=%s AND d.owner_user_id=%s",
                (decision_id, owner_id),
            ).fetchone()
        if row is None:
            raise PaperNotFound("execution decision not found")
        return self._decision(row)

    def create_proposal(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        *,
        execution_decision_id: UUID,
        client_request_id: str,
        now: datetime | None = None,
    ) -> PaperProposal:
        created_at = _utc(now or datetime.now(timezone.utc))
        request_hash = _request_hash(tenant_id, owner_id, client_request_id)
        proposal_id = uuid4()
        with self.pool.transaction(tenant_id, owner_id) as connection:
            replay = connection.execute(
                self._proposal_select()
                + " WHERE p.owner_user_id=%s AND p.request_id_hash=%s",
                (owner_id, request_hash),
            ).fetchone()
            if replay is not None:
                existing = self._proposal(replay)
                if existing.execution_decision_id != execution_decision_id:
                    raise PaperConflict("client request id was already used for different inputs")
                return existing
            decision_row = connection.execute(
                self._decision_select() + " WHERE d.id=%s AND d.owner_user_id=%s",
                (execution_decision_id, owner_id),
            ).fetchone()
            if decision_row is None:
                raise PaperNotFound("execution decision not found")
            decision = self._decision(decision_row)
            if decision.environment != "PAPER":
                raise PaperConflict("only PAPER decisions can create this proposal")
            if decision.decision != "ELIGIBLE" or decision.authorization_mode != "MANUAL":
                raise PaperConflict("execution decision is not eligible for a manual proposal")
            if decision.expires_at <= created_at:
                raise PaperConflict("execution decision has expired")
            proposal_digest = _digest(
                {
                    "execution_decision_id": execution_decision_id,
                    "exact_terms_digest": decision.exact_terms_digest,
                    "expires_at": decision.expires_at,
                    "environment": "PAPER",
                }
            )
            try:
                connection.execute(
                    """INSERT INTO trade_proposals
                         (id,tenant_id,owner_user_id,execution_decision_id,environment,
                          authorization_mode,exact_terms_json,exact_terms_digest,expires_at,
                          request_id_hash,proposal_digest,created_at)
                       VALUES (%s,%s,%s,%s,'PAPER','MANUAL',%s,%s,%s,%s,%s,%s)""",
                    (
                        proposal_id, tenant_id, owner_id, execution_decision_id,
                        Jsonb(_jsonable(decision.exact_terms)), decision.exact_terms_digest,
                        decision.expires_at, request_hash, proposal_digest, created_at,
                    ),
                )
                event_digest = self._proposal_event(
                    connection,
                    tenant_id,
                    proposal_id,
                    1,
                    "CREATED",
                    "AWAITING_CONFIRMATION",
                    owner_id,
                    "WEB",
                    {"exact_terms_digest": decision.exact_terms_digest},
                    None,
                    created_at,
                )
                connection.execute(
                    """INSERT INTO proposal_status_current
                         (proposal_id,tenant_id,status,version,last_event_digest,updated_at)
                       VALUES (%s,%s,'AWAITING_CONFIRMATION',1,%s,%s)""",
                    (proposal_id, tenant_id, event_digest, created_at),
                )
                self._outbox(
                    connection,
                    tenant_id,
                    "proposal",
                    proposal_id,
                    "proposal.created.v1",
                    {"proposal_id": proposal_id, "environment": "PAPER"},
                )
            except UniqueViolation as exc:
                raise PaperConflict("decision or client request already has a proposal") from exc
        return self.get_proposal(tenant_id, owner_id, proposal_id)

    def list_proposals(
        self, tenant_id: UUID, owner_id: UUID, *, limit: int = 100,
    ) -> list[PaperProposal]:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            rows = connection.execute(
                self._proposal_select()
                + " WHERE p.owner_user_id=%s ORDER BY p.created_at DESC,p.id DESC LIMIT %s",
                (owner_id, max(1, min(limit, 500))),
            ).fetchall()
        return [self._proposal(row) for row in rows]

    def get_proposal(
        self, tenant_id: UUID, owner_id: UUID, proposal_id: UUID,
    ) -> PaperProposal:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            row = connection.execute(
                self._proposal_select() + " WHERE p.id=%s AND p.owner_user_id=%s",
                (proposal_id, owner_id),
            ).fetchone()
        if row is None:
            raise PaperNotFound("proposal not found")
        return self._proposal(row)

    def cancel_proposal(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        proposal_id: UUID,
        *,
        expected_version: int,
        client_request_id: str,
        now: datetime | None = None,
    ) -> PaperProposal:
        occurred_at = _utc(now or datetime.now(timezone.utc))
        request_hash = _request_hash(tenant_id, owner_id, client_request_id)
        with self.pool.transaction(tenant_id, owner_id) as connection:
            row = connection.execute(
                """SELECT p.owner_user_id,s.status,s.version,s.last_event_digest
                   FROM trade_proposals p JOIN proposal_status_current s ON s.proposal_id=p.id
                   WHERE p.id=%s FOR UPDATE OF s""",
                (proposal_id,),
            ).fetchone()
            if row is None or row[0] != owner_id:
                raise PaperNotFound("proposal not found")
            prior = connection.execute(
                """SELECT resulting_status,sequence FROM proposal_status_events
                   WHERE proposal_id=%s AND payload_json->>'request_hash'=%s
                   ORDER BY sequence DESC LIMIT 1""",
                (proposal_id, request_hash),
            ).fetchone()
            if prior is not None:
                if str(prior[0]) != "CANCELED":
                    raise PaperConflict("client request id was already used for another action")
            else:
                if int(row[2]) != expected_version:
                    raise PaperPreconditionFailed("proposal version changed")
                if str(row[1]) != "AWAITING_CONFIRMATION":
                    raise PaperConflict("proposal can no longer be canceled")
                sequence = int(row[2]) + 1
                event_digest = self._proposal_event(
                    connection,
                    tenant_id,
                    proposal_id,
                    sequence,
                    "CANCELED",
                    "CANCELED",
                    owner_id,
                    "WEB",
                    {"request_hash": request_hash},
                    str(row[3]),
                    occurred_at,
                )
                connection.execute(
                    """UPDATE proposal_status_current
                       SET status='CANCELED',version=%s,last_event_digest=%s,updated_at=%s
                       WHERE proposal_id=%s AND version=%s""",
                    (sequence, event_digest, occurred_at, proposal_id, expected_version),
                )
                self._outbox(
                    connection, tenant_id, "proposal", proposal_id, "proposal.canceled.v1",
                    {"proposal_id": proposal_id, "proposal_version": sequence},
                )
        return self.get_proposal(tenant_id, owner_id, proposal_id)

    def approve_proposal(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        proposal_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        channel: str = "WEB",
        now: datetime | None = None,
    ) -> ApprovalResult:
        decided_at = _utc(now or datetime.now(timezone.utc))
        if channel not in {"WEB", "TELEGRAM"}:
            raise ValueError("unsupported approval channel")
        idempotency_hash = _request_hash(tenant_id, owner_id, idempotency_key)
        request_digest = _digest(
            {"proposal_id": proposal_id, "proposal_version": expected_version, "channel": channel}
        )
        result: ApprovalResult
        with self.pool.transaction(tenant_id, owner_id) as connection:
            replay = connection.execute(
                """SELECT a.request_digest,a.approval_outcome,p.status,p.version,t.public_trade_id
                   FROM approvals a
                   JOIN proposal_status_current p ON p.proposal_id=a.proposal_id
                   LEFT JOIN trades t ON t.approval_id=a.id
                   WHERE a.actor_user_id=%s AND a.idempotency_hash=%s""",
                (owner_id, idempotency_hash),
            ).fetchone()
            if replay is not None:
                if str(replay[0]) != request_digest:
                    raise PaperConflict("idempotency key was already used for different inputs")
                trade = (
                    self._get_trade_by_public(connection, owner_id, str(replay[4]))
                    if replay[4] is not None
                    else None
                )
                return ApprovalResult(str(replay[1]), str(replay[2]), int(replay[3]), trade, ())
            locked = connection.execute(
                """SELECT p.owner_user_id,p.expires_at,p.execution_decision_id,p.exact_terms_json,
                          p.exact_terms_digest,s.status,s.version,s.last_event_digest,
                          d.signal_evidence_snapshot_id,d.paper_account_id
                   FROM trade_proposals p
                   JOIN proposal_status_current s ON s.proposal_id=p.id
                   JOIN execution_decisions d ON d.id=p.execution_decision_id
                   WHERE p.id=%s FOR UPDATE OF s""",
                (proposal_id,),
            ).fetchone()
            if locked is None or locked[0] != owner_id:
                raise PaperNotFound("proposal not found")
            if int(locked[6]) != expected_version:
                raise PaperPreconditionFailed("proposal version changed")
            if str(locked[5]) != "AWAITING_CONFIRMATION":
                raise PaperConflict("proposal was already decided")
            if locked[1] <= decided_at:
                next_version = expected_version + 1
                event_digest = self._proposal_event(
                    connection, tenant_id, proposal_id, next_version, "EXPIRED", "EXPIRED",
                    owner_id, channel, {"expired_at": locked[1]}, str(locked[7]), decided_at,
                )
                connection.execute(
                    """UPDATE proposal_status_current SET status='EXPIRED',version=%s,
                         last_event_digest=%s,updated_at=%s WHERE proposal_id=%s AND version=%s""",
                    (next_version, event_digest, decided_at, proposal_id, expected_version),
                )
                self._outbox(
                    connection, tenant_id, "proposal", proposal_id, "proposal.expired.v1",
                    {"proposal_id": proposal_id},
                )
                result = ApprovalResult("EXPIRED", "EXPIRED", next_version, None, ("PROPOSAL_EXPIRED",))
            else:
                result = self._revalidate_and_approve(
                    connection,
                    tenant_id,
                    owner_id,
                    proposal_id,
                    expected_version,
                    locked,
                    idempotency_hash,
                    request_digest,
                    channel,
                    decided_at,
                )
        return result

    def list_trades(
        self, tenant_id: UUID, owner_id: UUID, *, limit: int = 100,
    ) -> list[PaperTrade]:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            rows = connection.execute(
                self._trade_select()
                + " WHERE t.owner_user_id=%s ORDER BY t.created_at DESC,t.id DESC LIMIT %s",
                (owner_id, max(1, min(limit, 500))),
            ).fetchall()
        return [self._trade(row) for row in rows]

    def due_tenants(self, *, limit: int = 20) -> list[UUID]:
        with self.pool.public_transaction() as connection:
            rows = connection.execute(
                "SELECT tenant_id FROM gatescope_security.paper_open_trade_due_tenants(%s)",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [row[0] for row in rows]

    def open_trade_targets(
        self,
        tenant_id: UUID,
        worker_subject_id: UUID,
        *,
        limit: int = 100,
        after_public_trade_id: str | None = None,
    ) -> list[OpenPaperTrade]:
        with self.pool.transaction(tenant_id, worker_subject_id) as connection:
            rows = connection.execute(
                """SELECT t.tenant_id,t.owner_user_id,t.public_trade_id
                   FROM trades t JOIN trade_state_current state ON state.trade_id=t.id
                   WHERE state.state='OPEN' AND (%s::text IS NULL OR t.public_trade_id>%s::text)
                   ORDER BY t.public_trade_id LIMIT %s""",
                (after_public_trade_id, after_public_trade_id, max(1, min(limit, 500))),
            ).fetchall()
        return [OpenPaperTrade(row[0], row[1], str(row[2])) for row in rows]

    def get_trade(
        self, tenant_id: UUID, owner_id: UUID, public_trade_id: str,
    ) -> PaperTrade:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            trade = self._get_trade_by_public(connection, owner_id, public_trade_id)
        if trade is None:
            raise PaperNotFound("trade not found")
        return trade

    def trade_timeline(
        self, tenant_id: UUID, owner_id: UUID, public_trade_id: str,
    ) -> list[dict[str, Any]]:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            identity = connection.execute(
                "SELECT id FROM trades WHERE public_trade_id=%s AND owner_user_id=%s",
                (public_trade_id, owner_id),
            ).fetchone()
            if identity is None:
                raise PaperNotFound("trade not found")
            rows = connection.execute(
                """SELECT id,sequence,event_type,payload_json,occurred_at,recorded_at,
                          previous_digest,event_digest FROM trade_events
                   WHERE trade_id=%s ORDER BY sequence""",
                (identity[0],),
            ).fetchall()
        return [
            {
                "id": row[0], "sequence": int(row[1]), "event_type": str(row[2]),
                "payload": dict(row[3]), "occurred_at": row[4], "recorded_at": row[5],
                "previous_digest": row[6], "event_digest": row[7],
            }
            for row in rows
        ]

    def trade_chart(
        self, tenant_id: UUID, owner_id: UUID, public_trade_id: str,
    ) -> dict[str, Any]:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            trade = self._get_trade_by_public(connection, owner_id, public_trade_id)
            if trade is None:
                raise PaperNotFound("trade not found")
            original_rows = connection.execute(
                """SELECT b.open_time,b.close_time,b.open,b.high,b.low,b.close,b.volume,
                          b.available_at,b.revision,b.payload_hash,b.quality_flags
                   FROM signal_bar_manifest_items m
                   JOIN bars b ON b.venue=m.venue AND b.market=m.market
                    AND b.instrument_id=m.instrument_id AND b.timeframe=m.timeframe
                    AND b.open_time=m.open_time AND b.revision=m.revision
                    AND b.payload_hash=m.payload_hash
                   WHERE m.signal_evidence_snapshot_id=%s ORDER BY m.sequence""",
                (trade.signal_evidence_snapshot_id,),
            ).fetchall()
            progress_rows = connection.execute(
                """SELECT open_time,NULL,open,high,low,close,volume,available_at,revision,
                          payload_hash,quality_flags FROM trade_bar_observations
                   WHERE trade_id=%s ORDER BY sequence""",
                (trade.id,),
            ).fetchall()
        return {
            "view": "progress",
            "frozen_original": True,
            "public_trade_id": public_trade_id,
            "evidence_boundary": trade.entry_available_at,
            "original_bars": [_bar_payload(row) for row in original_rows],
            "progress_bars": [_bar_payload(row) for row in progress_rows],
            "overlays": {
                "side": trade.side,
                "entry_fill": str(trade.entry_fill_price),
                "stop": trade.exact_terms["stop_price"],
                "take_profit": trade.exact_terms["take_profit_price"],
                "exit_fill": str(trade.exit_fill_price) if trade.exit_fill_price else None,
            },
            "state": trade.state,
            "close_reason": trade.close_reason,
            "data_watermark": trade.last_evaluated_available_at,
        }

    def reconcile_trade(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        public_trade_id: str,
        *,
        now: datetime | None = None,
    ) -> PaperTrade:
        observed_at = _utc(now or datetime.now(timezone.utc))
        with self.pool.transaction(tenant_id, owner_id) as connection:
            locked = connection.execute(
                """SELECT t.id,t.instrument_id,t.timeframe,t.exact_terms_json,t.paper_account_id,
                          s.state,s.version,s.last_event_digest,s.entry_fill_price,s.fees,
                          s.entry_available_at,s.evaluated_bars,s.last_evaluated_available_at
                   FROM trades t JOIN trade_state_current s ON s.trade_id=t.id
                   WHERE t.public_trade_id=%s AND t.owner_user_id=%s FOR UPDATE OF s""",
                (public_trade_id, owner_id),
            ).fetchone()
            if locked is None:
                raise PaperNotFound("trade not found")
            if str(locked[5]) == "CLOSED":
                trade = self._get_trade_by_public(connection, owner_id, public_trade_id)
                if trade is None:
                    raise AssertionError("locked trade disappeared")
                return trade
            existing_rows = connection.execute(
                """SELECT open_time,available_at,open,high,low,close,revision,payload_hash
                   FROM trade_bar_observations WHERE trade_id=%s ORDER BY sequence""",
                (locked[0],),
            ).fetchall()
            last_open = existing_rows[-1][0] if existing_rows else None
            new_rows = self._new_trade_bars(
                connection, locked[1], str(locked[2]), locked[10], last_open, observed_at, 5_000,
            )
            next_bar_sequence = len(existing_rows) + 1
            for row in new_rows:
                connection.execute(
                    """INSERT INTO trade_bar_observations
                         (id,tenant_id,trade_id,sequence,venue,market,instrument_id,timeframe,
                          open_time,revision,open,high,low,close,volume,available_at,payload_hash,
                          quality_flags,observed_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        uuid4(), tenant_id, locked[0], next_bar_sequence, row[0], row[1], row[2],
                        row[3], row[4], row[10], row[5], row[6], row[7], row[8], row[9],
                        row[11], row[12], row[13], observed_at,
                    ),
                )
                next_bar_sequence += 1
            all_bars = [
                PaperBar(row[0], row[1], Decimal(row[2]), Decimal(row[3]), Decimal(row[4]), Decimal(row[5]), int(row[6]), str(row[7]))
                for row in existing_rows
            ] + [
                PaperBar(row[4], row[11], Decimal(row[5]), Decimal(row[6]), Decimal(row[7]), Decimal(row[8]), int(row[10]), str(row[12]))
                for row in new_rows
            ]
            exact = _exact_terms_from_json(dict(locked[3]))
            exit_fact = evaluate_paper_exit(exact, all_bars, entered_after=locked[10])
            total_bars = len(all_bars)
            if exit_fact is not None:
                pnl = paper_pnl(
                    exact,
                    entry_fill_price=Decimal(locked[8]),
                    exit_fill_price=exit_fact.fill_price,
                )
                total_fees = Decimal(locked[9]) + (
                    exit_fact.fill_price * exact.quantity * exact.fee_bps_per_side / Decimal("10000")
                )
                sequence = int(locked[6]) + 1
                event_digest = self._trade_event(
                    connection,
                    tenant_id,
                    locked[0],
                    sequence,
                    "PAPER_EXIT_FILLED",
                    {
                        "close_reason": exit_fact.reason.value,
                        "trigger_price": exit_fact.trigger_price,
                        "fill_price": exit_fact.fill_price,
                        "bar_open_time": exit_fact.bar_open_time,
                        "bar_available_at": exit_fact.bar_available_at,
                        "evaluated_bars": exit_fact.evaluated_bars,
                        "ambiguity_policy_applied": exit_fact.ambiguity_policy_applied,
                        "gross_pnl": pnl.gross_pnl,
                        "fees": total_fees,
                        "net_pnl": pnl.net_pnl,
                    },
                    exit_fact.bar_available_at,
                    str(locked[7]),
                    observed_at,
                )
                connection.execute(
                    """UPDATE trade_state_current SET state='CLOSED',close_reason=%s,
                         exit_fill_price=%s,gross_pnl=%s,fees=%s,net_pnl=%s,closed_at=%s,
                         evaluated_bars=%s,last_evaluated_available_at=%s,protection_status='COMPLETED',
                         version=%s,last_event_digest=%s,updated_at=%s WHERE trade_id=%s AND version=%s""",
                    (
                        exit_fact.reason.value, exit_fact.fill_price, pnl.gross_pnl, total_fees,
                        pnl.net_pnl, exit_fact.bar_available_at, total_bars,
                        all_bars[-1].available_at, sequence, event_digest, observed_at,
                        locked[0], locked[6],
                    ),
                )
                connection.execute(
                    """UPDATE paper_accounts SET realized_pnl=realized_pnl+%s,etag=etag+1,updated_at=%s
                       WHERE id=%s""",
                    (pnl.net_pnl, observed_at, locked[4]),
                )
                self._outbox(
                    connection, tenant_id, "trade", locked[0], "trade.closed.v1",
                    {"public_trade_id": public_trade_id, "close_reason": exit_fact.reason.value},
                )
            elif total_bars > int(locked[11]):
                sequence = int(locked[6]) + 1
                event_digest = self._trade_event(
                    connection,
                    tenant_id,
                    locked[0],
                    sequence,
                    "PAPER_BAR_OBSERVED",
                    {
                        "from_evaluated_bars": int(locked[11]),
                        "to_evaluated_bars": total_bars,
                        "last_bar_open_time": all_bars[-1].open_time,
                        "last_bar_available_at": all_bars[-1].available_at,
                    },
                    all_bars[-1].available_at,
                    str(locked[7]),
                    observed_at,
                )
                connection.execute(
                    """UPDATE trade_state_current SET evaluated_bars=%s,
                         last_evaluated_available_at=%s,version=%s,last_event_digest=%s,updated_at=%s
                       WHERE trade_id=%s AND version=%s""",
                    (
                        total_bars, all_bars[-1].available_at, sequence, event_digest,
                        observed_at, locked[0], locked[6],
                    ),
                )
            trade = self._get_trade_by_public(connection, owner_id, public_trade_id)
            if trade is None:
                raise AssertionError("reconciled trade disappeared")
            return trade

    def _revalidate_and_approve(
        self,
        connection: Any,
        tenant_id: UUID,
        owner_id: UUID,
        proposal_id: UUID,
        expected_version: int,
        locked: Sequence[Any],
        idempotency_hash: str,
        request_digest: str,
        channel: str,
        decided_at: datetime,
    ) -> ApprovalResult:
        signal_row = connection.execute(
            self._signal_select() + " WHERE s.id=%s", (locked[8],),
        ).fetchone()
        account_row = connection.execute(
            self._account_select() + " WHERE id=%s AND owner_user_id=%s FOR UPDATE",
            (locked[9], owner_id),
        ).fetchone()
        if signal_row is None or account_row is None:
            raise PaperConflict("approval inputs are no longer available")
        signal, account = self._signal(signal_row), self._account(account_row)
        exact_json = dict(locked[3])
        exact = _exact_terms_from_json(exact_json)
        gates: list[dict[str, Any]] = []
        _gate(gates, "EXACT_TERMS_UNCHANGED", exact.digest == str(locked[4]), "TERMS_DIGEST_MISMATCH")
        _gate(gates, "PAPER_EVIDENCE", signal.evidence_tier == "PAPER", "EVIDENCE_NOT_PAPER")
        _gate(gates, "PAPER_ACCOUNT_ACTIVE", account.status == "ACTIVE", "ACCOUNT_NOT_ACTIVE")
        # The legacy proposal path shares the same locked cash account with
        # executable PAPER orders. Include pending reservations/new positions.
        from gatescope.adapters.postgres_paper_orders import PostgresPaperOrderRepository
        shared = PostgresPaperOrderRepository.budgets(connection, tenant_id, owner_id, {
            "id": account.id, "initial_equity": account.initial_equity,
            "realized_pnl": account.realized_pnl, "risk_fraction": account.risk_fraction,
            "max_notional_fraction": account.max_notional_fraction,
        })
        reserved_notional = exact.entry_reference * exact.quantity * (
            Decimal("1") + exact.max_entry_drift_bps / Decimal("10000")) * (
            Decimal("1") + exact.market_slippage_bps / Decimal("10000"))
        reserved_fee = reserved_notional * exact.fee_bps_per_side / Decimal("10000")
        _gate(gates, "SHARED_PAPER_COLLATERAL", shared["available_balance"] >= reserved_notional + reserved_fee,
              "PAPER_INSUFFICIENT_BALANCE")
        _gate(gates, "SHARED_PAPER_NOTIONAL", shared["notional_used"] + reserved_notional <= shared["notional_limit"],
              "PAPER_NOTIONAL_LIMIT")
        _gate(gates, "SHARED_PAPER_RISK", shared["risk_used"] + exact.worst_case_loss <= shared["risk_limit"],
              "PAPER_RISK_LIMIT")
        open_risk = connection.execute(
            """SELECT
                 coalesce(sum((t.exact_terms_json->>'worst_case_loss')::numeric),0),
                 coalesce(sum((t.exact_terms_json->>'worst_case_loss')::numeric)
                   FILTER (WHERE t.instrument_id=%s),0),
                 count(*) FILTER (WHERE t.instrument_id=%s AND t.side=%s)
               FROM trades t JOIN trade_state_current state ON state.trade_id=t.id
               WHERE t.paper_account_id=%s AND state.state='OPEN'""",
            (signal.instrument_id, signal.instrument_id, exact.side.value, account.id),
        ).fetchone()
        portfolio_risk = Decimal(open_risk[0]) if open_risk is not None else Decimal("0")
        instrument_risk = Decimal(open_risk[1]) if open_risk is not None else Decimal("0")
        duplicate_positions = int(open_risk[2]) if open_risk is not None else 0
        _gate(
            gates,
            "SINGLE_OPEN_POSITION",
            duplicate_positions == 0,
            "OPEN_POSITION_ALREADY_EXISTS",
        )
        _gate(
            gates,
            "PAPER_INSTRUMENT_RISK",
            instrument_risk + exact.worst_case_loss <= account.equity * Decimal("0.005"),
            "INSTRUMENT_RISK_LIMIT",
            {
                "post_trade_risk": str(instrument_risk + exact.worst_case_loss),
                "maximum": str(account.equity * Decimal("0.005")),
            },
        )
        _gate(
            gates,
            "PAPER_PORTFOLIO_RISK",
            portfolio_risk + exact.worst_case_loss <= account.equity * Decimal("0.015"),
            "PORTFOLIO_RISK_LIMIT",
            {
                "post_trade_risk": str(portfolio_risk + exact.worst_case_loss),
                "maximum": str(account.equity * Decimal("0.015")),
            },
        )
        latest = self._latest_bar(connection, signal.instrument_id, signal.timeframe, decided_at)
        if latest is None:
            observed = signal.entry_reference
            drift = Decimal("Infinity")
            _gate(gates, "MARKET_BAR_AVAILABLE", False, "NO_FINAL_BAR")
            _gate(gates, "MARKET_FRESH", False, "NO_FINAL_BAR")
            _gate(gates, "ENTRY_DRIFT", False, "NO_FINAL_BAR")
            market_manifest: dict[str, Any] = {"available": False}
        else:
            observed = Decimal(str(latest[6]))
            drift = entry_drift_bps(signal.entry_reference, observed)
            freshness = timedelta(seconds=max(120, TIMEFRAME_SECONDS[signal.timeframe] * 2 + 60))
            market_manifest = _market_manifest(latest)
            _gate(gates, "MARKET_BAR_AVAILABLE", True)
            _gate(
                gates, "MARKET_FRESH", decided_at - latest[11] <= freshness, "STALE_FINAL_BAR",
                {"available_at": latest[11], "maximum_age_seconds": int(freshness.total_seconds())},
            )
            _gate(
                gates, "ENTRY_DRIFT", drift <= exact.max_entry_drift_bps, "ENTRY_REFERENCE_MOVED",
                {"observed_bps": str(drift), "maximum_bps": str(exact.max_entry_drift_bps)},
            )
            entry_fill = apply_entry_slippage(exact.side, observed, exact.market_slippage_bps)
            slip = exact.market_slippage_bps / Decimal("10000")
            stop_fill = exact.stop_price * (
                Decimal("1") - slip if exact.side is PaperSide.LONG else Decimal("1") + slip
            )
            conservative_loss = (
                abs(entry_fill - stop_fill)
                + (entry_fill + stop_fill) * exact.fee_bps_per_side / Decimal("10000")
            ) * exact.quantity
            _gate(
                gates, "RISK_BUDGET", conservative_loss <= exact.risk_budget, "RISK_BUDGET_EXCEEDED",
                {"revalidated_loss": str(conservative_loss), "risk_budget": str(exact.risk_budget)},
            )
        passed = all(item["result"] == "PASS" for item in gates)
        revalidation_id = uuid4()
        snapshot_digest = _digest(
            {
                "proposal_id": proposal_id,
                "proposal_version": expected_version,
                "exact_terms_digest": locked[4],
                "market_manifest": market_manifest,
                "observed_price": observed,
                "entry_drift_bps": str(drift),
                "gates": gates,
                "result": "PASS" if passed else "FAIL",
                "created_at": decided_at,
            }
        )
        connection.execute(
            """INSERT INTO approval_revalidation_snapshots
                 (id,tenant_id,proposal_id,proposal_version,signal_evidence_snapshot_id,
                  exact_terms_digest,market_bar_manifest_json,observed_price,entry_drift_bps,
                  gates_json,result,snapshot_digest,created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                revalidation_id, tenant_id, proposal_id, expected_version, signal.id,
                locked[4], Jsonb(_jsonable(market_manifest)), observed,
                drift if drift.is_finite() else Decimal("9999999999"), Jsonb(_jsonable(gates)),
                "PASS" if passed else "FAIL", snapshot_digest, decided_at,
            ),
        )
        approval_id = uuid4()
        connection.execute(
            """INSERT INTO approvals
                 (id,tenant_id,proposal_id,proposal_version,approval_revalidation_snapshot_id,
                  actor_user_id,channel,approval_outcome,idempotency_hash,request_digest,decided_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                approval_id, tenant_id, proposal_id, expected_version, revalidation_id,
                owner_id, channel, "APPROVE" if passed else "REJECT", idempotency_hash,
                request_digest, decided_at,
            ),
        )
        next_version = expected_version + 1
        if not passed:
            gate_codes = tuple(
                str(item["gate_code"]) for item in gates if item["result"] != "PASS"
            )
            event_digest = self._proposal_event(
                connection, tenant_id, proposal_id, next_version, "REVALIDATION_REJECTED",
                "REVALIDATION_REJECTED", owner_id, channel,
                {"revalidation_snapshot_id": revalidation_id, "failed_gates": gate_codes},
                str(locked[7]), decided_at,
            )
            connection.execute(
                """UPDATE proposal_status_current SET status='REVALIDATION_REJECTED',version=%s,
                     last_event_digest=%s,updated_at=%s WHERE proposal_id=%s AND version=%s""",
                (next_version, event_digest, decided_at, proposal_id, expected_version),
            )
            self._outbox(
                connection, tenant_id, "proposal", proposal_id, "proposal.rejected.v1",
                {"proposal_id": proposal_id, "failed_gates": gate_codes},
            )
            return ApprovalResult("REJECT", "REVALIDATION_REJECTED", next_version, None, gate_codes)

        authorized_digest = self._proposal_event(
            connection, tenant_id, proposal_id, next_version, "AUTHORIZED", "AUTHORIZED",
            owner_id, channel, {"approval_id": approval_id, "revalidation_snapshot_id": revalidation_id},
            str(locked[7]), decided_at,
        )
        if latest is None:
            raise AssertionError("passing approval did not retain its market bar")
        trade_id, public_trade_id = uuid4(), _public_trade_id()
        connection.execute(
            """INSERT INTO trades
                 (id,public_trade_id,tenant_id,owner_user_id,proposal_id,approval_id,
                  signal_evidence_snapshot_id,paper_account_id,instrument_id,timeframe,
                  environment,side,exact_terms_json,exact_terms_digest,created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PAPER',%s,%s,%s,%s)""",
            (
                trade_id, public_trade_id, tenant_id, owner_id, proposal_id, approval_id,
                signal.id, account.id, signal.instrument_id, signal.timeframe, exact.side.value,
                Jsonb(_jsonable(exact_json)), exact.digest, decided_at,
            ),
        )
        entry_fill = apply_entry_slippage(exact.side, observed, exact.market_slippage_bps)
        entry_fee = entry_fill * exact.quantity * exact.fee_bps_per_side / Decimal("10000")
        previous: str | None = None
        for sequence, event_type, payload in (
            (1, "TRADE_CREATED", {"proposal_id": proposal_id, "approval_id": approval_id}),
            (
                2,
                "PAPER_ENTRY_FILLED",
                {
                    "observed_price": observed,
                    "fill_price": entry_fill,
                    "quantity": exact.quantity,
                    "fee": entry_fee,
                    "simulation": True,
                    "market_bar_manifest": market_manifest,
                },
            ),
            (
                3,
                "PAPER_PROTECTION_ACTIVE",
                {
                    "stop_price": exact.stop_price,
                    "take_profit_price": exact.take_profit_price,
                    "time_stop_bars": exact.time_stop_bars,
                    "same_bar_policy": exact.same_bar_policy,
                    "simulation": True,
                },
            ),
        ):
            previous = self._trade_event(
                connection, tenant_id, trade_id, sequence, event_type, payload,
                latest[11], previous, decided_at,
            )
        if previous is None:
            raise AssertionError("passing approval did not retain its market bar")
        connection.execute(
            """INSERT INTO trade_state_current
                 (trade_id,tenant_id,paper_account_id,instrument_id,side,state,
                  close_reason,entry_fill_price,exit_fill_price,
                  quantity,gross_pnl,fees,net_pnl,entry_available_at,closed_at,evaluated_bars,
                  last_evaluated_available_at,protection_status,version,last_event_digest,updated_at)
               VALUES (%s,%s,%s,%s,%s,'OPEN',NULL,%s,NULL,%s,NULL,%s,NULL,%s,NULL,0,%s,
                       'SIMULATED_ACTIVE',3,%s,%s)""",
            (
                trade_id, tenant_id, account.id, signal.instrument_id, exact.side.value,
                entry_fill, exact.quantity, entry_fee, latest[11], latest[11], previous, decided_at,
            ),
        )
        consumed_version = next_version + 1
        consumed_digest = self._proposal_event(
            connection, tenant_id, proposal_id, consumed_version, "CONSUMED", "CONSUMED",
            owner_id, channel, {"public_trade_id": public_trade_id}, authorized_digest, decided_at,
        )
        connection.execute(
            """UPDATE proposal_status_current SET status='CONSUMED',version=%s,
                 last_event_digest=%s,updated_at=%s WHERE proposal_id=%s AND version=%s""",
            (consumed_version, consumed_digest, decided_at, proposal_id, expected_version),
        )
        self._outbox(
            connection, tenant_id, "trade", trade_id, "trade.created.v1",
            {"public_trade_id": public_trade_id, "environment": "PAPER"},
        )
        trade = self._get_trade_by_public(connection, owner_id, public_trade_id)
        if trade is None:
            raise AssertionError("created paper trade is unavailable")
        return ApprovalResult("APPROVE", "CONSUMED", consumed_version, trade, ())

    @staticmethod
    def _frozen_bars(
        connection: Any,
        instrument_id: UUID,
        timeframe: str,
        decision_at: datetime,
        limit: int,
    ) -> list[Sequence[Any]]:
        rows = connection.execute(
            """SELECT * FROM (
                 SELECT DISTINCT ON (venue,market,instrument_id,timeframe,open_time)
                   venue,market,instrument_id,timeframe,open_time,open,close,high,low,volume,
                   revision,available_at,payload_hash,quality_flags,close_time
                 FROM bars WHERE instrument_id=%s AND timeframe=%s AND is_final=true
                   AND available_at<=%s
                 ORDER BY venue,market,instrument_id,timeframe,open_time,revision DESC
               ) frozen ORDER BY open_time DESC LIMIT %s""",
            (instrument_id, timeframe, decision_at, limit),
        ).fetchall()
        return list(reversed(rows))

    @staticmethod
    def _latest_bar(
        connection: Any, instrument_id: UUID, timeframe: str, available_at: datetime,
    ) -> Sequence[Any] | None:
        return connection.execute(
            """SELECT venue,market,instrument_id,timeframe,open_time,open,close,high,low,volume,
                      revision,available_at,payload_hash,quality_flags,close_time
               FROM bars WHERE instrument_id=%s AND timeframe=%s AND is_final=true
                 AND available_at<=%s ORDER BY open_time DESC,revision DESC LIMIT 1""",
            (instrument_id, timeframe, available_at),
        ).fetchone()

    @staticmethod
    def _new_trade_bars(
        connection: Any,
        instrument_id: UUID,
        timeframe: str,
        entry_available_at: datetime,
        last_open_time: datetime | None,
        observed_at: datetime,
        limit: int,
    ) -> list[Sequence[Any]]:
        return connection.execute(
            """SELECT * FROM (
                 SELECT DISTINCT ON (venue,market,instrument_id,timeframe,open_time)
                   venue,market,instrument_id,timeframe,open_time,open,high,low,close,volume,
                   revision,available_at,payload_hash,quality_flags
                 FROM bars WHERE instrument_id=%s AND timeframe=%s AND is_final=true
                   AND available_at>%s AND available_at<=%s
                   AND (%s::timestamptz IS NULL OR open_time>%s::timestamptz)
                 ORDER BY venue,market,instrument_id,timeframe,open_time,revision DESC
               ) observed ORDER BY open_time LIMIT %s""",
            (
                instrument_id, timeframe, entry_available_at, observed_at,
                last_open_time, last_open_time, limit,
            ),
        ).fetchall()

    @staticmethod
    def _account_select() -> str:
        return """SELECT id,name,base_currency,initial_equity,realized_pnl,risk_fraction,
                         max_notional_fraction,fee_bps_per_side,market_slippage_bps,
                         max_entry_drift_bps,status,etag,created_at,updated_at FROM paper_accounts"""

    @staticmethod
    def _signal_select() -> str:
        return """SELECT s.id,s.instrument_id,i.symbol,s.timeframe,s.strategy_version_ref,
                         s.policy_version,s.evidence_tier,s.eligibility_status,s.side,
                         s.entry_reference,s.stop_price,s.take_profit_price,s.time_stop_bars,
                         s.decision_at,s.data_watermark,s.metrics_json,s.assumptions_json,
                         s.reason_codes,s.indicator_snapshot_json,s.chart_spec_json,
                         s.evidence_digest,s.created_at
                  FROM signal_evidence_snapshots s JOIN instruments i ON i.id=s.instrument_id"""

    @staticmethod
    def _decision_select() -> str:
        return """SELECT d.id,d.signal_evidence_snapshot_id,d.paper_account_id,d.decision,
                         d.environment,d.authorization_mode,d.exact_terms_json,d.exact_terms_digest,
                         d.runtime_gates_json,d.risk_snapshot_json,d.expires_at,d.created_at
                  FROM execution_decisions d"""

    @staticmethod
    def _proposal_select() -> str:
        return """SELECT p.id,p.execution_decision_id,d.signal_evidence_snapshot_id,i.symbol,
                         s.timeframe,p.environment,p.authorization_mode,p.exact_terms_json,
                         p.exact_terms_digest,p.expires_at,p.proposal_digest,c.status,c.version,
                         p.created_at,c.updated_at
                  FROM trade_proposals p
                  JOIN proposal_status_current c ON c.proposal_id=p.id
                  JOIN execution_decisions d ON d.id=p.execution_decision_id
                  JOIN signal_evidence_snapshots s ON s.id=d.signal_evidence_snapshot_id
                  JOIN instruments i ON i.id=s.instrument_id"""

    @staticmethod
    def _trade_select() -> str:
        return """SELECT t.id,t.public_trade_id,t.proposal_id,t.signal_evidence_snapshot_id,
                         t.paper_account_id,t.instrument_id,i.symbol,t.timeframe,t.environment,t.side,
                         t.exact_terms_json,t.exact_terms_digest,s.state,s.close_reason,
                         s.entry_fill_price,s.exit_fill_price,s.quantity,s.gross_pnl,s.fees,s.net_pnl,
                         s.entry_available_at,s.closed_at,s.evaluated_bars,
                         s.last_evaluated_available_at,s.protection_status,s.version,
                         t.created_at,s.updated_at
                  FROM trades t JOIN trade_state_current s ON s.trade_id=t.id
                  JOIN instruments i ON i.id=t.instrument_id"""

    @staticmethod
    def _account(row: Sequence[Any]) -> PaperAccount:
        return PaperAccount(
            row[0], str(row[1]), str(row[2]), Decimal(row[3]), Decimal(row[4]),
            Decimal(row[5]), Decimal(row[6]), Decimal(row[7]), Decimal(row[8]),
            Decimal(row[9]), str(row[10]), int(row[11]), row[12], row[13],
        )

    @staticmethod
    def _signal(row: Sequence[Any]) -> SignalEvidence:
        return SignalEvidence(
            row[0], row[1], str(row[2]), str(row[3]), str(row[4]), str(row[5]),
            str(row[6]), str(row[7]), str(row[8]), Decimal(row[9]), Decimal(row[10]),
            Decimal(row[11]), int(row[12]), row[13], row[14], dict(row[15]),
            tuple(str(item) for item in row[16]), tuple(str(item) for item in row[17]),
            dict(row[18]), dict(row[19]), str(row[20]), row[21],
        )

    @staticmethod
    def _decision(row: Sequence[Any]) -> ExecutionDecision:
        return ExecutionDecision(
            row[0], row[1], row[2], str(row[3]), str(row[4]), str(row[5]),
            dict(row[6]), str(row[7]), tuple(dict(item) for item in row[8]),
            dict(row[9]), row[10], row[11],
        )

    @staticmethod
    def _proposal(row: Sequence[Any]) -> PaperProposal:
        return PaperProposal(
            row[0], row[1], row[2], str(row[3]), str(row[4]), str(row[5]), str(row[6]),
            dict(row[7]), str(row[8]), row[9], str(row[10]), str(row[11]), int(row[12]),
            row[13], row[14],
        )

    @staticmethod
    def _trade(row: Sequence[Any]) -> PaperTrade:
        return PaperTrade(
            row[0], str(row[1]), row[2], row[3], row[4], row[5], str(row[6]),
            str(row[7]), str(row[8]), str(row[9]), dict(row[10]), str(row[11]),
            str(row[12]), str(row[13]) if row[13] is not None else None,
            Decimal(row[14]), Decimal(row[15]) if row[15] is not None else None,
            Decimal(row[16]), Decimal(row[17]) if row[17] is not None else None,
            Decimal(row[18]), Decimal(row[19]) if row[19] is not None else None,
            row[20], row[21], int(row[22]), row[23], str(row[24]), int(row[25]), row[26], row[27],
        )

    def _get_trade_by_public(
        self, connection: Any, owner_id: UUID, public_trade_id: str,
    ) -> PaperTrade | None:
        row = connection.execute(
            self._trade_select() + " WHERE t.public_trade_id=%s AND t.owner_user_id=%s",
            (public_trade_id, owner_id),
        ).fetchone()
        return self._trade(row) if row is not None else None

    @staticmethod
    def _proposal_event(
        connection: Any,
        tenant_id: UUID,
        proposal_id: UUID,
        sequence: int,
        event_type: str,
        status: str,
        actor_id: UUID,
        channel: str,
        payload: Mapping[str, Any],
        previous_digest: str | None,
        occurred_at: datetime,
    ) -> str:
        digest = _digest(
            {
                "proposal_id": proposal_id,
                "sequence": sequence,
                "event_type": event_type,
                "resulting_status": status,
                "actor_user_id": actor_id,
                "channel": channel,
                "payload": payload,
                "previous_digest": previous_digest,
                "created_at": occurred_at,
            }
        )
        connection.execute(
            """INSERT INTO proposal_status_events
                 (id,tenant_id,proposal_id,sequence,event_type,resulting_status,actor_user_id,
                  channel,payload_json,previous_digest,event_digest,created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                uuid4(), tenant_id, proposal_id, sequence, event_type, status, actor_id,
                channel, Jsonb(_jsonable(dict(payload))), previous_digest, digest, occurred_at,
            ),
        )
        return digest

    @staticmethod
    def _trade_event(
        connection: Any,
        tenant_id: UUID,
        trade_id: UUID,
        sequence: int,
        event_type: str,
        payload: Mapping[str, Any],
        occurred_at: datetime,
        previous_digest: str | None,
        recorded_at: datetime,
    ) -> str:
        digest = _digest(
            {
                "trade_id": trade_id,
                "sequence": sequence,
                "event_type": event_type,
                "payload": payload,
                "occurred_at": occurred_at,
                "previous_digest": previous_digest,
            }
        )
        connection.execute(
            """INSERT INTO trade_events
                 (id,tenant_id,trade_id,sequence,event_type,payload_json,occurred_at,recorded_at,
                  previous_digest,event_digest)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                uuid4(), tenant_id, trade_id, sequence, event_type,
                Jsonb(_jsonable(dict(payload))), occurred_at, recorded_at, previous_digest, digest,
            ),
        )
        return digest

    @staticmethod
    def _outbox(
        connection: Any,
        tenant_id: UUID,
        aggregate_type: str,
        aggregate_id: UUID,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        now = datetime.now(timezone.utc)
        connection.execute(
            """INSERT INTO outbox_events
                 (event_id,tenant_id,aggregate_type,aggregate_id,event_type,schema_version,
                  payload_json,occurred_at,created_at,next_attempt_at)
               VALUES (%s,%s,%s,%s,%s,1,%s,%s,%s,%s)""",
            (
                uuid4(), tenant_id, aggregate_type, aggregate_id, event_type,
                Jsonb(_jsonable(dict(payload))), now, now, now,
            ),
        )


def _god_gates(decision: GodDecision) -> Iterable[tuple[str, str, dict[str, Any], str | None]]:
    reasons = set(decision.reason_codes)
    definitions: tuple[tuple[str, str, dict[str, Any]], ...] = (
        ("INDEPENDENT_BLOCKS", "INSUFFICIENT_INDEPENDENT_BLOCKS", {"count": decision.independent_blocks}),
        ("NET_COSTS_INCLUDED", "RETURNS_NOT_NET_OF_COSTS", {}),
        ("SEALED_HOLDOUT", "HOLDOUT_NOT_SEALED", {}),
        ("SOURCE_FRESHNESS", "SOURCE_STALE", {}),
        ("IN_DISTRIBUTION", "OUT_OF_DISTRIBUTION", {}),
        ("EXTERNAL_FEATURE_LICENSE", "EXTERNAL_FEATURE_LICENSE_BLOCKED", {}),
        (
            "WIN_LCB_ABOVE_BREAK_EVEN",
            "WIN_PROBABILITY_LCB_NOT_ABOVE_BREAK_EVEN",
            {"win_probability_lcb": decision.win_probability_lcb},
        ),
        (
            "EXPECTED_RETURN_LCB_POSITIVE",
            "EXPECTED_RETURN_LCB_NOT_POSITIVE",
            {"expected_net_return_lcb": decision.expected_net_return_lcb},
        ),
    )
    for gate_code, reason, value in definitions:
        failed = reason in reasons
        yield gate_code, "FAIL" if failed else "PASS", value, reason if failed else None


def _gate(
    gates: list[dict[str, Any]],
    code: str,
    passed: bool,
    failure_reason: str | None = None,
    value: Mapping[str, Any] | None = None,
) -> None:
    gates.append(
        {
            "gate_code": code,
            "result": "PASS" if passed else "FAIL",
            "reason_code": None if passed else failure_reason or code,
            "value": dict(value or {}),
        }
    )


def _risk_policy(account: PaperAccount) -> PaperRiskPolicy:
    return PaperRiskPolicy(
        f"paper-account-v2/etag-{account.etag}",
        account.equity,
        account.risk_fraction,
        account.max_notional_fraction,
        account.fee_bps_per_side,
        account.market_slippage_bps,
        account.max_entry_drift_bps,
    )


def _terms_from_signal(signal: SignalEvidence) -> SignalTerms:
    return SignalTerms(
        PaperSide(signal.side),
        signal.entry_reference,
        signal.stop_price,
        signal.take_profit_price,
        signal.time_stop_bars,
    )


def _signal_terms_json(terms: SignalTerms) -> dict[str, Any]:
    return {
        "side": terms.side.value,
        "entry_reference": str(terms.entry_reference),
        "stop_price": str(terms.stop_price),
        "take_profit_price": str(terms.take_profit_price),
        "time_stop_bars": terms.time_stop_bars,
    }


def _exact_terms_json(terms: PaperExactTerms) -> dict[str, Any]:
    return {
        "side": terms.side.value,
        "quantity": str(terms.quantity),
        "quantity_unit": terms.quantity_unit,
        "entry_reference": str(terms.entry_reference),
        "stop_price": str(terms.stop_price),
        "take_profit_price": str(terms.take_profit_price),
        "time_stop_bars": terms.time_stop_bars,
        "risk_budget": str(terms.risk_budget),
        "worst_case_loss": str(terms.worst_case_loss),
        "max_notional": str(terms.max_notional),
        "fee_bps_per_side": str(terms.fee_bps_per_side),
        "market_slippage_bps": str(terms.market_slippage_bps),
        "max_entry_drift_bps": str(terms.max_entry_drift_bps),
        "same_bar_policy": terms.same_bar_policy,
        "sizing_policy_version": terms.sizing_policy_version,
        "digest": terms.digest,
    }


def _exact_terms_from_json(value: Mapping[str, Any]) -> PaperExactTerms:
    terms = PaperExactTerms(
        PaperSide(str(value["side"])),
        Decimal(str(value["quantity"])),
        "BASE",
        Decimal(str(value["entry_reference"])),
        Decimal(str(value["stop_price"])),
        Decimal(str(value["take_profit_price"])),
        int(value["time_stop_bars"]),
        Decimal(str(value["risk_budget"])),
        Decimal(str(value["worst_case_loss"])),
        Decimal(str(value["max_notional"])),
        Decimal(str(value["fee_bps_per_side"])),
        Decimal(str(value["market_slippage_bps"])),
        Decimal(str(value["max_entry_drift_bps"])),
        "STOP_FIRST",
        str(value["sizing_policy_version"]),
        str(value["digest"]),
    )
    canonical = {key: item for key, item in value.items() if key != "digest"}
    if _digest(canonical) != terms.digest:
        raise PaperConflict("stored PAPER exact terms failed deterministic verification")
    return terms


def _market_manifest(row: Sequence[Any]) -> dict[str, Any]:
    return {
        "venue": row[0], "market": row[1], "instrument_id": row[2], "timeframe": row[3],
        "open_time": row[4], "revision": row[10], "available_at": row[11],
        "payload_hash": row[12], "quality_flags": list(row[13]),
    }


def _bar_payload(row: Sequence[Any]) -> dict[str, Any]:
    return {
        "open_time": row[0], "close_time": row[1], "open": str(row[2]), "high": str(row[3]),
        "low": str(row[4]), "close": str(row[5]), "volume": str(row[6]),
        "available_at": row[7], "revision": int(row[8]), "payload_hash": str(row[9]),
        "quality_flags": list(row[10]),
    }


def _request_hash(tenant_id: UUID, owner_id: UUID, value: str) -> str:
    clean = value.strip()
    if not 16 <= len(clean) <= 128 or re.fullmatch(r"[A-Za-z0-9_.:-]+", clean) is None:
        raise ValueError("request/idempotency key must contain 16-128 safe characters")
    return hashlib.sha256(f"{tenant_id}:{owner_id}:{clean}".encode()).hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime,)):
        return _utc(value).isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _public_trade_id() -> str:
    number = uuid4().int
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[number & 31])
        number >>= 5
    return "paper_" + "".join(reversed(chars))
