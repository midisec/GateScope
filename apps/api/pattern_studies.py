"""Authenticated public-data chart structures and durable historical studies."""
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from psycopg.errors import ForeignKeyViolation, TooManyConnections
from pydantic import BaseModel, ConfigDict, Field

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_pattern_studies import PatternStudyRepository
from gatescope.adapters.postgres_structure_archive import StructureArchive
from gatescope.application.replay_status import replay_control
from gatescope.domain.current_patterns import InvalidCurrentPatternFilter, validate_confidence_filter
from gatescope.security import SlidingWindowLimiter


class StudyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument_id: UUID
    timeframe: str = Field(pattern=r"^(1m|5m|15m|30m|1h|4h|8h|1d)$")


def build_pattern_study_router(pool: PostgresPool) -> APIRouter:
    router = APIRouter(prefix="/api/v2/patterns", tags=["pattern-studies"])
    repository, limiter = PatternStudyRepository(pool), SlidingWindowLimiter()
    archive = StructureArchive(pool)

    def limit(request: Request, name: str, count: int):
        session = request.state.auth_session
        allowed, retry = limiter.allow(f"{name}:{session.user_id}", count, 60)
        if not allowed:
            raise HTTPException(429, "请求较频繁，请稍后重试", headers={"Retry-After": str(retry)})

    @router.get("/current")
    def current(request: Request, response: Response, instrument_id: UUID,
                timeframe: str = Query(pattern=r"^(1m|5m|15m|30m|1h|4h|8h|1d)$"),
                offset: int = Query(default=0,ge=0,le=100000), limit_count: int = Query(default=96,alias="limit",ge=1,le=96),
                phase: str | None = Query(default=None,pattern="^(FORMING|FORMED|CONFIRMED|INVALIDATED|EXPIRED|CENSORED)$"),
                family: str | None = Query(default=None,pattern="^(chart|wave)$"), q: str = Query(default="",max_length=80),
                pattern: str | None = Query(default=None,min_length=1,max_length=80),
                confidence_min: float | None = Query(default=None,ge=0,le=1,allow_inf_nan=False),
                confidence_max: float | None = Query(default=None,ge=0,le=1,allow_inf_nan=False),
                unscored_only: bool = Query(default=False)) -> dict:
        limit(request, "pattern-current", 60)
        response.headers["Cache-Control"] = "no-store"
        try:
            validate_confidence_filter(confidence_min, confidence_max, unscored_only)
            return repository.current(instrument_id, timeframe, offset=offset,limit=limit_count,phase=phase,family=family,query=q,
                pattern=pattern,confidence_min=confidence_min,confidence_max=confidence_max,unscored_only=unscored_only)
        except InvalidCurrentPatternFilter as exc:
            raise HTTPException(422, str(exc)) from None
        except ValueError:
            raise HTTPException(409, "形态历史 K 线存在缺口，请等待缓存补齐") from None
        except ForeignKeyViolation:
            raise HTTPException(404, "标的不存在") from None
        except TooManyConnections:
            raise HTTPException(429, "结构跟踪队列已满，请稍后重试", headers={"Retry-After": "60"}) from None

    @router.get("/watchlist-progress")
    def watchlist_progress(request:Request,response:Response,offset:int=Query(default=0,ge=0,le=100000),
                          limit_count:int=Query(default=40,alias="limit",ge=1,le=80))->dict:
        limit(request,"pattern-watchlist",30)
        response.headers["Cache-Control"]="no-store"
        session=request.state.auth_session
        result=repository.watchlist_progress(session.tenant_id,session.user_id,offset,limit_count)
        return {**result,"replay_control":replay_control(pool)}

    @router.get("/events")
    def events(request: Request, response: Response, instrument_id: UUID,
               timeframe: str = Query(pattern=r"^(1m|5m|15m|30m|1h|4h|8h|1d)$"),
               source: str = Query(default="RECORDED", pattern="^(RECORDED|REPLAY)$"),
               phase: str | None = Query(default=None, pattern="^(FORMING|FORMED|CONFIRMED|INVALIDATED|EXPIRED|CENSORED)$"),
               pattern: str | None = Query(default=None, max_length=60, pattern="^[a-z_]+$"),
               context: str | None = Query(default=None, pattern="^[a-f0-9]{64}$"),
               offset: int = Query(default=0, ge=0, le=100000), limit_count: int = Query(default=12, alias="limit", ge=1, le=50)) -> dict:
        limit(request,"pattern-events",60)
        response.headers["Cache-Control"] = "no-store"
        return archive.list(instrument_id,timeframe,source=source,phase=phase,pattern=pattern,offset=offset,limit=limit_count,requested_context=context)

    @router.get("/events/{context}/{candidate}")
    def event_detail(request: Request, response: Response,
                     context: str = Path(pattern="^[a-f0-9]{64}$"),
                     candidate: str = Path(pattern="^[a-f0-9]{64}$")) -> dict:
        limit(request,"pattern-detail",30)
        response.headers["Cache-Control"] = "no-store"
        result = archive.detail(context,candidate)
        if result is None:
            raise HTTPException(404,"候选档案不存在")
        return result

    @router.get("/study")
    def study(request: Request, response: Response, instrument_id: UUID,
              timeframe: str = Query(pattern=r"^(1m|5m|15m|30m|1h|4h|8h|1d)$")) -> dict:
        limit(request, "pattern-study-read", 60)
        response.headers["Cache-Control"] = "no-store"
        return repository.get(instrument_id, timeframe)

    @router.post("/study", status_code=202)
    def create(payload: StudyRequest, request: Request) -> dict:
        limit(request, "pattern-study-create", 6)
        # Even VIEWER can research public candles; this never grants execution rights.
        try:
            return repository.request(payload.instrument_id, payload.timeframe)
        except ForeignKeyViolation:
            raise HTTPException(404, "标的不存在") from None
        except TooManyConnections:
            raise HTTPException(429, "历史统计队列已满，请稍后重试", headers={"Retry-After": "60"}) from None

    return router
