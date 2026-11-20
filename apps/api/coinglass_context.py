"""Session-authenticated, owner-scoped chart evidence and separate reviews."""
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from psycopg.errors import ForeignKeyViolation
from pydantic import BaseModel, ConfigDict, Field

from gatescope.adapters.postgres_coinglass_context import PostgresCoinglassContextRepository
from gatescope.security import SlidingWindowLimiter


class Scenario(BaseModel):
    model_config=ConfigDict(extra="forbid",allow_inf_nan=False)
    entry:float=Field(gt=0)
    target_lower:float=Field(gt=0)
    target_upper:float=Field(gt=0)
    direction:str|None=Field(default=None,pattern="^(bullish|bearish)$")


class ReviewRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    instrument_id:UUID
    timeframe:str=Field(pattern="^(1m|5m|15m|30m|1h|4h|8h|1d)$")
    context_id:str=Field(pattern="^[a-f0-9]{64}$")
    candidate_id:str=Field(pattern="^[a-f0-9]{64}$")
    expected_evidence_id:str|None=Field(default=None,pattern="^[a-f0-9]{64}$")
    scenario:Scenario|None=None


def build_coinglass_context_router(pool):
    router=APIRouter(prefix="/api/v2/coinglass",tags=["coinglass-context"])
    repository,limiter=PostgresCoinglassContextRepository(pool),SlidingWindowLimiter()

    def guard(request,count=30):
        session=request.state.auth_session
        allowed,retry=limiter.allow(f"coinglass-context:{session.user_id}:{request.method}",count,60)
        if not allowed:
            raise HTTPException(429,"请求较频繁，请稍后重试",headers={"Retry-After":str(retry)})
        return session

    @router.get("/chart")
    def chart(request:Request,response:Response,instrument_id:UUID,
              timeframe:str=Query(pattern="^(1m|5m|15m|30m|1h|4h|8h|1d)$")):
        session=guard(request)
        response.headers["Cache-Control"]="no-store"
        try:
            return repository.get(session.tenant_id,session.user_id,instrument_id,timeframe,request=True)
        except ForeignKeyViolation:
            raise HTTPException(404,"标的不存在") from None

    @router.post("/reviews",status_code=201)
    def review(payload:ReviewRequest,request:Request,response:Response):
        session=guard(request,12)
        response.headers["Cache-Control"]="no-store"
        try:
            return repository.review(session.tenant_id,session.user_id,payload.instrument_id,payload.timeframe,
              payload.context_id,payload.candidate_id,payload.expected_evidence_id,
              payload.scenario.model_dump(exclude_none=True) if payload.scenario else None)
        except LookupError:
            raise HTTPException(404,"该形态已不在当前跟踪窗口，请刷新列表") from None
        except ValueError as exc:
            if str(exc) in {"CURRENT_CONTEXT_REQUIRED","PATTERN_EVIDENCE_CHANGED","PATTERN_NO_LONGER_FORMING"}:
                raise HTTPException(409,"形态证据已更新，请刷新列表后重新研判") from None
            raise HTTPException(422,"情景参数不符合原结构，请检查区间、方向和失效位") from None

    @router.get("/reviews/{review_id}")
    def detail(review_id:UUID,request:Request,response:Response):
        session=guard(request)
        response.headers["Cache-Control"]="no-store"
        result=repository.review_detail(session.tenant_id,session.user_id,review_id)
        if result is None:
            raise HTTPException(404,"研判记录不存在")
        return result

    return router
