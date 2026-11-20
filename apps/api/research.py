"""Authenticated research control plane; all saved runs belong to the current user."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from psycopg.errors import ForeignKeyViolation, UniqueViolation

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_backtests import BacktestRepository
from gatescope.adapters.postgres_structure_research import StructureResearchRepository
from gatescope.adapters.postgres_indicator_research import IndicatorResearchRepository
from gatescope.adapters.postgres_portfolio_research import PortfolioResearchRepository
from gatescope.adapters.postgres_market import PostgresMarketReadDatabase
from gatescope.domain.market import GapReconciler
from gatescope.quant.backtest import CATALOG, ENGINE_VERSION, StrategySpec, features
from gatescope.quant.artifacts import report_digest
from gatescope.quant.research_rules import rule_catalog
from gatescope.quant.performance import POLICY_VERSION as PERFORMANCE_POLICY, THRESHOLDS
from gatescope.quant.portfolio_research import PortfolioPolicy
from gatescope.security import SlidingWindowLimiter


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument_id: UUID
    timeframe: str = Field(default="1h", pattern=r"^(1m|5m|15m|30m|1h|4h|8h|1d)$")
    spec: StrategySpec = Field(default_factory=StrategySpec)
    bar_limit: int = Field(default=1500, ge=300, le=550000)
    from_time: datetime | None = None
    to_time: datetime | None = None

    @model_validator(mode="after")
    def times(self) -> "BacktestRequest":
        if self.spec.engine_mode != "STRUCTURE" and self.bar_limit > 5000 and any(rule.startswith("pattern:") for rule in self.spec.rule_ids):
            raise ValueError("包含形态扫描的组合最多 5000 根；年度形态请使用同源形态回测")
        for value in (self.from_time, self.to_time):
            if value is not None and value.tzinfo is None:
                raise ValueError("区间时间必须包含时区")
        if self.from_time and self.to_time and self.from_time >= self.to_time:
            raise ValueError("起点必须早于终点")
        return self


class PortfolioRequest(PortfolioPolicy):
    source_run_ids: list[UUID] = Field(min_length=1,max_length=8)
    from_time: datetime|None = None
    to_time: datetime|None = None

    @model_validator(mode="after")
    def sources_and_times(self) -> "PortfolioRequest":
        if len(set(self.source_run_ids))!=len(self.source_run_ids):
            raise ValueError("来源研究不能重复")
        if any(value is not None and value.tzinfo is None for value in (self.from_time,self.to_time)):
            raise ValueError("区间时间必须包含时区")
        if self.from_time and self.to_time and self.from_time>=self.to_time:
            raise ValueError("起点必须早于终点")
        return self


def build_research_router(pool: PostgresPool) -> APIRouter:
    router = APIRouter(prefix="/api/v2/research", tags=["research"])
    repository = BacktestRepository(pool)
    structures = StructureResearchRepository(pool)
    indicators = IndicatorResearchRepository(pool)
    portfolios = PortfolioResearchRepository(pool)
    limiter = SlidingWindowLimiter()

    @router.post("/portfolios",status_code=202)
    def create_portfolio(payload: PortfolioRequest,request: Request,response: Response) -> dict:
        session=request.state.auth_session
        if session.role.value=="VIEWER":
            raise HTTPException(403,"观察者可查看回测，创建需要研究员权限")
        allowed,retry=limiter.allow(f"portfolio:{session.tenant_id}:{session.user_id}",4,60)
        if not allowed:
            raise HTTPException(429,"组合提交过于频繁",headers={"Retry-After":str(retry)})
        try:
            result=portfolios.create(session.tenant_id,session.user_id,payload.source_run_ids,
                PortfolioPolicy.model_validate(payload.model_dump(exclude={"source_run_ids","from_time","to_time"})),
                payload.from_time,payload.to_time)
        except UniqueViolation:
            raise HTTPException(409,"已有组合回测运行中，请等待完成") from None
        except LookupError:
            raise HTTPException(404,"来源必须是当前用户已完成的同源形态研究") from None
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None
        response.headers["Location"]=f"/api/v2/research/portfolios/{result['id']}"
        return result

    @router.get("/portfolios")
    def list_portfolios(request: Request,response: Response,limit: int=Query(default=20,ge=1,le=100),
                        offset: int=Query(default=0,ge=0,le=1000000)) -> dict:
        session=request.state.auth_session
        response.headers["Cache-Control"]="no-store"
        return portfolios.list(session.tenant_id,session.user_id,limit=limit,offset=offset)

    @router.get("/portfolios/{run_id}")
    def portfolio_report(run_id: UUID,request: Request,response: Response) -> dict:
        session=request.state.auth_session
        response.headers["Cache-Control"]="no-store"
        try:
            return portfolios.get(session.tenant_id,session.user_id,run_id)
        except LookupError:
            raise HTTPException(404,"组合回测不存在") from None

    @router.get("/portfolios/{run_id}/details/{kind}")
    @router.get("/portfolios/{run_id}/{kind}")
    def portfolio_details(run_id: UUID,kind: str,request: Request,response: Response,
                          limit: int=Query(default=25,ge=1,le=512),offset: int=Query(default=0,ge=0,le=1000000)) -> dict:
        if kind not in {"trades","decisions","curve"}:
            raise HTTPException(404,"未知组合证据类型")
        session=request.state.auth_session
        response.headers["Cache-Control"]="no-store"
        try:
            return portfolios.page(session.tenant_id,session.user_id,run_id,kind=kind,limit=limit,offset=offset)
        except LookupError:
            raise HTTPException(404,"组合报告不存在或尚未完成") from None
        except ValueError:
            raise HTTPException(409,"组合冻结证据校验失败，请稍后重试") from None

    @router.get("/discoveries")
    def discoveries(
        request: Request, response: Response, offset: int = Query(default=0, ge=0, le=1_000_000)
    ) -> dict:
        session = request.state.auth_session
        response.headers["Cache-Control"] = "no-store"
        with pool.transaction(session.tenant_id, session.user_id) as conn:
            rows = conn.execute(
                "SELECT id,name,trial_count,created_at FROM research_discovery_reports ORDER BY created_at DESC,id DESC LIMIT 11 OFFSET %s",
                (offset,),
            ).fetchall()
            total = conn.execute("SELECT count(*) FROM research_discovery_reports").fetchone()[0]
        return {
            "items": [
                dict(zip(("id", "name", "trial_count", "created_at"), r, strict=True)) for r in rows[:10]
            ],
            "total": total,
            "has_more": len(rows) > 10,
        }

    @router.get("/discoveries/{identifier}")
    def discovery(
        identifier: UUID, request: Request, response: Response, include_details: bool = False
    ) -> dict:
        session = request.state.auth_session
        response.headers["Cache-Control"] = "no-store"
        with pool.transaction(session.tenant_id, session.user_id) as conn:
            row = conn.execute(
                "SELECT CASE WHEN %s THEN report_json ELSE report_json-'details' END,artifact_digest,report_json ? 'details' FROM research_discovery_reports WHERE id=%s",
                (include_details, identifier),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "挖掘报告不存在")
        if row[2] and not include_details:
            summary = {**row[0], "source_artifact_digest": row[1]}
            return {**summary, "artifact_digest": report_digest(summary)}
        return {**row[0], "artifact_digest": row[1]}

    @router.get("/discoveries/{identifier}/directional/{key}")
    def directional_detail(identifier: UUID, key: str, request: Request, response: Response) -> dict:
        session = request.state.auth_session
        response.headers["Cache-Control"] = "no-store"
        with pool.transaction(session.tenant_id, session.user_id) as conn:
            row = conn.execute(
                "SELECT report_json->'details'->%s FROM research_discovery_reports WHERE id=%s AND report_json->>'kind'='DIRECTIONAL'",
                (key, identifier),
            ).fetchone()
        if row is None or row[0] is None:
            raise HTTPException(404, "方向策略详情不存在")
        return {**row[0], "artifact_digest": report_digest(row[0])}

    @router.get("/strategies")
    def catalog() -> dict:
        from gatescope.quant.pattern_study import catalog as structure_catalog
        from gatescope.quant.structure_research import ENGINE_VERSION as STRUCTURE_ENGINE
        return {
            "items": CATALOG,
            "rules": rule_catalog(),
            "defaults": StrategySpec(availability_mode="HISTORICAL_CLOSE").model_dump(),
            "engine_version": ENGINE_VERSION,
            "structure_catalog": structure_catalog(),
            "structure_engine_version": STRUCTURE_ENGINE,
            "performance_policy": {
                "version": PERFORMANCE_POLICY,
                "thresholds_enforced": False,
                "observation_metrics": [item[1] for item in THRESHOLDS],
                "execution_permissions_changed": False,
            },
        }

    @router.post("/backtests", status_code=202)
    def create(payload: BacktestRequest, request: Request, response: Response) -> dict:
        session = request.state.auth_session
        if session.role.value == "VIEWER":
            raise HTTPException(403, "观察者可查看回测，创建需要研究员权限")
        allowed, retry = limiter.allow(f"research:{session.tenant_id}:{session.user_id}", 6, 60)
        if not allowed:
            raise HTTPException(429, "回测提交过于频繁", headers={"Retry-After": str(retry)})
        try:
            result = repository.create(
                session.tenant_id,
                session.user_id,
                payload.instrument_id,
                payload.timeframe,
                payload.spec,
                payload.bar_limit,
                payload.from_time,
                payload.to_time,
            )
        except UniqueViolation:
            raise HTTPException(409, "已有回测正在执行，请等待完成") from None
        except ForeignKeyViolation:
            raise HTTPException(404, "标的不存在") from None
        response.headers["Location"] = f"/api/v2/research/backtests/{result['id']}"
        return result

    @router.get("/backtests")
    def list_runs(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=1_000_000),
        instrument_id: UUID | None = None,
        timeframe: str = Query(default="", pattern=r"^(|1m|5m|15m|30m|1h|4h|8h|1d)$"),
        screening: str = Query(default="", pattern=r"^(|PASS|REJECTED|OBSERVE_ONLY|UNASSESSED)$"),
        engine_mode: str = Query(default="", pattern=r"^(|INDICATOR|STRUCTURE)$"),
    ) -> dict:
        session = request.state.auth_session
        rows = repository.list(
            session.tenant_id,
            session.user_id,
            limit=limit + 1,
            offset=offset,
            instrument=instrument_id,
            timeframe=timeframe,
            screening=screening,
            engine_mode=engine_mode,
        )
        return {
            "items": rows[:limit],
            "total": repository.count(
                session.tenant_id,
                session.user_id,
                instrument=instrument_id,
                timeframe=timeframe,
                screening=screening,
                engine_mode=engine_mode,
            ),
            "has_more": len(rows) > limit,
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    @router.get("/backtests/{run_id}")
    def get_run(run_id: UUID, request: Request, response: Response) -> dict:
        session = request.state.auth_session
        response.headers["Cache-Control"] = "no-store"
        try:
            return repository.get(session.tenant_id, session.user_id, run_id)
        except LookupError:
            raise HTTPException(404, "回测不存在") from None

    @router.get("/coverage")
    def coverage(
        instrument_id: UUID, timeframe: str = Query(default="1h", pattern=r"^(1m|5m|15m|30m|1h|4h|8h|1d)$"),
        limit: int = Query(default=550000, ge=300, le=550000),
        from_time: datetime | None = None, to_time: datetime | None = None,
    ) -> dict:
        if any(value is not None and value.tzinfo is None for value in (from_time, to_time)):
            raise HTTPException(422,"区间时间必须包含时区")
        if from_time and to_time and from_time >= to_time:
            raise HTTPException(422,"起点必须早于终点")
        return {
            **repository.coverage(instrument_id, timeframe, limit=limit, start=from_time, end=to_time),
            "cache": PostgresMarketReadDatabase(pool).cache_status(str(instrument_id), timeframe),
        }

    @router.get("/backtests/{run_id}/indicator/chart")
    def indicator_chart(run_id: UUID, request: Request, response: Response,
                        offset: int | None = Query(default=None,ge=0,le=550000),
                        limit: int = Query(default=512,ge=50,le=2000),
                        trade_index: int | None = Query(default=None,ge=0,le=550000)) -> dict:
        session = request.state.auth_session
        response.headers["Cache-Control"] = "no-store"
        try:
            return indicators.chart(session.tenant_id,session.user_id,run_id,
                offset=offset,limit=limit,trade_index=trade_index)
        except LookupError:
            raise HTTPException(404,"年度指标报告或交易不存在") from None
        except ValueError:
            raise HTTPException(409,"冻结图表校验失败，请稍后重试") from None

    @router.get("/backtests/{run_id}/indicator/{kind}")
    def indicator_evidence(run_id: UUID, kind: str, request: Request, response: Response,
                           limit: int = Query(default=25,ge=1,le=512),
                           offset: int = Query(default=0,ge=0,le=550000)) -> dict:
        if kind not in {"inputs","signals","trades","curve","drawdown"}:
            raise HTTPException(404,"未知的研究证据类型")
        session = request.state.auth_session
        response.headers["Cache-Control"] = "no-store"
        try:
            return indicators.page(session.tenant_id,session.user_id,run_id,kind=kind,limit=limit,offset=offset)
        except LookupError:
            raise HTTPException(404,"年度指标报告不存在或尚未完成") from None
        except ValueError:
            raise HTTPException(409,"冻结证据校验失败，请稍后重试") from None

    @router.get("/backtests/{run_id}/cases")
    def structure_cases(run_id: UUID, request: Request, response: Response,
                        limit: int = Query(default=25,ge=1,le=100), offset: int = Query(default=0,ge=0,le=1_000_000),
                        state: str = Query(default="",pattern=r"^(|WAITING_CONFIRMATION|WAITING_ENTRY|OPEN|TARGET|STOP|TIMEOUT|MISSED|UNAVAILABLE|CENSORED)$")) -> dict:
        session = request.state.auth_session
        response.headers["Cache-Control"] = "no-store"
        try:
            return structures.cases(session.tenant_id,session.user_id,run_id,limit=limit,offset=offset,state=state)
        except LookupError:
            raise HTTPException(404,"回测不存在") from None

    @router.get("/backtests/{run_id}/events")
    @router.get("/backtests/{run_id}/inputs")
    def structure_evidence(run_id: UUID, request: Request, response: Response,
                           limit: int = Query(default=25,ge=1,le=200),offset: int = Query(default=0,ge=0,le=1_000_000)) -> dict:
        session = request.state.auth_session
        response.headers["Cache-Control"] = "no-store"
        try:
            return structures.evidence(session.tenant_id,session.user_id,run_id,
                kind="inputs" if request.url.path.endswith("/inputs") else "events",limit=limit,offset=offset)
        except LookupError:
            raise HTTPException(404,"回测不存在") from None

    @router.get("/trend")
    def trend(
        instrument_id: UUID,
        request: Request,
        timeframe: str = Query(default="1h", pattern=r"^(1m|5m|15m|30m|1h|4h|8h|1d)$"),
    ) -> dict:
        session = request.state.auth_session
        allowed, _ = limiter.allow(f"trend:{session.user_id}", 30, 60)
        if not allowed:
            raise HTTPException(429, "趋势查询过于频繁")
        bars = repository.bars(instrument_id, timeframe, 250, datetime.now(timezone.utc))
        if len(bars) < 60:
            raise HTTPException(409, "趋势分析至少需要 60 根已收盘 K 线")
        if GapReconciler().find_gaps(bars):
            raise HTTPException(409, "趋势历史 K 线存在缺口，请等待行情补齐或切换周期")
        latest = features(bars, StrategySpec(direction="BOTH"))[-1]
        v = latest["values"]
        return {
            "regime": "震荡"
            if v["adx.adx"] < 20
            else "上升趋势"
            if v["fast.ema"] > v["slow.ema"]
            else "下降趋势",
            "signal": latest,
            "as_of": bars[-1].close_time,
            "quality_flags": sorted(set().union(*(b.quality_flags for b in bars))),
            "engine_version": ENGINE_VERSION,
        }

    return router
