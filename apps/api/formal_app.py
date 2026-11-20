"""PostgreSQL API composition root for formal multi-tenant identity and V2 reads."""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Literal, TypeVar
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from apps.api.v2 import build_v2_router
from apps.api.research import build_research_router
from apps.api.shadow_trades import build_shadow_router
from apps.api.paper_orders import build_paper_orders_router
from apps.api.pattern_studies import build_pattern_study_router
from apps.api.coinglass_context import build_coinglass_context_router
from apps.api.volume_profile import build_volume_profile_router
from apps.api.admin_console import build_admin_console_router
from apps.api.mcp_market_preview import build_mcp_market_preview_router
from apps.api.mcp_oauth import build_mcp_oauth_router
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_market import PostgresMarketReadDatabase
from gatescope.adapters.postgres_exchange_accounts import (
    ExchangeAccount,
    ExchangeAccountConflict,
    ExchangeAccountNotFound,
    PostgresExchangeAccountRepository,
)
from gatescope.adapters.postgres_testnet_execution import (
    PostgresTestnetExecutionRepository,
    StrategyExecutionCertification,
    TestnetDrill,
    TestnetDrillConflict,
    TestnetDrillNotFound,
)
from gatescope.adapters.postgres_mcp import (
    MCP_SCOPES,
    McpClient,
    McpConflict,
    McpNotFound,
    McpToken,
    PostgresMcpRepository,
)
from gatescope.adapters.postgres_notifications import (
    NotificationChannel,
    NotificationConflict,
    NotificationEvent,
    NotificationNotFound,
    NotificationPreconditionFailed,
    NotificationRoute,
    PostgresNotificationRepository,
    TelegramConnection,
)
from gatescope.adapters.postgres_telegram_config import (
    PostgresTelegramConfigRepository,
    TelegramBotConfig,
    TelegramConfigNotFound,
)
from gatescope.adapters.postgres_paper import (
    ApprovalResult,
    ExecutionDecision,
    PaperAccount,
    PaperConflict,
    PaperNotFound,
    PaperPreconditionFailed,
    PaperProposal,
    PaperTrade,
    PostgresPaperRepository,
    SignalEvidence,
)
from gatescope.adapters.postgres_alert_rules import (
    AlertRule,
    AlertRuleConflict,
    AlertRuleNotFound,
    AlertRulePreconditionFailed,
    PostgresAlertRuleRepository,
)
from gatescope.adapters.postgres_resources import (
    PostgresResourceRepository,
    ResourceConflict,
    ResourceNotFound,
    ResourcePreconditionFailed,
    VersionedResource,
)
from gatescope.adapters.postgres_watchlists import (
    PostgresWatchlistRepository,
    Watchlist,
    WatchlistConflict,
    WatchlistNotFound,
    WatchlistPreconditionFailed,
)
from gatescope.adapters.coinglass import (
    COINGLASS_ORIGIN,
    SAFE_API_KEY,
    CoinglassClient,
    CoinglassError,
    CoinglassRateLimited,
)
from gatescope.adapters.postgres_coinglass import (
    CoinglassConfig,
    CoinglassConfigDisabled,
    CoinglassConfigNotFound,
    CoinglassTestJob,
    PostgresCoinglassRepository,
)
from gatescope.auth import CSRF_COOKIE, SESSION_COOKIE
from gatescope.domain.identity import (
    Action,
    AuthorizationContext,
    AuthorizationDenied,
    Role,
    authorize,
)
from gatescope.formal_auth import (
    FormalAuthError,
    FormalIdentityService,
    FormalInvalidCredentials,
    IdentityConflict,
    IdentityPreconditionFailed,
    LastMfaMethodRequired,
    LastOwnerRequired,
    PasswordReuse,
)
from gatescope.security import SecurityMiddleware, SlidingWindowLimiter, error_response
from gatescope.security_secrets import AesGcmSecretProtector
from gatescope.credential_crypto import CredentialMaterial, RsaOaepCredentialEncryptor
from gatescope.submission_crypto import (
    SUBMISSION_ALGORITHM,
    SUBMISSION_MAX_AGE_SECONDS,
    SUBMISSION_MAX_FUTURE_SKEW_SECONDS,
    SUBMISSION_MAX_PLAINTEXT_BYTES,
    EncryptedSubmissionRequest,
    RsaOaepSubmissionDecryptor,
    SubmissionError,
)
from gatescope.application.notifications import TelegramBotAdmin


class FormalLoginRequest(BaseModel):
    tenant: str = Field(min_length=3, max_length=63, pattern=r"^[a-zA-Z0-9-]+$")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


FormalLoginSubmission = FormalLoginRequest | EncryptedSubmissionRequest


class FormalReauthenticateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=256)


FormalReauthenticateSubmission = FormalReauthenticateRequest | EncryptedSubmissionRequest


class MfaCodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


class MfaEnrollRequest(BaseModel):
    label: str = Field(default="Primary", min_length=1, max_length=80)


class RecoveryCodeRequest(BaseModel):
    code: str = Field(min_length=8, max_length=32)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=16, max_length=256)


class UserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64)
    initial_password: str = Field(min_length=16, max_length=256)
    role: Role

    @field_validator("initial_password")
    @classmethod
    def password_bytes(cls, value: str) -> str:
        from gatescope.auth import validate_password
        validate_password(value)
        return value


UserCreateSubmission = UserCreateRequest | EncryptedSubmissionRequest


class MembershipUpdateRequest(BaseModel):
    role: Role
    status: str = Field(pattern=r"^(ACTIVE|REVOKED)$")


class ResourceWriteRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    spec: dict[str, Any]


class WatchlistWriteRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class WatchlistItemRequest(BaseModel):
    instrument_id: UUID
    default_timeframe: str = Field(pattern=r"^(1m|5m|15m|30m|1h|4h|8h|1d)$")


class AlertConditionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    instrument_id: UUID
    timeframe: str = Field(pattern=r"^(1m|5m|15m|30m|1h|4h|8h|1d)$")
    pattern_ids: list[str] = Field(min_length=1, max_length=28)
    match_mode: Literal["ANY", "ALL"] = "ANY"
    minimum_confidence: float = Field(default=0.65, ge=0, le=1)
    confirmed_only: bool = True
    coincidence_bars: int = Field(default=3, ge=1, le=100)
    cooldown_seconds: int = Field(default=900, ge=0, le=604_800)

    @field_validator("pattern_ids")
    @classmethod
    def valid_pattern_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("pattern_ids must be unique")
        if any(re.fullmatch(r"[a-z][a-z0-9_]{1,63}", item) is None for item in value):
            raise ValueError("pattern_ids contains an invalid identifier")
        from gatescope.quant.pattern_study import catalog

        if any(item not in {pattern['id'] for pattern in catalog()} for item in value):
            raise ValueError("pattern_ids contains an unsupported pattern")
        return value


class AlertRoutingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    channel_ids: list[UUID] = Field(default_factory=list, max_length=20)

    @field_validator("channel_ids")
    @classmethod
    def unique_channels(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("channel_ids must be unique")
        return value


class AlertRuleCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    condition: AlertConditionRequest
    routing: AlertRoutingRequest


class AlertRuleRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class AlertRuleVersionRequest(BaseModel):
    condition: AlertConditionRequest
    routing: AlertRoutingRequest


class NotificationChannelRequest(BaseModel):
    alias: str = Field(min_length=1, max_length=80)
    connection_id: UUID


class TelegramBotConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot_token: str = Field(min_length=37, max_length=149)
    allowed_user_ids: list[int] = Field(default_factory=list, max_length=100)
    current_password: str = Field(min_length=1, max_length=256)
    mfa_code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")

    @field_validator("bot_token")
    @classmethod
    def valid_bot_token(cls, value: str) -> str:
        if value != value.strip() or re.fullmatch(r"[0-9]{6,20}:[A-Za-z0-9_-]{30,128}", value) is None:
            raise ValueError("invalid Telegram Bot token")
        return value

    @field_validator("allowed_user_ids")
    @classmethod
    def valid_allowed_user_ids(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value) or any(item <= 0 or item >= 2**63 for item in value):
            raise ValueError("invalid Telegram user allowlist")
        return value


TelegramBotConfigSubmission = TelegramBotConfigRequest | EncryptedSubmissionRequest


class CoinglassConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=35, max_length=131)
    current_password: str = Field(min_length=1, max_length=256)
    mfa_code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")

    @field_validator("api_key")
    @classmethod
    def valid_api_key(cls, value: str) -> str:
        if value != value.strip() or SAFE_API_KEY.fullmatch(value) is None:
            raise ValueError("invalid Coinglass API key")
        return value


CoinglassConfigSubmission = CoinglassConfigRequest | EncryptedSubmissionRequest


class QuietHoursRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    start: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    end: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown IANA timezone") from exc
        return value


class QuietBypassRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal["PROPOSAL", "FILL", "TP_SL", "RISK_CRITICAL", "RESEARCH_SIGNAL", "SYSTEM"]
    environments: list[Literal["PAPER", "TESTNET", "LIVE"]] = Field(default_factory=list, max_length=3)


class NotificationRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    schema_version: Literal[1] = 1
    channel_id: UUID
    event_types: list[
        Literal["PROPOSAL", "FILL", "TP_SL", "RISK_CRITICAL", "RESEARCH_SIGNAL", "SYSTEM"]
    ] = Field(min_length=1, max_length=6)
    strategy_ids: list[UUID] = Field(default_factory=list, max_length=100)
    exchange_account_ids: list[UUID] = Field(default_factory=list, max_length=100)
    symbols: list[str] = Field(default_factory=list, max_length=100)
    environments: list[Literal["PAPER", "TESTNET", "LIVE"]] = Field(default_factory=list, max_length=3)
    minimum_severity: Literal["INFO", "WARNING", "CRITICAL"] = "INFO"
    quiet_hours: QuietHoursRequest | None = None
    digest_mode: bool = False
    max_per_minute: int = Field(default=10, ge=1, le=60)
    dedupe_seconds: int = Field(default=60, ge=0, le=86_400)
    bypass_quiet_rules: list[QuietBypassRequest] = Field(
        default_factory=lambda: [QuietBypassRequest(event_type="RISK_CRITICAL")],
        max_length=6,
    )
    enabled: bool = True

    @field_validator("event_types", "strategy_ids", "exchange_account_ids", "environments")
    @classmethod
    def unique_values(cls, value: list[Any]) -> list[Any]:
        if len(set(value)) != len(value):
            raise ValueError("values must be unique")
        return value

    @field_validator("symbols")
    @classmethod
    def valid_symbols(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().upper() for item in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("symbols must be unique")
        if any(re.fullmatch(r"[A-Z0-9]{1,20}[_/-][A-Z0-9]{1,20}", item) is None for item in normalized):
            raise ValueError("symbols contains an invalid identifier")
        return normalized

    @model_validator(mode="after")
    def risk_critical_always_bypasses_quiet_hours(self) -> "NotificationRouteRequest":
        if "RISK_CRITICAL" in self.event_types and not any(
            item.event_type == "RISK_CRITICAL" and not item.environments
            for item in self.bypass_quiet_rules
        ):
            raise ValueError("RISK_CRITICAL must bypass quiet hours in every environment")
        return self


class McpClientCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)


McpScope = Literal["mcp:connect", "market:read", "analysis:read", "alerts:read", "research:read", "paper:read", "paper:trade"]


def _default_mcp_scopes() -> list[McpScope]:
    return ["mcp:connect", "market:read", "analysis:read"]


class McpTokenCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=80)
    scopes: list[McpScope] = Field(
        default_factory=_default_mcp_scopes,
        min_length=1,
        max_length=7,
    )
    ttl_hours: int = Field(default=720, ge=1, le=2_160)
    mfa_code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")

    @field_validator("scopes")
    @classmethod
    def valid_scopes(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("scopes must be unique")
        if "mcp:connect" not in value or not set(value) <= MCP_SCOPES:
            raise ValueError("mcp:connect and only supported scopes are allowed")
        if "paper:trade" in value and "paper:read" not in value:
            raise ValueError("paper:trade requires paper:read")
        return value


class PaperAccountCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    base_currency: str = Field(default="USDT", pattern=r"^[A-Za-z0-9]{2,12}$")
    initial_equity: Decimal = Field(default=Decimal("10000"), gt=0, le=Decimal("1000000000"))
    risk_fraction: Decimal = Field(default=Decimal("0.0025"), gt=0, le=Decimal("0.005"))
    max_notional_fraction: Decimal = Field(default=Decimal("0.10"), gt=0, le=Decimal("0.10"))
    fee_bps_per_side: Decimal = Field(default=Decimal("5"), ge=0, le=100)
    market_slippage_bps: Decimal = Field(default=Decimal("3"), ge=0, le=100)
    max_entry_drift_bps: Decimal = Field(default=Decimal("50"), gt=0, le=100)


class ExecutionDecisionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_account_id: UUID
    client_request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class ProposalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_decision_id: UUID
    client_request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class ProposalVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_version: int = Field(ge=1)


class ProposalCancelRequest(ProposalVersionRequest):
    client_request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class ExchangeAccountCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1, max_length=80)
    venue: Literal["GATE"] = "GATE"
    environment: Literal["TESTNET"] = "TESTNET"
    market_type: Literal["FUTURES_USDT"] = "FUTURES_USDT"
    api_key: str = Field(min_length=16, max_length=128)
    api_secret: str = Field(min_length=16, max_length=256)
    acknowledge_no_withdrawal: Literal[True]

    @field_validator("api_key", "api_secret")
    @classmethod
    def valid_credential_component(cls, value: str) -> str:
        if value != value.strip() or not value.isascii() or any(character.isspace() for character in value):
            raise ValueError("credential component must be bounded ASCII without whitespace")
        return value


ExchangeAccountCreateSubmission = ExchangeAccountCreateRequest | EncryptedSubmissionRequest


class ExchangeCredentialRotateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=16, max_length=128)
    api_secret: str = Field(min_length=16, max_length=256)
    acknowledge_no_withdrawal: Literal[True]

    @field_validator("api_key", "api_secret")
    @classmethod
    def valid_credential_component(cls, value: str) -> str:
        return ExchangeAccountCreateRequest.valid_credential_component(value)


ExchangeCredentialRotateSubmission = ExchangeCredentialRotateRequest | EncryptedSubmissionRequest


class ExchangeTestnetArmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mfa_code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")
    acknowledge_testnet_only: Literal[True]
    acknowledge_ip_whitelist: Literal[True]
    acknowledge_no_withdrawal: Literal[True]
    acknowledge_exclusive_account: Literal[True]


class TestnetDrillCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_public_trade_id: str = Field(pattern=r"^paper_[0-9A-HJKMNP-TV-Z]{26}$")
    exchange_account_id: UUID
    mfa_code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


def create_formal_app() -> FastAPI:
    database_url = _database_url()
    pool = PostgresPool(database_url)
    identity = FormalIdentityService(
        pool,
        AesGcmSecretProtector(_required_secret("GATESCOPE_IDENTITY_KEK", exact=32), _key_version()),
        _required_secret("GATESCOPE_RECOVERY_PEPPER", minimum=32),
    )
    cursor_secret = _required_secret("GATESCOPE_CURSOR_SIGNING_KEY", minimum=32)
    credential_encryptor = _optional_credential_encryptor()
    telegram_config_encryptor = _optional_telegram_config_encryptor()
    coinglass_config_encryptor = _optional_coinglass_config_encryptor()
    submission_decryptor = _optional_submission_decryptor()
    public_origin = (os.environ.get("GATESCOPE_PUBLIC_ORIGIN") or "http://127.0.0.1:8777").rstrip("/")
    credential_storage_ready = credential_encryptor is not None
    browser_submission_ready = submission_decryptor is not None
    cookie_secure = public_origin.startswith("https://")
    telegram_bot_username = os.environ.get("GATESCOPE_TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
    telegram_webhook_id = os.environ.get("GATESCOPE_TELEGRAM_WEBHOOK_ID", "").strip()
    telegram_webhook_secret = os.environ.get("GATESCOPE_TELEGRAM_WEBHOOK_SECRET", "").strip()
    telegram_allowed_users = _positive_id_set(os.environ.get("GATESCOPE_TELEGRAM_ALLOWED_USER_IDS", ""))
    coinglass_api_key = os.environ.get("GATESCOPE_COINGLASS_API_KEY", "").strip()
    coinglass = CoinglassClient(coinglass_api_key) if coinglass_api_key else None
    login_workspaces = _login_workspace_slugs()
    telegram_values = (telegram_bot_username, telegram_webhook_id, telegram_webhook_secret)
    if any(telegram_values) and not all(telegram_values):
        raise RuntimeError("Telegram API configuration must include bot username, webhook id and webhook secret")
    if telegram_webhook_secret and re.fullmatch(r"[A-Za-z0-9_-]{16,256}", telegram_webhook_secret) is None:
        raise RuntimeError("GATESCOPE_TELEGRAM_WEBHOOK_SECRET has an invalid format")
    if telegram_webhook_id and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", telegram_webhook_id) is None:
        raise RuntimeError("GATESCOPE_TELEGRAM_WEBHOOK_ID has an invalid format")
    if telegram_bot_username and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,63}", telegram_bot_username) is None:
        raise RuntimeError("GATESCOPE_TELEGRAM_BOT_USERNAME has an invalid format")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool.open()
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(title="GateScope V2", version="2.0.0-phase1", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.pool = pool
    app.state.identity = identity
    app.add_middleware(
        SecurityMiddleware,
        auth=identity,
        public_origin=public_origin,
        cookie_secure=cookie_secure,
        limiter=SlidingWindowLimiter(),
    )
    login_limiter = SlidingWindowLimiter()
    reauthentication_limiter = SlidingWindowLimiter()
    resources = PostgresResourceRepository(pool)
    watchlists = PostgresWatchlistRepository(pool)
    alert_rules = PostgresAlertRuleRepository(pool)
    notifications = PostgresNotificationRepository(pool)
    telegram_configs = PostgresTelegramConfigRepository(pool)
    from gatescope.adapters.postgres_telegram_market import TelegramMarketRepository
    from gatescope.application.telegram_market import market_command, callback_update
    telegram_market=TelegramMarketRepository(pool)
    coinglass_configs = PostgresCoinglassRepository(pool)
    telegram_admin = TelegramBotAdmin()
    mcp_access = PostgresMcpRepository(pool)
    paper = PostgresPaperRepository(pool)
    exchange_accounts = PostgresExchangeAccountRepository(pool)
    testnet_execution = PostgresTestnetExecutionRepository(pool)
    credential_limiter = SlidingWindowLimiter()
    provider_limiter = SlidingWindowLimiter()

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = str(exc.detail) if exc.status_code < 500 else "服务暂不可用"
        return error_response(exc.status_code, f"HTTP_{exc.status_code}", message, request.state.trace_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(422, "INVALID_REQUEST", "请求参数无效", request.state.trace_id)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        return error_response(500, "INTERNAL_ERROR", "服务暂不可用", request.state.trace_id)

    @app.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "ok", "service": "gatescope-api-v2"}

    @app.get("/health/ready")
    def health_ready() -> JSONResponse:
        healthy = pool.healthy()
        return JSONResponse(
            {"status": "ready" if healthy else "not_ready", "checks": {"database": "ok" if healthy else "unavailable"}},
            status_code=200 if healthy else 503,
        )

    @app.get("/api/v2/security/submission-key", response_model=None)
    def submission_key(request: Request, response: Response) -> Any:
        if submission_decryptor is None:
            return error_response(
                503,
                "SUBMISSION_ENCRYPTION_UNAVAILABLE",
                "应用层加密暂不可用",
                request.state.trace_id,
            )
        key_info = submission_decryptor.key_info()
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return {
            "algorithm": SUBMISSION_ALGORITHM,
            "key_version": key_info.key_version,
            "public_key_spki": key_info.public_key_spki,
            "rsa_hash": "SHA-256",
            "content_encryption": "A256GCM",
            "max_age_seconds": SUBMISSION_MAX_AGE_SECONDS,
            "max_future_skew_seconds": SUBMISSION_MAX_FUTURE_SKEW_SECONDS,
            "max_plaintext_bytes": SUBMISSION_MAX_PLAINTEXT_BYTES,
            "trace_id": request.state.trace_id,
        }

    @app.get("/auth/workspaces")
    def workspaces() -> dict[str, Any]:
        with pool.public_transaction() as connection:
            rows = connection.execute(
                "SELECT slug,name FROM tenants WHERE status='ACTIVE' AND slug=ANY(%s)",
                (list(login_workspaces),),
            ).fetchall()
        names = {str(row[0]): str(row[1]) for row in rows}
        return {
            "items": [
                {"slug": slug, "name": names[slug]}
                for slug in login_workspaces
                if slug in names
            ]
        }

    def worker_health(workload: str) -> dict[str, Any]:
        if workload not in {"market-worker", "strategy-worker", "research-worker"}:
            raise ValueError("unsupported workload")
        try:
            with pool.public_transaction() as connection:
                row = connection.execute(
                    """SELECT worker_id,running,heartbeat_at,status_json
                         FROM worker_heartbeats WHERE workload=%s""",
                    (workload,),
                ).fetchone()
        except Exception:
            return {"configured": True, "healthy": False, "state": "UNAVAILABLE"}
        if row is None:
            return {"configured": True, "healthy": False, "state": "NOT_STARTED"}
        heartbeat_at = row[2]
        fresh = bool(row[1]) and heartbeat_at >= datetime.now(timezone.utc) - timedelta(seconds=60)
        status = dict(row[3]) if isinstance(row[3], dict) else {}
        return {
            "configured": True,
            "healthy": fresh and not bool(status.get("degraded")),
            "state": "HEALTHY" if fresh and not status.get("degraded") else "DEGRADED",
            "heartbeat_at": heartbeat_at,
            "worker_id": str(row[0]),
            "status": status,
        }

    @app.get("/api/v2/data-sources", response_model=None)
    def data_sources(request: Request) -> Any:
        denied = _require_owner(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        web_telegram = telegram_configs.get(session.tenant_id, session.user_id)
        web_coinglass = coinglass_configs.get(session.tenant_id, session.user_id)
        configured_bot_username = (
            web_telegram.bot_username
            if web_telegram is not None and web_telegram.status == "ACTIVE"
            else telegram_bot_username
        )
        worker = worker_health("market-worker")
        strategy_worker = worker_health("strategy-worker")
        research_worker = worker_health("research-worker")
        return {
            "items": [
                {
                    "id": "gate", "name": "Gate 公共行情", "configured": True,
                    "state": worker["state"], "secret_location": "NONE_REQUIRED",
                },
                {
                    "id": "coinglass", "name": "Coinglass / LSR",
                    "configured": (
                        web_coinglass is not None and web_coinglass.status != "DISABLED"
                    ) or coinglass is not None,
                    "state": (
                        web_coinglass.status if web_coinglass is not None
                        else "READY_TO_TEST" if coinglass is not None else "SECRET_REQUIRED"
                    ),
                    "origin": COINGLASS_ORIGIN,
                    "secret_location": (
                        "MARKET_WORKER_ONLY"
                        if web_coinglass is not None or coinglass is None
                        else "LEGACY_SERVER_SECRET"
                    ),
                    "key_fingerprint": (
                        web_coinglass.key_fingerprint if web_coinglass is not None else None
                    ),
                    "last_tested_at": (
                        web_coinglass.last_tested_at if web_coinglass is not None else None
                    ),
                    "automatic_feature_eligible": False,
                },
                {
                    "id": "telegram", "name": "Telegram Bot",
                    "configured": bool(configured_bot_username),
                    "state": (
                        web_telegram.status if web_telegram is not None
                        else "CONFIGURED" if telegram_bot_username else "SECRET_REQUIRED"
                    ),
                    "bot_username": configured_bot_username or None,
                    "secret_location": "NOTIFICATION_WORKER_ONLY",
                },
            ],
            "market_worker": worker,
            "strategy_worker": strategy_worker,
            "research_worker": research_worker,
            "credential_writes_require_https": False,
            "browser_submission_configured": browser_submission_ready,
            "trace_id": request.state.trace_id,
        }

    @app.get("/api/v2/data-sources/coinglass/config", response_model=None)
    def get_coinglass_config(request: Request) -> Any:
        denied = _require_owner(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        config = coinglass_configs.get(session.tenant_id, session.user_id)
        latest_test = coinglass_configs.latest_test(session.tenant_id, session.user_id)
        payload = _coinglass_config_payload(
            config,
            secret_write_allowed=(
                coinglass_config_encryptor is not None
                and (browser_submission_ready or _legacy_plaintext_submission_allowed(request))
            ),
            browser_submission_configured=browser_submission_ready,
            trace_id=request.state.trace_id,
        )
        return {**payload, "latest_test": _coinglass_test_payload(latest_test) if latest_test else None}

    @app.put(
        "/api/v2/data-sources/coinglass/config", status_code=202, response_model=None,
    )
    def configure_coinglass(payload: CoinglassConfigSubmission, request: Request) -> Any:
        denied = _require_owner(request)
        if denied is not None:
            return denied
        if coinglass_config_encryptor is None:
            return error_response(
                503, "COINGLASS_SECRET_STORAGE_UNAVAILABLE",
                "Coinglass 加密存储尚未初始化", request.state.trace_id,
            )
        session = request.state.auth_session
        resolved = _resolve_sensitive_submission(
            payload,
            request,
            decryptor=submission_decryptor,
            expected_purpose=f"tenant:{session.tenant_id}:coinglass_config",
            model_type=CoinglassConfigRequest,
        )
        if isinstance(resolved, JSONResponse):
            return resolved
        try:
            session = identity.reauthenticate_password(
                session, resolved.current_password, trace_id=request.state.trace_id,
            )
            identity.verify_session_mfa(
                session, resolved.mfa_code, trace_id=request.state.trace_id,
            )
        except FormalInvalidCredentials:
            return error_response(
                401, "INVALID_CREDENTIALS_OR_MFA",
                "当前密码或 MFA 验证码无效，验证码也可能已使用",
                request.state.trace_id,
            )
        allowed, retry_after = provider_limiter.allow(
            f"coinglass-config:{session.tenant_id}", 3, 60,
        )
        if not allowed:
            return error_response(
                429, "COINGLASS_CONFIG_RATE_LIMITED", "配置过于频繁，请稍后重试",
                request.state.trace_id, **{"Retry-After": str(retry_after)},
            )
        try:
            config, job = coinglass_configs.save_and_enqueue(
                session.tenant_id,
                session.user_id,
                api_key=resolved.api_key,
                test_symbol="BTC",
                encryptor=coinglass_config_encryptor,
            )
        except ValueError:
            return error_response(
                422, "COINGLASS_CONFIG_INVALID", "Coinglass 配置无效",
                request.state.trace_id,
            )
        return {
            "config": _coinglass_config_payload(
                config,
                secret_write_allowed=True,
                browser_submission_configured=browser_submission_ready,
                trace_id=request.state.trace_id,
            ),
            "test": _coinglass_test_payload(job),
            "trace_id": request.state.trace_id,
        }

    @app.post("/api/v2/data-sources/coinglass:test", response_model=None)
    def test_coinglass(request: Request, response: Response) -> Any:
        denied = _require_owner(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        allowed, retry_after = provider_limiter.allow(f"coinglass-test:{session.tenant_id}", 3, 60)
        if not allowed:
            result = error_response(
                429, "COINGLASS_TEST_RATE_LIMITED", "测试过于频繁，请稍后重试",
                request.state.trace_id,
            )
            result.headers["Retry-After"] = str(retry_after)
            return result
        web_config = coinglass_configs.get(session.tenant_id, session.user_id)
        if web_config is not None:
            try:
                job = coinglass_configs.enqueue_test(
                    session.tenant_id, session.user_id, test_symbol="BTC",
                )
            except CoinglassConfigDisabled:
                return error_response(
                    409, "COINGLASS_CONFIG_DISABLED", "Coinglass 配置已停用",
                    request.state.trace_id,
                )
            except RuntimeError:
                return error_response(
                    409, "COINGLASS_TEST_ALREADY_QUEUED", "检测任务已经在执行",
                    request.state.trace_id,
                )
            response.status_code = 202
            response.headers["Location"] = f"/api/v2/data-sources/coinglass/tests/{job.id}"
            response.headers["Cache-Control"] = "no-store"
            return {"test": _coinglass_test_payload(job), "trace_id": request.state.trace_id}
        if coinglass is None:
            return error_response(
                409, "COINGLASS_SECRET_REQUIRED",
                "Coinglass Key 尚未注入服务器 Secret", request.state.trace_id,
            )
        try:
            markets = coinglass.get_coins_markets(per_page=10)
            lsr = coinglass.get_lsr_signal("BTC", mode="trader")
            account = coinglass.get_account(force=True)
        except CoinglassRateLimited as exc:
            result = error_response(
                429, exc.code, "上游限流，已停止继续重试", request.state.trace_id,
            )
            result.headers["Retry-After"] = str(exc.retry_after_seconds)
            return result
        except CoinglassError as exc:
            return error_response(502, exc.code, "Coinglass/LSR 检测失败", request.state.trace_id)
        response.headers["Cache-Control"] = "no-store"
        return {
            "connected": True,
            "origin": COINGLASS_ORIGIN,
            "checks": {
                "coins_markets": {
                    "ok": True, "items": len(markets.data), "elapsed_ms": markets.elapsed_ms,
                    "received_at": markets.received_at,
                },
                "lsr_btc": {
                    "ok": True, "elapsed_ms": lsr.elapsed_ms, "received_at": lsr.received_at,
                    "fields": sorted(str(key) for key in lsr.data.keys())[:40],
                },
                "account": {
                    "ok": True, "elapsed_ms": account.elapsed_ms, "received_at": account.received_at,
                    "metadata": _safe_provider_metadata(account.data),
                },
            },
            "automatic_feature_eligible": False,
            "automatic_feature_reason": "等待许可确认与 point-in-time 历史样本",
            "trace_id": request.state.trace_id,
        }

    @app.get("/api/v2/data-sources/coinglass/tests/{job_id}", response_model=None)
    def get_coinglass_test(job_id: UUID, request: Request, response: Response) -> Any:
        denied = _require_owner(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            job = coinglass_configs.get_test(session.tenant_id, session.user_id, job_id)
        except CoinglassConfigNotFound:
            return error_response(
                404, "COINGLASS_TEST_NOT_FOUND", "检测任务不存在",
                request.state.trace_id,
            )
        response.headers["Cache-Control"] = "no-store"
        return {"test": _coinglass_test_payload(job), "trace_id": request.state.trace_id}

    @app.post("/api/v2/data-sources/coinglass/config:disable", response_model=None)
    def disable_coinglass(payload: MfaCodeRequest, request: Request) -> Any:
        denied = _require_credential_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            identity.verify_session_mfa(
                session, payload.code, trace_id=request.state.trace_id,
            )
            coinglass_configs.disable(session.tenant_id, session.user_id)
        except FormalInvalidCredentials:
            return error_response(
                401, "INVALID_MFA_CODE", "验证码无效或已使用", request.state.trace_id,
            )
        except CoinglassConfigNotFound:
            return error_response(
                404, "COINGLASS_CONFIG_NOT_FOUND", "Coinglass 配置不存在",
                request.state.trace_id,
            )
        return {"disabled": True, "trace_id": request.state.trace_id}

    @app.post("/auth/login")
    def login(payload: FormalLoginSubmission, request: Request) -> JSONResponse:
        client_key = request.client.host if request.client else "unknown"
        allowed, retry_after = login_limiter.allow(f"formal-login:{client_key}", 10, 600)
        if not allowed:
            return error_response(
                429, "LOGIN_RATE_LIMITED", "登录尝试过多，请稍后重试", request.state.trace_id,
                **{"Retry-After": str(retry_after)},
            )
        resolved = _resolve_sensitive_submission(
            payload,
            request,
            decryptor=submission_decryptor,
            expected_purpose="auth_login",
            model_type=FormalLoginRequest,
        )
        if isinstance(resolved, JSONResponse):
            return resolved
        try:
            created = identity.authenticate_password(
                resolved.tenant, resolved.username, resolved.password, trace_id=request.state.trace_id,
            )
        except FormalInvalidCredentials:
            return error_response(401, "INVALID_CREDENTIALS", "账号或密码错误", request.state.trace_id)
        response = JSONResponse({
            "user": {
                "id": str(created.session.user_id), "tenant_id": str(created.session.tenant_id),
                "username": created.session.username, "role": created.session.role.value,
            },
            "expires_at": created.session.expires_at.isoformat(),
            "mfa_required": not created.session.fully_authenticated,
        })
        max_age = max(0, int((created.session.expires_at - created.session.recent_auth_at).total_seconds())) if created.session.recent_auth_at else 43_200
        _set_auth_cookies(response, created.token, created.csrf_token, max_age, cookie_secure)
        return response

    @app.get("/auth/me")
    def me(request: Request) -> dict[str, Any]:
        session = request.state.auth_session
        return {
            "user": {
                "id": str(session.user_id), "tenant_id": str(session.tenant_id),
                "username": session.username, "role": session.role.value,
            },
            "expires_at": session.expires_at.isoformat(),
            "mfa_required": not session.fully_authenticated,
            "auth_strength": session.auth_strength,
        }

    @app.post("/auth/reauthenticate", response_model=None)
    def reauthenticate(payload: FormalReauthenticateSubmission, request: Request) -> Any:
        """Refresh this authenticated session; never bypass its existing MFA state.

        Public requests use ``encrypted_submission`` with purpose
        ``user:{user_id}:reauthenticate`` and decrypted content ``{"password": ...}``.
        The existing submission boundary permits plaintext only on a real local hop.
        """
        session = request.state.auth_session
        allowed, retry_after = reauthentication_limiter.allow(
            f"reauthenticate:{session.tenant_id}:{session.user_id}", 5, 300,
        )
        if not allowed:
            return error_response(
                429, "REAUTH_RATE_LIMITED", "密码确认尝试过多，请稍后重试", request.state.trace_id,
                **{"Retry-After": str(retry_after)},
            )
        resolved = _resolve_sensitive_submission(
            payload,
            request,
            decryptor=submission_decryptor,
            expected_purpose=f"user:{session.user_id}:reauthenticate",
            model_type=FormalReauthenticateRequest,
        )
        if isinstance(resolved, JSONResponse):
            return resolved
        try:
            refreshed = identity.reauthenticate_password(
                session, resolved.password, trace_id=request.state.trace_id,
            )
        except FormalInvalidCredentials:
            return error_response(401, "INVALID_CREDENTIALS", "当前密码不正确", request.state.trace_id)
        return {
            "reauthenticated": True,
            "recent_auth_at": refreshed.recent_auth_at,
            "trace_id": request.state.trace_id,
        }

    @app.post("/auth/mfa-methods:enroll", response_model=None)
    def enroll_mfa(payload: MfaEnrollRequest, request: Request) -> Any:
        session = request.state.auth_session
        methods = identity.list_mfa_methods(session)
        if not session.fully_authenticated and any(item["status"] == "ACTIVE" for item in methods):
            return error_response(
                403, "MFA_REQUIRED", "已有验证器时，必须先使用验证器或恢复码完成认证",
                request.state.trace_id,
            )
        enrollment = identity.enroll_totp(session, label=payload.label)
        return {
            "method_id": str(enrollment.method_id),
            "secret": enrollment.secret,
            "provisioning_uri": enrollment.provisioning_uri,
            "warning": "该密钥只显示一次；确认后请安全删除页面副本",
        }

    @app.post("/auth/mfa-methods/{method_id}:confirm")
    def confirm_mfa(method_id: UUID, payload: MfaCodeRequest, request: Request) -> Any:
        try:
            identity.confirm_totp(
                request.state.auth_session, method_id, payload.code, trace_id=request.state.trace_id,
            )
        except FormalInvalidCredentials:
            return error_response(401, "INVALID_MFA_CODE", "验证码无效", request.state.trace_id)
        except FormalAuthError:
            return error_response(404, "MFA_METHOD_NOT_FOUND", "待确认验证器不存在", request.state.trace_id)
        return {"confirmed": True}

    @app.post("/auth/mfa/verify")
    def verify_mfa(payload: MfaCodeRequest, request: Request) -> Any:
        try:
            session = identity.verify_session_mfa(
                request.state.auth_session, payload.code, trace_id=request.state.trace_id,
            )
        except FormalInvalidCredentials:
            return error_response(401, "INVALID_MFA_CODE", "验证码无效", request.state.trace_id)
        return {"verified": True, "auth_strength": session.auth_strength}

    @app.post("/auth/recovery-codes:regenerate")
    def regenerate_recovery(request: Request) -> Any:
        session = request.state.auth_session
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        if (
            session.recent_auth_at is None
            or session.mfa_verified_at is None
            or session.recent_auth_at < cutoff
            or session.mfa_verified_at < cutoff
        ):
            return error_response(403, "RECENT_MFA_REQUIRED", "需要近期密码与多因素认证", request.state.trace_id)
        codes = identity.regenerate_recovery_codes(request.state.auth_session, trace_id=request.state.trace_id)
        return {"codes": codes, "shown_once": True}

    @app.post("/auth/recovery-codes:consume")
    def consume_recovery(payload: RecoveryCodeRequest, request: Request) -> Any:
        try:
            session = identity.consume_recovery_code(
                request.state.auth_session, payload.code, trace_id=request.state.trace_id,
            )
        except (FormalInvalidCredentials, UnicodeError, ValueError):
            return error_response(401, "INVALID_RECOVERY_CODE", "恢复码无效", request.state.trace_id)
        return {"verified": True, "auth_strength": session.auth_strength}

    @app.post("/auth/logout")
    def logout(request: Request) -> JSONResponse:
        session = request.state.auth_session
        identity.revoke_session(session, session.session_id, trace_id=request.state.trace_id)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/", secure=cookie_secure, httponly=True, samesite="strict")
        response.delete_cookie(CSRF_COOKIE, path="/", secure=cookie_secure, httponly=False, samesite="strict")
        return response

    @app.post("/auth/password:change", response_model=None)
    def change_password(payload: PasswordChangeRequest, request: Request) -> dict[str, bool] | JSONResponse:
        try:
            identity.change_password(
                request.state.auth_session,
                payload.current_password,
                payload.new_password,
                trace_id=request.state.trace_id,
            )
        except FormalInvalidCredentials:
            return error_response(401, "INVALID_CREDENTIALS", "当前密码无效", request.state.trace_id)
        except PasswordReuse:
            return error_response(409, "PASSWORD_REUSED", "不能使用近期密码", request.state.trace_id)
        return {"changed": True}

    @app.get("/auth/sessions")
    def sessions(request: Request) -> dict[str, Any]:
        return {"items": identity.list_sessions(request.state.auth_session), "trace_id": request.state.trace_id}

    @app.delete("/auth/sessions/{session_id}", response_model=None)
    def revoke_session(session_id: UUID, request: Request) -> dict[str, bool] | JSONResponse:
        try:
            identity.revoke_session(request.state.auth_session, session_id, trace_id=request.state.trace_id)
        except FormalAuthError:
            return error_response(404, "SESSION_NOT_FOUND", "会话不存在", request.state.trace_id)
        return {"revoked": True}

    @app.get("/auth/mfa-methods")
    def mfa_methods(request: Request) -> dict[str, Any]:
        return {"items": identity.list_mfa_methods(request.state.auth_session), "trace_id": request.state.trace_id}

    @app.delete("/auth/mfa-methods/{method_id}", response_model=None)
    def remove_mfa(method_id: UUID, request: Request) -> dict[str, bool] | JSONResponse:
        session = request.state.auth_session
        methods = identity.list_mfa_methods(session)
        target = next((item for item in methods if item["id"] == str(method_id)), None)
        if target is not None and target["status"] == "ACTIVE" and not session.fully_authenticated:
            return error_response(
                403, "MFA_REQUIRED", "必须先完成多因素认证才能移除有效验证器",
                request.state.trace_id,
            )
        try:
            identity.remove_mfa_method(session, method_id, trace_id=request.state.trace_id)
        except LastMfaMethodRequired:
            return error_response(409, "LAST_MFA_REQUIRED", "不能移除最后一个必需验证器", request.state.trace_id)
        except FormalAuthError:
            return error_response(404, "MFA_METHOD_NOT_FOUND", "验证器不存在", request.state.trace_id)
        return {"revoked": True}

    @app.get("/api/v2/users", response_model=None)
    def users(request: Request) -> dict[str, Any] | JSONResponse:
        try:
            items = identity.list_tenant_users(request.state.auth_session)
        except FormalAuthError:
            return error_response(403, "FORBIDDEN", "权限不足", request.state.trace_id)
        return {"items": items, "trace_id": request.state.trace_id}

    @app.post("/api/v2/users", status_code=201, response_model=None)
    def create_user(payload: UserCreateSubmission, request: Request) -> dict[str, Any] | JSONResponse:
        denied = _require_user_management(request)
        if denied is not None:
            return denied
        resolved = _resolve_sensitive_submission(
            payload, request, decryptor=submission_decryptor,
            expected_purpose=f"tenant:{request.state.auth_session.tenant_id}:user_create",
            model_type=UserCreateRequest,
        )
        if isinstance(resolved, JSONResponse):
            return resolved
        try:
            return identity.create_tenant_user(
                request.state.auth_session,
                resolved.username,
                resolved.initial_password,
                resolved.role,
                trace_id=request.state.trace_id,
            )
        except IdentityConflict:
            return error_response(409, "USERNAME_UNAVAILABLE", "用户名不可用", request.state.trace_id)
        except FormalAuthError:
            return error_response(403, "RECENT_OWNER_AUTH_REQUIRED", "需要 OWNER 近期认证", request.state.trace_id)
        except ValueError:
            return error_response(422, "INVALID_USER", "用户名或密码格式无效", request.state.trace_id)

    @app.get("/api/v2/memberships", response_model=None)
    def memberships(request: Request) -> dict[str, Any] | JSONResponse:
        users_result = users(request)
        if isinstance(users_result, JSONResponse):
            return users_result
        return {
            "items": [
                {**item["membership"], "user_id": item["id"], "username": item["username"]}
                for item in users_result["items"]
            ],
            "trace_id": request.state.trace_id,
        }

    @app.patch("/api/v2/memberships/{membership_id}", response_model=None)
    def update_membership(
        membership_id: UUID,
        payload: MembershipUpdateRequest,
        request: Request,
        if_match: Annotated[str, Header(alias="If-Match")],
    ) -> dict[str, Any] | JSONResponse:
        denied = _require_user_management(request)
        if denied is not None:
            return denied
        try:
            expected_etag = _etag(if_match)
        except ValueError:
            return error_response(422, "INVALID_ETAG", "If-Match 无效", request.state.trace_id)
        try:
            result = identity.update_membership(
                request.state.auth_session,
                membership_id,
                role=payload.role,
                status=payload.status,
                expected_etag=expected_etag,
                trace_id=request.state.trace_id,
            )
        except IdentityPreconditionFailed:
            return error_response(412, "PRECONDITION_FAILED", "成员版本已变化", request.state.trace_id)
        except LastOwnerRequired:
            return error_response(409, "LAST_OWNER_REQUIRED", "不能移除最后一个 OWNER", request.state.trace_id)
        except FormalAuthError:
            return error_response(404, "MEMBERSHIP_NOT_FOUND", "成员不存在或权限不足", request.state.trace_id)
        except ValueError:
            return error_response(422, "INVALID_MEMBERSHIP", "成员更新无效", request.state.trace_id)
        return result

    def list_resources(kind: Any, request: Request) -> dict[str, Any]:
        session = request.state.auth_session
        return {
            "items": [_resource_payload(item) for item in resources.list(kind, session.tenant_id, session.user_id)],
            "next_cursor": None,
            "has_more": False,
            "trace_id": request.state.trace_id,
        }

    def get_resource(kind: Any, resource_id: UUID, request: Request, response: Response) -> Any:
        session = request.state.auth_session
        try:
            item = resources.get(kind, session.tenant_id, session.user_id, resource_id)
        except ResourceNotFound:
            return error_response(404, "RESOURCE_NOT_FOUND", "配置不存在", request.state.trace_id)
        response.headers["ETag"] = f'"{item.etag}"'
        return _resource_payload(item)

    def create_resource(kind: Any, payload: ResourceWriteRequest, request: Request, response: Response) -> Any:
        session = request.state.auth_session
        try:
            item = resources.create(
                kind, session.tenant_id, session.user_id, name=payload.name, spec=payload.spec,
            )
        except ResourceConflict:
            return error_response(409, "RESOURCE_NAME_CONFLICT", "同名配置已存在", request.state.trace_id)
        response.headers["ETag"] = f'"{item.etag}"'
        response.headers["Location"] = f"/api/v2/{_resource_path(kind)}/{item.id}"
        return _resource_payload(item)

    def update_resource(
        kind: Any,
        resource_id: UUID,
        payload: ResourceWriteRequest,
        request: Request,
        response: Response,
        if_match: str,
    ) -> Any:
        session = request.state.auth_session
        try:
            expected = _etag(if_match)
            item = resources.update(
                kind, session.tenant_id, session.user_id, resource_id,
                name=payload.name, spec=payload.spec, expected_etag=expected,
            )
        except ValueError:
            return error_response(422, "INVALID_ETAG", "If-Match 无效", request.state.trace_id)
        except ResourceNotFound:
            return error_response(404, "RESOURCE_NOT_FOUND", "配置不存在", request.state.trace_id)
        except ResourcePreconditionFailed:
            return error_response(412, "PRECONDITION_FAILED", "配置版本已变化", request.state.trace_id)
        except ResourceConflict:
            return error_response(409, "RESOURCE_NAME_CONFLICT", "同名配置已存在", request.state.trace_id)
        response.headers["ETag"] = f'"{item.etag}"'
        return _resource_payload(item)

    def delete_resource(kind: Any, resource_id: UUID, request: Request) -> Any:
        session = request.state.auth_session
        try:
            resources.soft_delete(kind, session.tenant_id, session.user_id, resource_id)
        except ResourceNotFound:
            return error_response(404, "RESOURCE_NOT_FOUND", "配置不存在", request.state.trace_id)
        return {"deleted": True}

    @app.get("/api/v2/chart-layouts")
    def chart_layouts(request: Request) -> dict[str, Any]:
        return list_resources("chart_layout", request)

    @app.post("/api/v2/chart-layouts", status_code=201, response_model=None)
    def create_chart_layout(payload: ResourceWriteRequest, request: Request, response: Response) -> Any:
        return create_resource("chart_layout", payload, request, response)

    @app.get("/api/v2/chart-layouts/{resource_id}", response_model=None)
    def chart_layout(resource_id: UUID, request: Request, response: Response) -> Any:
        return get_resource("chart_layout", resource_id, request, response)

    @app.patch("/api/v2/chart-layouts/{resource_id}", response_model=None)
    def update_chart_layout(
        resource_id: UUID, payload: ResourceWriteRequest, request: Request, response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
    ) -> Any:
        return update_resource("chart_layout", resource_id, payload, request, response, if_match)

    @app.delete("/api/v2/chart-layouts/{resource_id}", response_model=None)
    def delete_chart_layout(resource_id: UUID, request: Request) -> Any:
        return delete_resource("chart_layout", resource_id, request)

    @app.get("/api/v2/indicator-templates")
    def indicator_templates(request: Request) -> dict[str, Any]:
        return list_resources("indicator_template", request)

    @app.post("/api/v2/indicator-templates", status_code=201, response_model=None)
    def create_indicator_template(payload: ResourceWriteRequest, request: Request, response: Response) -> Any:
        return create_resource("indicator_template", payload, request, response)

    @app.get("/api/v2/indicator-templates/{resource_id}", response_model=None)
    def indicator_template(resource_id: UUID, request: Request, response: Response) -> Any:
        return get_resource("indicator_template", resource_id, request, response)

    @app.patch("/api/v2/indicator-templates/{resource_id}", response_model=None)
    def update_indicator_template(
        resource_id: UUID, payload: ResourceWriteRequest, request: Request, response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
    ) -> Any:
        return update_resource("indicator_template", resource_id, payload, request, response, if_match)

    @app.delete("/api/v2/indicator-templates/{resource_id}", response_model=None)
    def delete_indicator_template(resource_id: UUID, request: Request) -> Any:
        return delete_resource("indicator_template", resource_id, request)

    def watchlist_result(item: Watchlist, request: Request, response: Response | None = None) -> dict[str, Any]:
        if response is not None:
            response.headers["ETag"] = f'"{item.etag}"'
        return {**_watchlist_payload(item), "trace_id": request.state.trace_id}

    @app.get("/api/v2/watchlists")
    def list_watchlists(request: Request) -> dict[str, Any]:
        session = request.state.auth_session
        return {
            "items": [
                _watchlist_payload(item)
                for item in watchlists.list(session.tenant_id, session.user_id)
            ],
            "next_cursor": None,
            "has_more": False,
            "trace_id": request.state.trace_id,
        }

    @app.post("/api/v2/watchlists", status_code=201, response_model=None)
    def create_watchlist(
        payload: WatchlistWriteRequest, request: Request, response: Response,
    ) -> Any:
        session = request.state.auth_session
        try:
            item = watchlists.create(session.tenant_id, session.user_id, payload.name)
        except WatchlistConflict:
            return error_response(409, "WATCHLIST_NAME_CONFLICT", "同名观察列表已存在", request.state.trace_id)
        response.headers["Location"] = f"/api/v2/watchlists/{item.id}"
        return watchlist_result(item, request, response)

    @app.get("/api/v2/watchlists/{watchlist_id}", response_model=None)
    def get_watchlist(watchlist_id: UUID, request: Request, response: Response) -> Any:
        session = request.state.auth_session
        try:
            item = watchlists.get(session.tenant_id, session.user_id, watchlist_id)
        except WatchlistNotFound:
            return error_response(404, "WATCHLIST_NOT_FOUND", "观察列表不存在", request.state.trace_id)
        return watchlist_result(item, request, response)

    @app.patch("/api/v2/watchlists/{watchlist_id}", response_model=None)
    def rename_watchlist(
        watchlist_id: UUID,
        payload: WatchlistWriteRequest,
        request: Request,
        response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
    ) -> Any:
        session = request.state.auth_session
        try:
            item = watchlists.rename(
                session.tenant_id, session.user_id, watchlist_id,
                name=payload.name, expected_etag=_etag(if_match),
            )
        except ValueError:
            return error_response(422, "INVALID_ETAG", "If-Match 无效", request.state.trace_id)
        except WatchlistNotFound:
            return error_response(404, "WATCHLIST_NOT_FOUND", "观察列表不存在", request.state.trace_id)
        except WatchlistPreconditionFailed:
            return error_response(412, "PRECONDITION_FAILED", "观察列表版本已变化", request.state.trace_id)
        except WatchlistConflict:
            return error_response(409, "WATCHLIST_NAME_CONFLICT", "同名观察列表已存在", request.state.trace_id)
        return watchlist_result(item, request, response)

    @app.post("/api/v2/watchlists/{watchlist_id}/items", status_code=201, response_model=None)
    def add_watchlist_item(
        watchlist_id: UUID,
        payload: WatchlistItemRequest,
        request: Request,
        response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
    ) -> Any:
        session = request.state.auth_session
        try:
            item = watchlists.add_item(
                session.tenant_id, session.user_id, watchlist_id,
                instrument_id=payload.instrument_id,
                default_timeframe=payload.default_timeframe,
                expected_etag=_etag(if_match),
            )
        except ValueError:
            return error_response(422, "INVALID_ETAG", "If-Match 无效", request.state.trace_id)
        except WatchlistNotFound:
            return error_response(404, "WATCHLIST_NOT_FOUND", "观察列表不存在", request.state.trace_id)
        except WatchlistPreconditionFailed:
            return error_response(412, "PRECONDITION_FAILED", "观察列表版本已变化", request.state.trace_id)
        except WatchlistConflict:
            return error_response(409, "WATCHLIST_ITEM_CONFLICT", "标的不可用或已存在", request.state.trace_id)
        return watchlist_result(item, request, response)

    @app.delete("/api/v2/watchlists/{watchlist_id}/items/{instrument_id}", response_model=None)
    def remove_watchlist_item(
        watchlist_id: UUID,
        instrument_id: UUID,
        request: Request,
        response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
    ) -> Any:
        session = request.state.auth_session
        try:
            item = watchlists.remove_item(
                session.tenant_id, session.user_id, watchlist_id,
                instrument_id=instrument_id, expected_etag=_etag(if_match),
            )
        except ValueError:
            return error_response(422, "INVALID_ETAG", "If-Match 无效", request.state.trace_id)
        except WatchlistNotFound:
            return error_response(404, "WATCHLIST_ITEM_NOT_FOUND", "观察列表或标的不存在", request.state.trace_id)
        except WatchlistPreconditionFailed:
            return error_response(412, "PRECONDITION_FAILED", "观察列表版本已变化", request.state.trace_id)
        return watchlist_result(item, request, response)

    def alert_rule_result(item: AlertRule, request: Request, response: Response | None = None) -> dict[str, Any]:
        if response is not None:
            response.headers["ETag"] = f'"{item.etag}"'
        return {**_alert_rule_payload(item), "trace_id": request.state.trace_id}

    @app.get("/api/v2/alert-rules")
    def list_alert_rules(request: Request) -> dict[str, Any]:
        session = request.state.auth_session
        return {
            "items": [
                _alert_rule_payload(item)
                for item in alert_rules.list(session.tenant_id, session.user_id)
            ],
            "next_cursor": None,
            "has_more": False,
            "trace_id": request.state.trace_id,
        }

    @app.get("/api/v2/strategy-events")
    def list_strategy_events(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        session = request.state.auth_session
        return {
            "items": alert_rules.list_events(
                session.tenant_id, session.user_id, limit=limit,
            ),
            "next_cursor": None,
            "has_more": False,
            "trace_id": request.state.trace_id,
        }

    @app.post("/api/v2/alert-rules", status_code=201, response_model=None)
    def create_alert_rule(
        payload: AlertRuleCreateRequest, request: Request, response: Response,
    ) -> Any:
        denied = _require_alert_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            item = alert_rules.create(
                session.tenant_id, session.user_id, name=payload.name,
                condition=payload.condition.model_dump(mode="json"),
                routing=payload.routing.model_dump(mode="json"),
            )
        except AlertRuleConflict:
            return error_response(409, "ALERT_RULE_CONFLICT", "规则名称冲突或标的不可用", request.state.trace_id)
        response.headers["Location"] = f"/api/v2/alert-rules/{item.id}"
        return alert_rule_result(item, request, response)

    @app.get("/api/v2/alert-rules/{rule_id}", response_model=None)
    def get_alert_rule(rule_id: UUID, request: Request, response: Response) -> Any:
        session = request.state.auth_session
        try:
            item = alert_rules.get(session.tenant_id, session.user_id, rule_id)
        except AlertRuleNotFound:
            return error_response(404, "ALERT_RULE_NOT_FOUND", "告警规则不存在", request.state.trace_id)
        return alert_rule_result(item, request, response)

    @app.patch("/api/v2/alert-rules/{rule_id}", response_model=None)
    def rename_alert_rule(
        rule_id: UUID,
        payload: AlertRuleRenameRequest,
        request: Request,
        response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
    ) -> Any:
        denied = _require_alert_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            item = alert_rules.rename(
                session.tenant_id, session.user_id, rule_id,
                name=payload.name, expected_etag=_etag(if_match),
            )
        except ValueError:
            return error_response(422, "INVALID_ETAG", "If-Match 无效", request.state.trace_id)
        except AlertRuleNotFound:
            return error_response(404, "ALERT_RULE_NOT_FOUND", "告警规则不存在", request.state.trace_id)
        except AlertRulePreconditionFailed:
            return error_response(412, "PRECONDITION_FAILED", "告警规则版本已变化", request.state.trace_id)
        except AlertRuleConflict:
            return error_response(409, "ALERT_RULE_NAME_CONFLICT", "同名告警规则已存在", request.state.trace_id)
        return alert_rule_result(item, request, response)

    @app.put("/api/v2/alert-rules/{rule_id}", response_model=None)
    def update_alert_rule(rule_id: UUID, payload: AlertRuleCreateRequest, request: Request,
                          response: Response, if_match: Annotated[str, Header(alias="If-Match")]) -> Any:
        denied = _require_alert_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            item = alert_rules.update(session.tenant_id, session.user_id, rule_id,
                name=payload.name, condition=payload.condition.model_dump(mode="json"),
                routing=payload.routing.model_dump(mode="json"), expected_etag=_etag(if_match))
        except ValueError:
            return error_response(422, "INVALID_INPUT", "名称或版本参数无效", request.state.trace_id)
        except AlertRuleNotFound:
            return error_response(404, "ALERT_RULE_NOT_FOUND", "策略不存在或已删除", request.state.trace_id)
        except AlertRulePreconditionFailed:
            return error_response(412, "PRECONDITION_FAILED", "策略已变化，请重新打开编辑；本次未保存", request.state.trace_id)
        except AlertRuleConflict:
            return error_response(409, "ALERT_RULE_CONFLICT", "名称重复、标的或通知通道不可用", request.state.trace_id)
        return alert_rule_result(item, request, response)

    @app.delete("/api/v2/alert-rules/{rule_id}", response_model=None)
    def delete_alert_rule(rule_id: UUID, request: Request,
                          if_match: Annotated[str, Header(alias="If-Match")]) -> Any:
        denied = _require_alert_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            alert_rules.delete(session.tenant_id, session.user_id, rule_id, expected_etag=_etag(if_match))
        except ValueError:
            return error_response(422, "INVALID_ETAG", "版本参数无效", request.state.trace_id)
        except AlertRuleNotFound:
            return error_response(404, "ALERT_RULE_NOT_FOUND", "策略不存在或已删除", request.state.trace_id)
        except AlertRulePreconditionFailed:
            return error_response(412, "PRECONDITION_FAILED", "策略已变化，请刷新后确认删除", request.state.trace_id)
        return {"deleted": True, "history_preserved": True, "trace_id": request.state.trace_id}

    @app.post("/api/v2/alert-rules/{rule_id}/versions", status_code=201, response_model=None)
    def version_alert_rule(
        rule_id: UUID,
        payload: AlertRuleVersionRequest,
        request: Request,
        response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
    ) -> Any:
        denied = _require_alert_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            item = alert_rules.add_version(
                session.tenant_id, session.user_id, rule_id,
                condition=payload.condition.model_dump(mode="json"),
                routing=payload.routing.model_dump(mode="json"),
                expected_etag=_etag(if_match),
            )
        except ValueError:
            return error_response(422, "INVALID_ETAG", "If-Match 无效", request.state.trace_id)
        except AlertRuleNotFound:
            return error_response(404, "ALERT_RULE_NOT_FOUND", "告警规则不存在", request.state.trace_id)
        except AlertRulePreconditionFailed:
            return error_response(412, "PRECONDITION_FAILED", "告警规则版本已变化", request.state.trace_id)
        except AlertRuleConflict:
            return error_response(409, "ALERT_RULE_CONFLICT", "告警标的不可用", request.state.trace_id)
        return alert_rule_result(item, request, response)

    @app.post("/api/v2/alert-rules/{rule_id}/pause", response_model=None)
    def pause_alert_rule(
        rule_id: UUID,
        request: Request,
        response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
    ) -> Any:
        denied = _require_alert_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            item = alert_rules.pause(
                session.tenant_id, session.user_id, rule_id, expected_etag=_etag(if_match),
            )
        except ValueError:
            return error_response(422, "INVALID_ETAG", "If-Match 无效", request.state.trace_id)
        except AlertRuleNotFound:
            return error_response(404, "ALERT_RULE_NOT_FOUND", "告警规则不存在", request.state.trace_id)
        except AlertRulePreconditionFailed:
            return error_response(412, "PRECONDITION_FAILED", "告警规则版本已变化", request.state.trace_id)
        except AlertRuleConflict:
            return error_response(409, "ALERT_RULE_NOT_ACTIVE", "告警规则当前不可暂停", request.state.trace_id)
        return alert_rule_result(item, request, response)

    @app.post("/api/v2/alert-rules/{rule_id}/resume", response_model=None)
    def resume_alert_rule(
        rule_id: UUID,
        request: Request,
        response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
    ) -> Any:
        denied = _require_alert_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            item = alert_rules.resume(
                session.tenant_id, session.user_id, rule_id, expected_etag=_etag(if_match),
            )
        except ValueError:
            return error_response(422, "INVALID_ETAG", "If-Match 无效", request.state.trace_id)
        except AlertRuleNotFound:
            return error_response(404, "ALERT_RULE_NOT_FOUND", "告警规则不存在", request.state.trace_id)
        except AlertRulePreconditionFailed:
            return error_response(412, "PRECONDITION_FAILED", "告警规则版本已变化", request.state.trace_id)
        except AlertRuleConflict:
            return error_response(409, "ALERT_RULE_NOT_PAUSED", "告警规则当前不可恢复，请检查状态与标的", request.state.trace_id)
        return alert_rule_result(item, request, response)

    @app.get("/api/v2/telegram/bot-config", response_model=None)
    def get_telegram_bot_config(request: Request) -> Any:
        denied = _require_owner(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        config = telegram_configs.get(session.tenant_id, session.user_id)
        return _telegram_bot_config_payload(
            config,
            secret_write_allowed=browser_submission_ready or _legacy_plaintext_submission_allowed(request),
            public_https=public_origin.startswith("https://"),
            wrapping_configured=telegram_config_encryptor is not None,
            browser_submission_configured=browser_submission_ready,
            legacy_username=telegram_bot_username or None,
            legacy_allowed_ids=telegram_allowed_users,
            trace_id=request.state.trace_id,
        )

    @app.put("/api/v2/telegram/bot-config", response_model=None)
    def configure_telegram_bot(payload: TelegramBotConfigSubmission, request: Request) -> Any:
        # A Bot credential is shared by the workspace.  Members manage their own
        # Telegram binding below; only an OWNER may rotate the shared secret.
        denied = _require_owner(request)
        if denied is not None:
            return denied
        if telegram_config_encryptor is None:
            return error_response(
                503, "TELEGRAM_SECRET_STORAGE_UNAVAILABLE",
                "Telegram 加密存储尚未由管理员初始化", request.state.trace_id,
            )
        session = request.state.auth_session
        resolved = _resolve_sensitive_submission(
            payload,
            request,
            decryptor=submission_decryptor,
            expected_purpose=f"tenant:{session.tenant_id}:telegram_bot_config",
            model_type=TelegramBotConfigRequest,
        )
        if isinstance(resolved, JSONResponse):
            return resolved
        try:
            session = identity.reauthenticate_password(
                session,
                resolved.current_password,
                trace_id=request.state.trace_id,
            )
            identity.verify_session_mfa(session, resolved.mfa_code, trace_id=request.state.trace_id)
        except FormalInvalidCredentials:
            return error_response(
                401,
                "INVALID_CREDENTIALS_OR_MFA",
                "当前密码或 MFA 验证码无效，验证码也可能已使用",
                request.state.trace_id,
            )
        allowed, retry_after = provider_limiter.allow(f"telegram-config:{session.tenant_id}", 3, 60)
        if not allowed:
            return error_response(
                429, "RATE_LIMITED", "Telegram 配置检测过于频繁", request.state.trace_id,
                **{"Retry-After": str(retry_after)},
            )
        try:
            bot_username = telegram_admin.validate_token(resolved.bot_token)
        except ValueError:
            return error_response(422, "TELEGRAM_TOKEN_REJECTED", "Bot Token 无效", request.state.trace_id)
        except RuntimeError as exc:
            return error_response(502, str(exc), "Telegram 连通性检测失败", request.state.trace_id)
        webhook_id = secrets.token_urlsafe(32)
        webhook_secret = secrets.token_urlsafe(32)
        status = "NEEDS_HTTPS"
        safe_error: str | None = "TG_HTTPS_REQUIRED"
        if public_origin.startswith("https://"):
            try:
                telegram_admin.set_webhook(
                    resolved.bot_token,
                    f"{public_origin}/api/v2/telegram/webhook/{webhook_id}",
                    webhook_secret,
                )
                status, safe_error = "ACTIVE", None
            except (RuntimeError, ValueError) as exc:
                status, safe_error = "VALIDATION_FAILED", str(exc)
        config = telegram_configs.save(
            session.tenant_id,
            session.user_id,
            bot_username=bot_username,
            bot_token=resolved.bot_token,
            allowed_user_ids=tuple(resolved.allowed_user_ids),
            webhook_id=webhook_id,
            webhook_secret=webhook_secret,
            encryptor=telegram_config_encryptor,
            status=status,
            last_safe_error_code=safe_error,
        )
        return _telegram_bot_config_payload(
            config,
            secret_write_allowed=True,
            public_https=public_origin.startswith("https://"),
            wrapping_configured=True,
            browser_submission_configured=browser_submission_ready,
            legacy_username=None,
            legacy_allowed_ids=frozenset(),
            trace_id=request.state.trace_id,
        )

    @app.post("/api/v2/telegram/bot-config:disable", response_model=None)
    def disable_telegram_bot(payload: MfaCodeRequest, request: Request) -> Any:
        denied = _require_credential_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            identity.verify_session_mfa(session, payload.code, trace_id=request.state.trace_id)
            telegram_configs.disable(session.tenant_id, session.user_id)
        except FormalInvalidCredentials:
            return error_response(401, "INVALID_MFA_CODE", "验证码无效或已使用", request.state.trace_id)
        except TelegramConfigNotFound:
            return error_response(404, "TELEGRAM_CONFIG_NOT_FOUND", "Telegram 配置不存在", request.state.trace_id)
        return {"disabled": True, "trace_id": request.state.trace_id}

    @app.get("/api/v2/telegram/connections")
    def telegram_connections(request: Request) -> dict[str, Any]:
        session = request.state.auth_session
        web_config = telegram_configs.get(session.tenant_id, session.user_id)
        active_bot_username = (
            web_config.bot_username
            if web_config is not None and web_config.status == "ACTIVE"
            else telegram_bot_username or None
        )
        return {
            "items": [
                _telegram_connection_payload(item)
                for item in notifications.list_connections(session.tenant_id, session.user_id)
            ],
            "configured": bool(
                (web_config is not None and web_config.status == "ACTIVE") or telegram_bot_username
            ),
            "bot_username": active_bot_username,
            "binding_policy": "AUTHENTICATED_MEMBER_SELF_SERVICE",
            "trace_id": request.state.trace_id,
        }

    @app.post("/api/v2/telegram/link-tokens", status_code=201, response_model=None)
    def create_telegram_link(request: Request) -> Any:
        session = request.state.auth_session
        web_config = telegram_configs.get(session.tenant_id, session.user_id)
        bot_username = (
            web_config.bot_username
            if web_config is not None and web_config.status == "ACTIVE"
            else telegram_bot_username
        )
        if not bot_username:
            return error_response(
                503,
                "TELEGRAM_NOT_CONFIGURED",
                "工作区 Telegram Bot 尚未由管理员配置",
                request.state.trace_id,
            )
        link = notifications.create_link_token(session.tenant_id, session.user_id, session.session_id)
        return {
            "deep_link": f"https://t.me/{bot_username}?start={link.token}",
            "expires_at": link.expires_at,
            "expires_in_seconds": int(notifications.LINK_TTL.total_seconds()),
            "trace_id": request.state.trace_id,
        }

    @app.post(
        "/api/v2/telegram/webhook/{opaque_bot_id}",
        include_in_schema=False,
        response_model=None,
    )
    async def telegram_webhook(
        opaque_bot_id: str,
        request: Request,
        telegram_secret: Annotated[str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")] = None,
    ) -> dict[str, Any] | JSONResponse:
        web_config = telegram_configs.resolve_webhook(opaque_bot_id)
        if web_config is not None:
            if telegram_secret is None or not web_config.accepts_secret(telegram_secret):
                return error_response(403, "WEBHOOK_REJECTED", "Webhook 验证失败", request.state.trace_id)
            allowed_users = web_config.allowed_user_ids
            expected_tenant_id: UUID | None = web_config.tenant_id
        else:
            if (
                not telegram_webhook_id
                or not telegram_webhook_secret
                or not hmac.compare_digest(opaque_bot_id, telegram_webhook_id)
            ):
                return error_response(404, "NOT_FOUND", "资源不存在", request.state.trace_id)
            if telegram_secret is None or not hmac.compare_digest(telegram_secret, telegram_webhook_secret):
                return error_response(403, "WEBHOOK_REJECTED", "Webhook 验证失败", request.state.trace_id)
            allowed_users = telegram_allowed_users
            expected_tenant_id = None
        try:
            raw_update=await request.json()
            callback=callback_update(raw_update)
            update = callback[:5] if callback else _telegram_message_update(raw_update)
        except (TypeError, ValueError):
            return {"ok": True}
        update_id, telegram_user_id, telegram_chat_id, telegram_username, text = update
        if text.startswith("/start "):
            # The high-entropy, five-minute, one-use token is already bound to an
            # authenticated Web session and user.  Requiring a tenant-wide static
            # Telegram ID allowlist here made every new member depend on an OWNER.
            # Web MFA remains mandatory before this pending binding becomes active.
            token = text.split(maxsplit=1)[1]
            try:
                result = notifications.accept_private_start(
                    token,
                    update_id=update_id,
                    telegram_user_id=telegram_user_id,
                    telegram_chat_id=telegram_chat_id,
                    telegram_username=telegram_username,
                    expected_tenant_id=expected_tenant_id,
                )
            except ValueError:
                result = None
            if result is not None and result.duplicate:
                return {"ok": True}
            if result is not None and result.accepted:
                message = "绑定请求已收到。请回到 GateScope 网页，用新的 MFA 验证码确认绑定。"
            else:
                message = "绑定链接无效或已过期，请回到 GateScope 网页重新生成。"
            return {
                "method": "sendMessage", "chat_id": telegram_chat_id,
                "text": message, "protect_content": True,
            }
        principal = notifications.resolve_active_telegram_principal(
            telegram_user_id, telegram_chat_id,
        )
        if principal is None or (
            expected_tenant_id is not None and principal.tenant_id != expected_tenant_id
        ):
            if callback:
                return {'method':'answerCallbackQuery','callback_query_id':callback[5],
                        'text':'当前私聊尚未有效绑定，请在网页完成绑定。','cache_time':0}
            # Keep the legacy allowlist only as an optional response allowlist for
            # unsolicited, unbound commands.  It is not an authorization boundary
            # for authenticated self-service links or verified connections.
            if telegram_user_id not in allowed_users:
                return {"ok": True}
            return {
                "method": "sendMessage", "chat_id": telegram_chat_id,
                "text": "这个私聊尚未完成安全绑定。请先在 GateScope 网页生成绑定链接。",
                "protect_content": True,
            }
        # Read-only market jobs are atomically deduplicated with enqueue. The
        # webhook immediately acknowledges callbacks; image work runs off-thread
        # in the existing notification worker, not in an HTTP request.
        try:
            command=None if callback else market_command(text)
        except ValueError as exc:
            return {'method':'sendMessage','chat_id':telegram_chat_id,'text':str(exc),'protect_content':True}
        if callback or command:
            state=telegram_market.enqueue(principal,update_id,telegram_chat_id,command, text if callback else None)
            feedback={'QUEUED':'正在读取后台数据…','DUPLICATE':'已收到，请稍候。','EXPIRED':'菜单已过期，请发送 /watchlist。',
                'UNBOUND':'绑定已失效，请在网页重新绑定。','RATE_LIMITED':'查询较频繁，请稍后再试。'}[state]
            if callback:return {'method':'answerCallbackQuery','callback_query_id':callback[5],'text':feedback,'cache_time':0}
            if state=='DUPLICATE':return {'ok':True}
            return {'method':'sendMessage','chat_id':telegram_chat_id,'text':feedback,'protect_content':True}
        if not notifications.accept_command_update(
            principal, update_id=update_id, command_text=text,
        ):
            return {"ok": True}
        try:
            message = _telegram_command_response(
                paper,
                testnet_execution,
                principal.tenant_id,
                principal.user_id,
                text,
                public_origin,
            )
        except (PaperConflict, ValueError):
            message = "暂时无法刷新这笔 PAPER 交易，请稍后重试；历史记录没有被改写。"
        return {
            "method": "sendMessage", "chat_id": telegram_chat_id,
            "text": message, "protect_content": True,
        }

    @app.post("/api/v2/telegram/connections/{connection_id}:confirm", response_model=None)
    def confirm_telegram_connection(
        connection_id: UUID,
        payload: MfaCodeRequest,
        request: Request,
    ) -> Any:
        session = request.state.auth_session
        try:
            identity.verify_session_mfa(session, payload.code, trace_id=request.state.trace_id)
        except FormalInvalidCredentials:
            return error_response(401, "INVALID_MFA_CODE", "验证码无效或已使用", request.state.trace_id)
        try:
            connection = notifications.confirm_connection(
                session.tenant_id, session.user_id, connection_id,
            )
        except NotificationNotFound:
            return error_response(404, "TELEGRAM_CONNECTION_NOT_FOUND", "待确认绑定不存在", request.state.trace_id)
        return {**_telegram_connection_payload(connection), "trace_id": request.state.trace_id}

    @app.get("/api/v2/notification-channels")
    def notification_channels(request: Request) -> dict[str, Any]:
        session = request.state.auth_session
        return {
            "items": [
                _notification_channel_payload(item)
                for item in notifications.list_channels(session.tenant_id, session.user_id)
            ],
            "next_cursor": None,
            "has_more": False,
            "trace_id": request.state.trace_id,
        }

    @app.post("/api/v2/notification-channels", status_code=201, response_model=None)
    def create_notification_channel(
        payload: NotificationChannelRequest, request: Request, response: Response,
    ) -> Any:
        session = request.state.auth_session
        try:
            channel = notifications.create_channel(
                session.tenant_id,
                session.user_id,
                connection_id=payload.connection_id,
                alias=payload.alias,
            )
        except (NotificationConflict, ValueError):
            return error_response(
                409, "NOTIFICATION_CHANNEL_CONFLICT", "通道名称冲突或 Telegram 绑定未激活",
                request.state.trace_id,
            )
        response.headers["ETag"] = f'"{channel.etag}"'
        response.headers["Location"] = f"/api/v2/notification-channels/{channel.id}"
        return {**_notification_channel_payload(channel), "trace_id": request.state.trace_id}

    @app.get("/api/v2/notification-channels/{channel_id}", response_model=None)
    def get_notification_channel(
        channel_id: UUID, request: Request, response: Response,
    ) -> Any:
        session = request.state.auth_session
        try:
            channel = notifications.get_channel(session.tenant_id, session.user_id, channel_id)
        except NotificationNotFound:
            return error_response(404, "NOTIFICATION_CHANNEL_NOT_FOUND", "通知通道不存在", request.state.trace_id)
        response.headers["ETag"] = f'"{channel.etag}"'
        return {**_notification_channel_payload(channel), "trace_id": request.state.trace_id}

    @app.post("/api/v2/notification-channels/{channel_id}/test", status_code=202, response_model=None)
    def test_notification_channel(channel_id: UUID, request: Request, response: Response) -> Any:
        session = request.state.auth_session
        try:
            operation_id = notifications.enqueue_test(session.tenant_id, session.user_id, channel_id)
        except NotificationNotFound:
            return error_response(404, "NOTIFICATION_CHANNEL_NOT_FOUND", "通知通道不存在", request.state.trace_id)
        response.headers["Location"] = f"/api/v2/operations/{operation_id}"
        return {
            "operation_id": str(operation_id),
            "status": "QUEUED",
            "trace_id": request.state.trace_id,
        }

    @app.delete("/api/v2/notification-channels/{channel_id}", response_model=None)
    def revoke_notification_channel(channel_id: UUID, request: Request) -> Any:
        session = request.state.auth_session
        try:
            notifications.revoke_channel(session.tenant_id, session.user_id, channel_id)
        except NotificationNotFound:
            return error_response(404, "NOTIFICATION_CHANNEL_NOT_FOUND", "通知通道不存在", request.state.trace_id)
        return {"revoked": True, "trace_id": request.state.trace_id}

    @app.get("/api/v2/notification-routes")
    def notification_routes(request: Request) -> dict[str, Any]:
        session = request.state.auth_session
        return {
            "items": [
                _notification_route_payload(item)
                for item in notifications.list_routes(session.tenant_id, session.user_id)
            ],
            "next_cursor": None,
            "has_more": False,
            "trace_id": request.state.trace_id,
        }

    @app.get("/api/v2/notifications")
    def notification_events(request: Request, limit: int = Query(default=50, ge=1, le=200),
                            offset: int = Query(default=0, ge=0, le=1_000_000),
                            status: str = Query(default="", pattern=r"^(|PENDING|SENDING|RETRY|SENT|FAILED|DEAD_LETTER)$")) -> dict[str, Any]:
        session = request.state.auth_session
        items = notifications.list_notification_events(
            session.tenant_id, session.user_id, limit=limit + 1, offset=offset, status=status,
        )
        total, attention = notifications.notification_counts(session.tenant_id, session.user_id, status)
        return {
            "items": [_notification_event_payload(item) for item in items[:limit]],
            "next_cursor": None,
            "has_more": len(items) > limit,
            "next_offset": offset + limit if len(items) > limit else None,
            "total": total,
            "attention_count": attention,
            "trace_id": request.state.trace_id,
        }

    @app.post("/api/v2/notification-routes", status_code=201, response_model=None)
    def create_notification_route(
        payload: NotificationRouteRequest, request: Request, response: Response,
    ) -> Any:
        session = request.state.auth_session
        try:
            route = notifications.create_route(
                session.tenant_id,
                session.user_id,
                name=payload.name,
                spec=payload.model_dump(mode="json", exclude={"name"}),
            )
        except (NotificationConflict, ValueError):
            return error_response(
                409, "NOTIFICATION_ROUTE_CONFLICT", "路由名称冲突或通知通道不可用",
                request.state.trace_id,
            )
        response.headers["ETag"] = f'"{route.etag}"'
        response.headers["Location"] = f"/api/v2/notification-routes/{route.id}"
        return {**_notification_route_payload(route), "trace_id": request.state.trace_id}

    @app.get("/api/v2/notification-routes/{route_id}", response_model=None)
    def get_notification_route(route_id: UUID, request: Request, response: Response) -> Any:
        session = request.state.auth_session
        try:
            route = notifications.get_route(session.tenant_id, session.user_id, route_id)
        except NotificationNotFound:
            return error_response(404, "NOTIFICATION_ROUTE_NOT_FOUND", "通知路由不存在", request.state.trace_id)
        response.headers["ETag"] = f'"{route.etag}"'
        return {**_notification_route_payload(route), "trace_id": request.state.trace_id}

    @app.patch("/api/v2/notification-routes/{route_id}", response_model=None)
    def update_notification_route(
        route_id: UUID,
        payload: NotificationRouteRequest,
        request: Request,
        response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
    ) -> Any:
        session = request.state.auth_session
        try:
            route = notifications.update_route(
                session.tenant_id,
                session.user_id,
                route_id,
                name=payload.name,
                spec=payload.model_dump(mode="json", exclude={"name"}),
                expected_etag=_etag(if_match),
            )
        except ValueError:
            return error_response(422, "INVALID_ETAG", "If-Match 无效", request.state.trace_id)
        except NotificationNotFound:
            return error_response(404, "NOTIFICATION_ROUTE_NOT_FOUND", "通知路由不存在", request.state.trace_id)
        except NotificationPreconditionFailed:
            return error_response(412, "PRECONDITION_FAILED", "通知路由版本已变化", request.state.trace_id)
        except NotificationConflict:
            return error_response(
                409, "NOTIFICATION_ROUTE_CONFLICT", "路由名称冲突或通知通道不可用",
                request.state.trace_id,
            )
        response.headers["ETag"] = f'"{route.etag}"'
        return {**_notification_route_payload(route), "trace_id": request.state.trace_id}

    @app.delete("/api/v2/notification-routes/{route_id}", response_model=None)
    def delete_notification_route(route_id: UUID, request: Request) -> Any:
        session = request.state.auth_session
        try:
            notifications.delete_route(session.tenant_id, session.user_id, route_id)
        except NotificationNotFound:
            return error_response(404, "NOTIFICATION_ROUTE_NOT_FOUND", "通知路由不存在", request.state.trace_id)
        return {"deleted": True, "trace_id": request.state.trace_id}

    @app.get("/api/v2/exchange-accounts", response_model=None)
    def list_exchange_accounts(request: Request) -> Any:
        denied = _require_owner(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        return {
            "items": [
                _exchange_account_payload(account)
                for account in exchange_accounts.list_accounts(session.tenant_id, session.user_id)
            ],
            "credential_admission_configured": credential_storage_ready and browser_submission_ready,
            "credential_storage_configured": credential_storage_ready,
            "browser_submission_configured": browser_submission_ready,
            "live_trading_enabled": False,
            "next_cursor": None,
            "has_more": False,
            "trace_id": request.state.trace_id,
        }

    @app.post("/api/v2/exchange-accounts", status_code=201, response_model=None)
    def create_exchange_account(
        payload: ExchangeAccountCreateSubmission,
        request: Request,
        response: Response,
    ) -> Any:
        denied = _require_credential_write(request)
        if denied is not None:
            return denied
        if not credential_storage_ready or credential_encryptor is None:
            return error_response(
                503,
                "CREDENTIAL_ENCRYPTION_UNAVAILABLE",
                "服务器未配置凭据存储公钥，未接收或保存密钥",
                request.state.trace_id,
            )
        session = request.state.auth_session
        resolved = _resolve_sensitive_submission(
            payload,
            request,
            decryptor=submission_decryptor,
            expected_purpose=f"tenant:{session.tenant_id}:exchange_account_create",
            model_type=ExchangeAccountCreateRequest,
        )
        if isinstance(resolved, JSONResponse):
            return resolved
        allowed, retry_after = credential_limiter.allow(f"credential:{session.session_id}", 10, 600)
        if not allowed:
            return error_response(
                429, "CREDENTIAL_RATE_LIMITED", "凭据操作过于频繁", request.state.trace_id,
                **{"Retry-After": str(retry_after)},
            )
        try:
            account, command_id = exchange_accounts.create_account(
                session.tenant_id,
                session.user_id,
                alias=resolved.alias,
                material=CredentialMaterial(api_key=resolved.api_key, api_secret=resolved.api_secret),
                encryptor=credential_encryptor,
                no_withdrawal_attested=resolved.acknowledge_no_withdrawal,
            )
        except ExchangeAccountConflict:
            return error_response(
                409, "EXCHANGE_ACCOUNT_CONFLICT", "账户别名或凭据已存在", request.state.trace_id,
            )
        except ValueError:
            return error_response(
                422, "CREDENTIAL_ADMISSION_REJECTED", "交易所凭据不符合接入策略", request.state.trace_id,
            )
        response.headers["Location"] = f"/api/v2/exchange-accounts/{account.id}"
        response.headers["ETag"] = f'"{account.etag}"'
        return {
            **_exchange_account_payload(account),
            "validation_command_id": str(command_id),
            "secret_stored": True,
            "secret_returned": False,
            "trace_id": request.state.trace_id,
        }

    @app.get("/api/v2/exchange-accounts/{account_id}", response_model=None)
    def get_exchange_account(account_id: UUID, request: Request, response: Response) -> Any:
        denied = _require_owner(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            account = exchange_accounts.get_account(session.tenant_id, session.user_id, account_id)
        except ExchangeAccountNotFound:
            return error_response(
                404, "EXCHANGE_ACCOUNT_NOT_FOUND", "交易所账户不存在", request.state.trace_id,
            )
        response.headers["ETag"] = f'"{account.etag}"'
        return {**_exchange_account_payload(account), "trace_id": request.state.trace_id}

    @app.post("/api/v2/exchange-accounts/{account_id}/credentials:rotate", status_code=202, response_model=None)
    def rotate_exchange_credential(
        account_id: UUID,
        payload: ExchangeCredentialRotateSubmission,
        request: Request,
        response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
    ) -> Any:
        denied = _require_credential_write(request)
        if denied is not None:
            return denied
        if not credential_storage_ready or credential_encryptor is None:
            return error_response(
                503,
                "CREDENTIAL_ENCRYPTION_UNAVAILABLE",
                "服务器未配置凭据存储公钥，未接收或保存密钥",
                request.state.trace_id,
            )
        session = request.state.auth_session
        resolved = _resolve_sensitive_submission(
            payload,
            request,
            decryptor=submission_decryptor,
            expected_purpose=(
                f"tenant:{session.tenant_id}:exchange_credential_rotate:{account_id}"
            ),
            model_type=ExchangeCredentialRotateRequest,
        )
        if isinstance(resolved, JSONResponse):
            return resolved
        allowed, retry_after = credential_limiter.allow(f"credential:{session.session_id}", 10, 600)
        if not allowed:
            return error_response(
                429, "CREDENTIAL_RATE_LIMITED", "凭据操作过于频繁", request.state.trace_id,
                **{"Retry-After": str(retry_after)},
            )
        try:
            expected_etag = _etag(if_match)
            account, command_id = exchange_accounts.rotate_credential(
                session.tenant_id,
                session.user_id,
                account_id,
                expected_etag=expected_etag,
                material=CredentialMaterial(api_key=resolved.api_key, api_secret=resolved.api_secret),
                encryptor=credential_encryptor,
                no_withdrawal_attested=resolved.acknowledge_no_withdrawal,
            )
        except ValueError:
            return error_response(422, "INVALID_ETAG", "请求或 If-Match 无效", request.state.trace_id)
        except ExchangeAccountNotFound:
            return error_response(
                404, "EXCHANGE_ACCOUNT_NOT_FOUND", "交易所账户不存在", request.state.trace_id,
            )
        except ExchangeAccountConflict:
            return error_response(
                409, "EXCHANGE_ACCOUNT_CONFLICT", "账户版本已变化、已停用或正在验证", request.state.trace_id,
            )
        response.headers["ETag"] = f'"{account.etag}"'
        return {
            **_exchange_account_payload(account),
            "validation_command_id": str(command_id),
            "secret_returned": False,
            "trace_id": request.state.trace_id,
        }

    @app.post("/api/v2/exchange-accounts/{account_id}/validate", status_code=202, response_model=None)
    def validate_exchange_account(
        account_id: UUID,
        request: Request,
        response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
    ) -> Any:
        denied = _require_credential_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            account, command_id = exchange_accounts.request_validation(
                session.tenant_id,
                session.user_id,
                account_id,
                expected_etag=_etag(if_match),
            )
        except ValueError:
            return error_response(422, "INVALID_ETAG", "If-Match 无效", request.state.trace_id)
        except ExchangeAccountNotFound:
            return error_response(
                404, "EXCHANGE_ACCOUNT_NOT_FOUND", "交易所账户不存在", request.state.trace_id,
            )
        except ExchangeAccountConflict:
            return error_response(
                409, "EXCHANGE_ACCOUNT_CONFLICT", "账户版本已变化、已停用或正在验证", request.state.trace_id,
            )
        response.headers["ETag"] = f'"{account.etag}"'
        return {
            **_exchange_account_payload(account),
            "validation_command_id": str(command_id),
            "trace_id": request.state.trace_id,
        }

    @app.delete("/api/v2/exchange-accounts/{account_id}", response_model=None)
    def disable_exchange_account(
        account_id: UUID,
        request: Request,
        response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
    ) -> Any:
        denied = _require_credential_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            account = exchange_accounts.disable_account(
                session.tenant_id,
                session.user_id,
                account_id,
                expected_etag=_etag(if_match),
            )
        except ValueError:
            return error_response(422, "INVALID_ETAG", "If-Match 无效", request.state.trace_id)
        except ExchangeAccountNotFound:
            return error_response(
                404, "EXCHANGE_ACCOUNT_NOT_FOUND", "交易所账户不存在", request.state.trace_id,
            )
        except ExchangeAccountConflict:
            return error_response(
                409, "EXCHANGE_ACCOUNT_CONFLICT", "账户版本已变化或已经停用", request.state.trace_id,
            )
        response.headers["ETag"] = f'"{account.etag}"'
        return {
            **_exchange_account_payload(account),
            "remote_key_revoked": False,
            "warning": "本地已停用；请同时在 Gate 控制台撤销 API Key。",
            "trace_id": request.state.trace_id,
        }

    @app.post("/api/v2/exchange-accounts/{account_id}/testnet:arm", response_model=None)
    def arm_exchange_testnet(
        account_id: UUID,
        payload: ExchangeTestnetArmRequest,
        request: Request,
        response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
    ) -> Any:
        denied = _require_credential_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            identity.verify_session_mfa(session, payload.mfa_code, trace_id=request.state.trace_id)
        except FormalInvalidCredentials:
            return error_response(401, "INVALID_MFA_CODE", "验证码无效或已使用", request.state.trace_id)
        try:
            account = exchange_accounts.arm_testnet_execution(
                session.tenant_id,
                session.user_id,
                account_id,
                expected_etag=_etag(if_match),
                ip_whitelist_attested=payload.acknowledge_ip_whitelist,
                no_withdrawal_attested=payload.acknowledge_no_withdrawal,
                exclusive_account_attested=payload.acknowledge_exclusive_account,
            )
        except ValueError:
            return error_response(422, "TESTNET_ARMING_REJECTED", "TESTNET 安全确认不完整", request.state.trace_id)
        except ExchangeAccountNotFound:
            return error_response(404, "EXCHANGE_ACCOUNT_NOT_FOUND", "交易所账户不存在", request.state.trace_id)
        except ExchangeAccountConflict:
            return error_response(
                409,
                "TESTNET_ARMING_CONFLICT",
                "账户未完成只读验证、未配置固定 IP，或版本已变化",
                request.state.trace_id,
            )
        response.headers["ETag"] = f'"{account.etag}"'
        return {**_exchange_account_payload(account), "trace_id": request.state.trace_id}

    @app.post("/api/v2/exchange-accounts/{account_id}/testnet:disarm", response_model=None)
    def disarm_exchange_testnet(
        account_id: UUID,
        request: Request,
        response: Response,
        if_match: Annotated[str, Header(alias="If-Match")],
    ) -> Any:
        denied = _require_credential_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            account = exchange_accounts.disarm_testnet_execution(
                session.tenant_id,
                session.user_id,
                account_id,
                expected_etag=_etag(if_match),
            )
        except ValueError:
            return error_response(422, "INVALID_ETAG", "If-Match 无效", request.state.trace_id)
        except ExchangeAccountNotFound:
            return error_response(404, "EXCHANGE_ACCOUNT_NOT_FOUND", "交易所账户不存在", request.state.trace_id)
        except ExchangeAccountConflict:
            return error_response(
                409, "TESTNET_DISARM_CONFLICT", "账户正在执行或版本已变化", request.state.trace_id,
            )
        response.headers["ETag"] = f'"{account.etag}"'
        return {**_exchange_account_payload(account), "trace_id": request.state.trace_id}

    @app.get("/api/v2/testnet-drills", response_model=None)
    def list_testnet_drills(request: Request, limit: int = Query(default=100, ge=1, le=500)) -> Any:
        denied = _require_owner(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        return {
            "items": [
                _testnet_drill_payload(item)
                for item in testnet_execution.list_drills(
                    session.tenant_id, session.user_id, limit=limit,
                )
            ],
            "live_trading_enabled": False,
            "trace_id": request.state.trace_id,
        }

    @app.get("/api/v2/execution-certifications", response_model=None)
    def list_execution_certifications(request: Request) -> Any:
        denied = _require_owner(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        return {
            "items": [
                _execution_certification_payload(item)
                for item in testnet_execution.list_certifications(
                    session.tenant_id, session.user_id,
                )
            ],
            "required_evidence_tier": "LIVE_ELIGIBLE",
            "environment": "TESTNET",
            "trace_id": request.state.trace_id,
        }

    @app.post("/api/v2/testnet-drills", status_code=202, response_model=None)
    def create_testnet_drill(
        payload: TestnetDrillCreateRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    ) -> Any:
        denied = _require_credential_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            identity.verify_session_mfa(session, payload.mfa_code, trace_id=request.state.trace_id)
        except FormalInvalidCredentials:
            return error_response(401, "INVALID_MFA_CODE", "验证码无效或已使用", request.state.trace_id)
        try:
            drill = testnet_execution.create_drill(
                session.tenant_id,
                session.user_id,
                paper_public_trade_id=payload.paper_public_trade_id,
                exchange_account_id=payload.exchange_account_id,
                idempotency_key=idempotency_key,
            )
        except TestnetDrillNotFound:
            return error_response(404, "PAPER_TRADE_NOT_FOUND", "PAPER 交易不存在", request.state.trace_id)
        except TestnetDrillConflict:
            return error_response(
                409,
                "TESTNET_DRILL_CONFLICT",
                "必须选择有效 LIVE_ELIGIBLE 认证下的 OPEN 冻结交易和空闲 TESTNET 账户",
                request.state.trace_id,
            )
        except ValueError:
            return error_response(422, "TESTNET_DRILL_INVALID", "TESTNET 演练请求无效", request.state.trace_id)
        response.headers["Location"] = f"/api/v2/testnet-drills/{drill.public_drill_id}"
        response.headers["ETag"] = f'"{drill.version}"'
        return {**_testnet_drill_payload(drill), "trace_id": request.state.trace_id}

    @app.get("/api/v2/testnet-drills/{public_drill_id}", response_model=None)
    def get_testnet_drill(public_drill_id: str, request: Request, response: Response) -> Any:
        denied = _require_owner(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            drill = testnet_execution.get_drill(
                session.tenant_id, session.user_id, public_drill_id,
            )
        except TestnetDrillNotFound:
            return error_response(404, "TESTNET_DRILL_NOT_FOUND", "TESTNET 演练不存在", request.state.trace_id)
        response.headers["ETag"] = f'"{drill.version}"'
        return {**_testnet_drill_payload(drill), "trace_id": request.state.trace_id}

    @app.get("/api/v2/testnet-drills/{public_drill_id}/timeline", response_model=None)
    def get_testnet_drill_timeline(public_drill_id: str, request: Request) -> Any:
        denied = _require_owner(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            items = testnet_execution.timeline(
                session.tenant_id, session.user_id, public_drill_id,
            )
        except TestnetDrillNotFound:
            return error_response(404, "TESTNET_DRILL_NOT_FOUND", "TESTNET 演练不存在", request.state.trace_id)
        return {"items": items, "trace_id": request.state.trace_id}

    @app.get("/api/v2/mcp/clients", response_model=None)
    def list_mcp_clients(request: Request) -> Any:
        denied = _require_owner(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        return {
            "items": [
                {
                    **_mcp_client_payload(item),
                    "tokens": [
                        _mcp_token_payload(token)
                        for token in mcp_access.list_tokens(session.tenant_id, session.user_id, item.id)
                    ],
                }
                for item in mcp_access.list_clients(session.tenant_id, session.user_id)
            ],
            "supported_scopes": sorted(MCP_SCOPES),
            "trace_id": request.state.trace_id,
        }

    @app.post("/api/v2/mcp/clients", status_code=201, response_model=None)
    def create_mcp_client(
        payload: McpClientCreateRequest, request: Request, response: Response,
    ) -> Any:
        denied = _require_credential_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            client = mcp_access.create_client(session.tenant_id, session.user_id, name=payload.name)
        except (McpConflict, ValueError):
            return error_response(409, "MCP_CLIENT_CONFLICT", "MCP 客户端名称冲突", request.state.trace_id)
        response.headers["ETag"] = f'"{client.etag}"'
        response.headers["Location"] = f"/api/v2/mcp/clients/{client.id}"
        return {**_mcp_client_payload(client), "trace_id": request.state.trace_id}

    @app.post("/api/v2/mcp/clients/{client_id}:revoke", response_model=None)
    def revoke_mcp_client(
        client_id: UUID, payload: MfaCodeRequest, request: Request, response: Response,
    ) -> Any:
        denied = _require_credential_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            identity.verify_session_mfa(session, payload.code, trace_id=request.state.trace_id)
        except FormalInvalidCredentials:
            return error_response(401, "INVALID_MFA_CODE", "验证码无效或已使用", request.state.trace_id)
        try:
            client = mcp_access.revoke_client(session.tenant_id, session.user_id, client_id)
        except McpNotFound:
            return error_response(404, "MCP_CLIENT_NOT_FOUND", "MCP 客户端不存在", request.state.trace_id)
        except McpConflict:
            return error_response(409, "MCP_CLIENT_REVOKED", "MCP 客户端已吊销", request.state.trace_id)
        response.headers["ETag"] = f'"{client.etag}"'
        return {**_mcp_client_payload(client), "trace_id": request.state.trace_id}

    @app.post("/api/v2/mcp/clients/{client_id}/tokens", status_code=201, response_model=None)
    def create_mcp_token(
        client_id: UUID, payload: McpTokenCreateRequest, request: Request, response: Response,
    ) -> Any:
        denied = _require_credential_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            identity.verify_session_mfa(session, payload.mfa_code, trace_id=request.state.trace_id)
        except FormalInvalidCredentials:
            return error_response(401, "INVALID_MFA_CODE", "验证码无效或已使用", request.state.trace_id)
        try:
            created = mcp_access.create_token(
                session.tenant_id,
                session.user_id,
                client_id,
                label=payload.label,
                scopes=payload.scopes,
                ttl=timedelta(hours=payload.ttl_hours),
            )
        except McpNotFound:
            return error_response(404, "MCP_CLIENT_NOT_FOUND", "有效 MCP 客户端不存在", request.state.trace_id)
        except ValueError:
            return error_response(422, "INVALID_MCP_TOKEN_POLICY", "MCP 令牌策略无效", request.state.trace_id)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return {
            **_mcp_token_payload(created.token),
            "token": created.secret,
            "shown_once": True,
            "warning": "令牌只显示一次，请存入密码管理器；遗失后只能吊销并重建。",
            "trace_id": request.state.trace_id,
        }

    @app.post(
        "/api/v2/mcp/clients/{client_id}/tokens/{token_id}:revoke",
        response_model=None,
    )
    def revoke_mcp_token(
        client_id: UUID,
        token_id: UUID,
        payload: MfaCodeRequest,
        request: Request,
    ) -> Any:
        denied = _require_credential_write(request)
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            identity.verify_session_mfa(session, payload.code, trace_id=request.state.trace_id)
        except FormalInvalidCredentials:
            return error_response(401, "INVALID_MFA_CODE", "验证码无效或已使用", request.state.trace_id)
        try:
            token = mcp_access.revoke_token(session.tenant_id, session.user_id, client_id, token_id)
        except McpNotFound:
            return error_response(404, "MCP_TOKEN_NOT_FOUND", "MCP 令牌不存在", request.state.trace_id)
        except McpConflict:
            return error_response(409, "MCP_TOKEN_REVOKED", "MCP 令牌已吊销", request.state.trace_id)
        return {**_mcp_token_payload(token), "trace_id": request.state.trace_id}

    @app.get("/api/v2/paper-accounts", response_model=None)
    def list_paper_accounts(request: Request) -> Any:
        session = request.state.auth_session
        return {
            "items": [
                _paper_account_payload(item)
                for item in paper.list_accounts(session.tenant_id, session.user_id)
            ],
            "next_cursor": None,
            "has_more": False,
            "trace_id": request.state.trace_id,
        }

    @app.post("/api/v2/paper-accounts", status_code=201, response_model=None)
    def create_paper_account(
        payload: PaperAccountCreateRequest, request: Request, response: Response,
    ) -> Any:
        denied = _require_action(request, Action.PROPOSAL_CREATE, "当前角色不能创建模拟账户")
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            account = paper.create_account(
                session.tenant_id,
                session.user_id,
                name=payload.name,
                base_currency=payload.base_currency,
                initial_equity=payload.initial_equity,
                risk_fraction=payload.risk_fraction,
                max_notional_fraction=payload.max_notional_fraction,
                fee_bps_per_side=payload.fee_bps_per_side,
                market_slippage_bps=payload.market_slippage_bps,
                max_entry_drift_bps=payload.max_entry_drift_bps,
            )
        except PaperConflict:
            return error_response(409, "PAPER_ACCOUNT_CONFLICT", "同名模拟账户已存在", request.state.trace_id)
        except ValueError:
            return error_response(422, "INVALID_PAPER_ACCOUNT", "模拟账户参数无效", request.state.trace_id)
        response.headers["ETag"] = f'"{account.etag}"'
        response.headers["Location"] = f"/api/v2/paper-accounts/{account.id}"
        return {**_paper_account_payload(account), "trace_id": request.state.trace_id}

    @app.get("/api/v2/signals", response_model=None)
    def list_signals(request: Request, limit: int = Query(default=100, ge=1, le=500)) -> Any:
        session = request.state.auth_session
        return {
            "items": [
                _signal_payload(item)
                for item in paper.list_signals(session.tenant_id, session.user_id, limit=limit)
            ],
            "next_cursor": None,
            "has_more": False,
            "trace_id": request.state.trace_id,
        }

    @app.get("/api/v2/signals/{signal_id}", response_model=None)
    def get_signal(signal_id: UUID, request: Request) -> Any:
        session = request.state.auth_session
        try:
            item = paper.get_signal(session.tenant_id, session.user_id, signal_id)
        except PaperNotFound:
            return error_response(404, "SIGNAL_NOT_FOUND", "信号证据不存在", request.state.trace_id)
        return {**_signal_payload(item), "trace_id": request.state.trace_id}

    @app.get("/api/v2/signals/{signal_id}/gates", response_model=None)
    def get_signal_gates(signal_id: UUID, request: Request) -> Any:
        session = request.state.auth_session
        try:
            items = paper.signal_gates(session.tenant_id, session.user_id, signal_id)
        except PaperNotFound:
            return error_response(404, "SIGNAL_NOT_FOUND", "信号证据不存在", request.state.trace_id)
        return {"items": items, "trace_id": request.state.trace_id}

    @app.get("/api/v2/signals/{signal_id}/chart", response_model=None)
    def get_signal_chart(signal_id: UUID, request: Request) -> Any:
        session = request.state.auth_session
        try:
            chart = paper.signal_chart(session.tenant_id, session.user_id, signal_id)
        except PaperNotFound:
            return error_response(404, "SIGNAL_NOT_FOUND", "信号证据不存在", request.state.trace_id)
        return {**_paper_chart_payload(chart), "trace_id": request.state.trace_id}

    @app.post("/api/v2/signals/{signal_id}/execution-decisions", status_code=201, response_model=None)
    def create_execution_decision(
        signal_id: UUID, payload: ExecutionDecisionCreateRequest, request: Request,
    ) -> Any:
        denied = _require_action(request, Action.PROPOSAL_CREATE, "当前角色不能创建模拟执行决定")
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            item = paper.create_execution_decision(
                session.tenant_id,
                session.user_id,
                signal_id,
                paper_account_id=payload.paper_account_id,
                client_request_id=payload.client_request_id,
            )
        except PaperNotFound:
            return error_response(404, "PAPER_INPUT_NOT_FOUND", "信号或模拟账户不存在", request.state.trace_id)
        except PaperConflict:
            return error_response(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于不同输入", request.state.trace_id)
        except ValueError:
            return error_response(422, "INVALID_EXECUTION_DECISION", "模拟执行决定参数无效", request.state.trace_id)
        return {**_execution_decision_payload(item), "trace_id": request.state.trace_id}

    @app.get("/api/v2/execution-decisions/{decision_id}", response_model=None)
    def get_execution_decision(decision_id: UUID, request: Request) -> Any:
        session = request.state.auth_session
        try:
            item = paper.get_execution_decision(session.tenant_id, session.user_id, decision_id)
        except PaperNotFound:
            return error_response(404, "EXECUTION_DECISION_NOT_FOUND", "执行决定不存在", request.state.trace_id)
        return {**_execution_decision_payload(item), "trace_id": request.state.trace_id}

    @app.get("/api/v2/proposals", response_model=None)
    def list_proposals(request: Request, limit: int = Query(default=100, ge=1, le=500)) -> Any:
        session = request.state.auth_session
        return {
            "items": [
                _proposal_payload(item)
                for item in paper.list_proposals(session.tenant_id, session.user_id, limit=limit)
            ],
            "next_cursor": None,
            "has_more": False,
            "trace_id": request.state.trace_id,
        }

    @app.post("/api/v2/proposals", status_code=201, response_model=None)
    def create_proposal(payload: ProposalCreateRequest, request: Request, response: Response) -> Any:
        denied = _require_action(request, Action.PROPOSAL_CREATE, "当前角色不能创建模拟提案")
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            item = paper.create_proposal(
                session.tenant_id,
                session.user_id,
                execution_decision_id=payload.execution_decision_id,
                client_request_id=payload.client_request_id,
            )
        except PaperNotFound:
            return error_response(404, "EXECUTION_DECISION_NOT_FOUND", "执行决定不存在", request.state.trace_id)
        except PaperConflict:
            return error_response(409, "PROPOSAL_CONFLICT", "执行决定不可提案或请求已存在", request.state.trace_id)
        except ValueError:
            return error_response(422, "INVALID_PROPOSAL_REQUEST", "提案请求无效", request.state.trace_id)
        response.headers["ETag"] = f'"{item.version}"'
        response.headers["Location"] = f"/api/v2/proposals/{item.id}"
        return {**_proposal_payload(item), "trace_id": request.state.trace_id}

    @app.get("/api/v2/proposals/{proposal_id}", response_model=None)
    def get_proposal(proposal_id: UUID, request: Request, response: Response) -> Any:
        session = request.state.auth_session
        try:
            item = paper.get_proposal(session.tenant_id, session.user_id, proposal_id)
        except PaperNotFound:
            return error_response(404, "PROPOSAL_NOT_FOUND", "提案不存在", request.state.trace_id)
        response.headers["ETag"] = f'"{item.version}"'
        return {**_proposal_payload(item), "trace_id": request.state.trace_id}

    @app.post("/api/v2/proposals/{proposal_id}/cancel", response_model=None)
    def cancel_proposal(
        proposal_id: UUID, payload: ProposalCancelRequest, request: Request, response: Response,
    ) -> Any:
        denied = _require_action(request, Action.PROPOSAL_CREATE, "当前角色不能取消模拟提案")
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            item = paper.cancel_proposal(
                session.tenant_id,
                session.user_id,
                proposal_id,
                expected_version=payload.proposal_version,
                client_request_id=payload.client_request_id,
            )
        except PaperNotFound:
            return error_response(404, "PROPOSAL_NOT_FOUND", "提案不存在", request.state.trace_id)
        except PaperPreconditionFailed:
            return error_response(412, "PROPOSAL_VERSION_CHANGED", "提案版本已变化", request.state.trace_id)
        except PaperConflict:
            return error_response(409, "PROPOSAL_ALREADY_DECIDED", "提案已处理，不能取消", request.state.trace_id)
        response.headers["ETag"] = f'"{item.version}"'
        return {**_proposal_payload(item), "trace_id": request.state.trace_id}

    @app.post("/api/v2/proposals/{proposal_id}/approve", response_model=None)
    def approve_proposal(
        proposal_id: UUID,
        payload: ProposalVersionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    ) -> Any:
        denied = _require_action(request, Action.PROPOSAL_APPROVE, "当前角色不能确认模拟提案")
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            result = paper.approve_proposal(
                session.tenant_id,
                session.user_id,
                proposal_id,
                expected_version=payload.proposal_version,
                idempotency_key=idempotency_key,
                channel="WEB",
            )
        except PaperNotFound:
            return error_response(404, "PROPOSAL_NOT_FOUND", "提案不存在", request.state.trace_id)
        except PaperPreconditionFailed:
            return error_response(412, "PROPOSAL_VERSION_CHANGED", "提案版本已变化", request.state.trace_id)
        except PaperConflict:
            return error_response(409, "PROPOSAL_ALREADY_DECIDED", "提案已处理或幂等键冲突", request.state.trace_id)
        except ValueError:
            return error_response(422, "INVALID_IDEMPOTENCY_KEY", "幂等键无效", request.state.trace_id)
        return {**_approval_payload(result), "trace_id": request.state.trace_id}

    @app.get("/api/v2/trades", response_model=None)
    def list_trades(request: Request, limit: int = Query(default=100, ge=1, le=500)) -> Any:
        session = request.state.auth_session
        return {
            "items": [
                _trade_payload(item)
                for item in paper.list_trades(session.tenant_id, session.user_id, limit=limit)
            ],
            "next_cursor": None,
            "has_more": False,
            "trace_id": request.state.trace_id,
        }

    @app.get("/api/v2/trades/{public_trade_id}", response_model=None)
    def get_trade(public_trade_id: str, request: Request) -> Any:
        session = request.state.auth_session
        try:
            item = paper.get_trade(session.tenant_id, session.user_id, public_trade_id)
        except PaperNotFound:
            return error_response(404, "TRADE_NOT_FOUND", "交易事件不存在", request.state.trace_id)
        return {**_trade_payload(item), "trace_id": request.state.trace_id}

    @app.get("/api/v2/trades/{public_trade_id}/timeline", response_model=None)
    def get_trade_timeline(public_trade_id: str, request: Request) -> Any:
        session = request.state.auth_session
        try:
            items = paper.trade_timeline(session.tenant_id, session.user_id, public_trade_id)
        except PaperNotFound:
            return error_response(404, "TRADE_NOT_FOUND", "交易事件不存在", request.state.trace_id)
        return {"items": items, "trace_id": request.state.trace_id}

    @app.get("/api/v2/trades/{public_trade_id}/chart", response_model=None)
    def get_trade_chart(public_trade_id: str, request: Request) -> Any:
        session = request.state.auth_session
        try:
            chart = paper.trade_chart(session.tenant_id, session.user_id, public_trade_id)
        except PaperNotFound:
            return error_response(404, "TRADE_NOT_FOUND", "交易事件不存在", request.state.trace_id)
        return {**_paper_chart_payload(chart), "trace_id": request.state.trace_id}

    @app.post("/api/v2/trades/{public_trade_id}:reconcile-paper", response_model=None)
    def reconcile_paper_trade(public_trade_id: str, request: Request) -> Any:
        denied = _require_action(request, Action.PROPOSAL_APPROVE, "当前角色不能推进模拟交易状态")
        if denied is not None:
            return denied
        session = request.state.auth_session
        try:
            item = paper.reconcile_trade(session.tenant_id, session.user_id, public_trade_id)
        except PaperNotFound:
            return error_response(404, "TRADE_NOT_FOUND", "交易事件不存在", request.state.trace_id)
        except PaperConflict:
            return error_response(409, "PAPER_RECONCILE_CONFLICT", "模拟交易状态无法推进", request.state.trace_id)
        return {**_trade_payload(item), "trace_id": request.state.trace_id}

    @app.get("/api/v2/operations/{operation_id}", response_model=None)
    def get_operation(operation_id: UUID, request: Request) -> Any:
        session = request.state.auth_session
        try:
            operation = notifications.get_operation(
                session.tenant_id, session.user_id, operation_id,
            )
        except NotificationNotFound:
            return error_response(404, "OPERATION_NOT_FOUND", "操作不存在或已过期", request.state.trace_id)
        return {
            "id": str(operation.id),
            "operation_type": operation.operation_type,
            "resource_type": operation.resource_type,
            "resource_id": str(operation.resource_id) if operation.resource_id else None,
            "status": operation.state,
            "progress": operation.progress,
            "result_link": operation.result_link,
            "safe_error_code": operation.safe_error_code,
            "version": operation.version,
            "created_at": operation.created_at,
            "updated_at": operation.updated_at,
            "trace_id": request.state.trace_id,
        }

    app.include_router(build_admin_console_router(pool, identity))
    app.include_router(build_research_router(pool))
    app.include_router(build_shadow_router(pool))
    app.include_router(build_paper_orders_router(pool))
    app.include_router(build_pattern_study_router(pool))
    app.include_router(build_coinglass_context_router(pool))
    app.include_router(build_volume_profile_router(pool))
    app.include_router(build_mcp_market_preview_router(pool))
    # Operator-pinned transport alias only; never derive an OAuth audience from
    # Host/Forwarded, browser query parameters, or dynamic client registration.
    tunnel_resource = os.environ.get("GATESCOPE_MCP_OAUTH_TUNNEL_RESOURCE", "")
    app.include_router(build_mcp_oauth_router(
        pool, public_origin=public_origin, cookie_secure=cookie_secure,
        require_credential_write=_require_credential_write,
        resource_aliases=(tunnel_resource,) if tunnel_resource else (),
    ))
    app.include_router(
        build_v2_router(PostgresMarketReadDatabase(pool), identity, cursor_secret=cursor_secret)
    )
    web_dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if web_dist.is_dir():
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="formal-web-assets")

    @app.get("/login", include_in_schema=False, response_model=None)
    @app.get("/markets/chart", include_in_schema=False, response_model=None)
    @app.get("/strategies/alerts", include_in_schema=False, response_model=None)
    @app.get("/strategies/backtests", include_in_schema=False, response_model=None)
    @app.get("/trading/paper", include_in_schema=False, response_model=None)
    @app.get("/trading/shadow", include_in_schema=False, response_model=None)
    @app.get("/trading/exchange-accounts", include_in_schema=False, response_model=None)
    @app.get("/integrations/mcp", include_in_schema=False, response_model=None)
    @app.get("/integrations/mcp/authorize", include_in_schema=False, response_model=None)
    @app.get("/integrations/data-sources", include_in_schema=False, response_model=None)
    @app.get("/notifications/inbox", include_in_schema=False, response_model=None)
    @app.get("/settings/account", include_in_schema=False, response_model=None)
    @app.get("/settings/notifications", include_in_schema=False, response_model=None)
    @app.get("/admin/overview", include_in_schema=False, response_model=None)
    @app.get("/admin/users", include_in_schema=False, response_model=None)
    @app.get("/admin/resources", include_in_schema=False, response_model=None)
    @app.get("/admin/audit", include_in_schema=False, response_model=None)
    def formal_web() -> Response:
        if not web_dist.is_dir():
            return JSONResponse(
                {"error": {"code": "WEB_BUILD_MISSING", "message": "Web 工作台尚未构建"}},
                status_code=503,
            )
        return FileResponse(web_dist / "index.html", headers={"Cache-Control": "no-store"})

    legacy_web_routes = {
        "/": "/markets/chart",
        "/markets": "/markets/chart",
        "/alerts": "/strategies/alerts",
        "/paper": "/trading/paper",
        "/exchange": "/trading/exchange-accounts",
        "/mcp": "/integrations/mcp",
        "/data-sources": "/integrations/data-sources",
        "/notifications": "/notifications/inbox",
        "/settings": "/settings/account",
        "/settings/security": "/settings/account",
        "/admin": "/admin/overview",
    }
    def legacy_redirect(destination: str) -> Any:
        def redirect() -> RedirectResponse:
            return RedirectResponse(destination, status_code=308)

        return redirect

    for legacy_path, canonical_path in legacy_web_routes.items():
        app.add_api_route(
            legacy_path,
            legacy_redirect(canonical_path),
            methods=["GET"],
            include_in_schema=False,
        )

    return app


def _set_auth_cookies(response: JSONResponse, token: str, csrf: str, max_age: int, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=max_age, secure=secure, httponly=True, samesite="strict", path="/",
    )
    response.set_cookie(
        CSRF_COOKIE, csrf, max_age=max_age, secure=secure, httponly=False, samesite="strict", path="/",
    )


def _resource_payload(item: VersionedResource) -> dict[str, Any]:
    return {
        "id": str(item.id), "kind": item.kind, "name": item.name, "spec": item.spec,
        "etag": item.etag, "created_at": item.created_at, "updated_at": item.updated_at,
    }


def _resource_path(kind: str) -> str:
    return "chart-layouts" if kind == "chart_layout" else "indicator-templates"


def _watchlist_payload(item: Watchlist) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "name": item.name,
        "etag": item.etag,
        "items": [
            {
                "id": str(entry.id),
                "instrument_id": str(entry.instrument_id),
                "venue": entry.venue,
                "market": entry.market,
                "symbol": entry.symbol,
                "position": entry.position,
                "default_timeframe": entry.default_timeframe,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
            }
            for entry in item.items
        ],
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _alert_rule_payload(item: AlertRule) -> dict[str, Any]:
    return {
        "symbol": item.symbol,
        "id": str(item.id),
        "name": item.name,
        "status": item.status,
        "etag": item.etag,
        "current_version": item.current_version,
        "version": {
            "id": str(item.version_id),
            "version": item.current_version,
            "condition": item.condition,
            "routing": item.routing,
            "created_by": str(item.version_created_by),
            "created_at": item.version_created_at,
        },
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _paper_account_payload(item: PaperAccount) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "name": item.name,
        "base_currency": item.base_currency,
        "initial_equity": str(item.initial_equity),
        "realized_pnl": str(item.realized_pnl),
        "equity": str(item.equity),
        "risk_fraction": str(item.risk_fraction),
        "max_notional_fraction": str(item.max_notional_fraction),
        "fee_bps_per_side": str(item.fee_bps_per_side),
        "market_slippage_bps": str(item.market_slippage_bps),
        "max_entry_drift_bps": str(item.max_entry_drift_bps),
        "status": item.status,
        "environment": "PAPER",
        "etag": item.etag,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _exchange_account_payload(item: ExchangeAccount) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "alias": item.alias,
        "venue": item.venue,
        "environment": item.environment,
        "market_type": item.market_type,
        "settle_asset": item.settle_asset,
        "status": item.status,
        "trading_ready": item.trading_ready,
        "credential_version": item.credential_version,
        "credential_fingerprint": item.credential_fingerprint,
        "capabilities": item.capabilities,
        "warning_codes": list(item.warning_codes),
        "last_safe_error_code": item.last_safe_error_code,
        "testnet_armed_at": item.testnet_armed_at,
        "testnet_armed_by": str(item.testnet_armed_by) if item.testnet_armed_by else None,
        "execution_freeze_code": item.execution_freeze_code,
        "validation_requested_at": item.validation_requested_at,
        "validated_at": item.validated_at,
        "etag": item.etag,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "secret_fields": "NEVER_RETURNED",
        "live_eligible": False,
    }


def _testnet_drill_payload(item: TestnetDrill) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "public_drill_id": item.public_drill_id,
        "paper_trade_id": str(item.paper_trade_id),
        "paper_public_trade_id": item.paper_public_trade_id,
        "exchange_account_id": str(item.exchange_account_id),
        "exchange_alias": item.exchange_alias,
        "instrument_id": str(item.instrument_id),
        "contract": item.contract,
        "side": item.side,
        "environment": "TESTNET",
        "exact_terms": item.exact_terms,
        "exact_terms_digest": item.exact_terms_digest,
        "state": item.state,
        "safe_code": item.safe_code,
        "entry_order_id": item.entry_order_id,
        "filled_contracts": item.filled_contracts,
        "stop_order_id": item.stop_order_id,
        "take_profit_order_id": item.take_profit_order_id,
        "fail_safe_order_id": item.fail_safe_order_id,
        "frozen": item.frozen,
        "version": item.version,
        "created_at": item.created_at,
        "started_at": item.started_at,
        "completed_at": item.completed_at,
        "next_reconcile_at": item.next_reconcile_at,
        "updated_at": item.updated_at,
        "live_eligible": False,
    }


def _execution_certification_payload(
    item: StrategyExecutionCertification,
) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "strategy_version_ref": item.strategy_version_ref,
        "environment": item.environment,
        "decision": item.decision,
        "evidence_tier": item.evidence_tier,
        "certification_policy_version": item.certification_policy_version,
        "source_artifact_digest": item.source_artifact_digest,
        "certification_digest": item.certification_digest,
        "approved_at": item.approved_at,
        "expires_at": item.expires_at,
        "created_at": item.created_at,
    }


def _signal_payload(item: SignalEvidence) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "instrument_id": str(item.instrument_id),
        "symbol": item.symbol,
        "timeframe": item.timeframe,
        "strategy_version_ref": item.strategy_version_ref,
        "policy_version": item.policy_version,
        "evidence_tier": item.evidence_tier,
        "eligibility_status": item.eligibility_status,
        "side": item.side,
        "entry_reference": str(item.entry_reference),
        "stop_price": str(item.stop_price),
        "take_profit_price": str(item.take_profit_price),
        "time_stop_bars": item.time_stop_bars,
        "decision_at": item.decision_at,
        "data_watermark": item.data_watermark,
        "metrics": item.metrics,
        "assumptions": item.assumptions,
        "reason_codes": item.reason_codes,
        "indicator_snapshot": item.indicator_snapshot,
        "chart_spec": item.chart_spec,
        "evidence_digest": item.evidence_digest,
        "created_at": item.created_at,
        "research_notice": "历史概率下界不保证下一笔盈利；RESEARCH 证据不能生成交易提案。",
    }


def _execution_decision_payload(item: ExecutionDecision) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "signal_evidence_snapshot_id": str(item.signal_evidence_snapshot_id),
        "paper_account_id": str(item.paper_account_id),
        "decision": item.decision,
        "environment": item.environment,
        "authorization_mode": item.authorization_mode,
        "exact_terms": item.exact_terms,
        "exact_terms_digest": item.exact_terms_digest,
        "runtime_gates": item.runtime_gates,
        "risk_snapshot": item.risk_snapshot,
        "expires_at": item.expires_at,
        "created_at": item.created_at,
        "live_eligible": False,
    }


def _proposal_payload(item: PaperProposal) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "execution_decision_id": str(item.execution_decision_id),
        "signal_evidence_snapshot_id": str(item.signal_evidence_snapshot_id),
        "symbol": item.symbol,
        "timeframe": item.timeframe,
        "environment": item.environment,
        "authorization_mode": item.authorization_mode,
        "exact_terms": item.exact_terms,
        "exact_terms_digest": item.exact_terms_digest,
        "expires_at": item.expires_at,
        "proposal_digest": item.proposal_digest,
        "status": item.status,
        "version": item.version,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "confirmation_notice": "确认仅授权这份冻结条款；价格漂移或风险复核失败会拒绝，不会自动改价。",
    }


def _trade_payload(item: PaperTrade) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "public_trade_id": item.public_trade_id,
        "proposal_id": str(item.proposal_id),
        "signal_evidence_snapshot_id": str(item.signal_evidence_snapshot_id),
        "paper_account_id": str(item.paper_account_id),
        "instrument_id": str(item.instrument_id),
        "symbol": item.symbol,
        "timeframe": item.timeframe,
        "environment": item.environment,
        "side": item.side,
        "exact_terms": item.exact_terms,
        "exact_terms_digest": item.exact_terms_digest,
        "state": item.state,
        "close_reason": item.close_reason,
        "entry_fill_price": str(item.entry_fill_price),
        "exit_fill_price": str(item.exit_fill_price) if item.exit_fill_price is not None else None,
        "quantity": str(item.quantity),
        "gross_pnl": str(item.gross_pnl) if item.gross_pnl is not None else None,
        "fees": str(item.fees),
        "net_pnl": str(item.net_pnl) if item.net_pnl is not None else None,
        "entry_available_at": item.entry_available_at,
        "closed_at": item.closed_at,
        "evaluated_bars": item.evaluated_bars,
        "last_evaluated_available_at": item.last_evaluated_available_at,
        "protection_status": item.protection_status,
        "version": item.version,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "simulation": True,
        "live_eligible": False,
    }


def _approval_payload(item: ApprovalResult) -> dict[str, Any]:
    return {
        "outcome": item.outcome,
        "proposal_status": item.proposal_status,
        "proposal_version": item.proposal_version,
        "failed_gates": item.gate_codes,
        "trade": _trade_payload(item.trade) if item.trade is not None else None,
    }


def _paper_chart_payload(chart: dict[str, Any]) -> dict[str, Any]:
    result = dict(chart)
    signal = result.get("signal")
    if isinstance(signal, SignalEvidence):
        result["signal"] = _signal_payload(signal)
    result["history_policy"] = (
        "original bars use the exact revisions frozen at signal time; progress bars are append-only "
        "PAPER observations and never rewrite the original evidence"
    )
    return result


def _etag(value: str) -> int:
    if re.fullmatch(r'"[1-9][0-9]*"', value) is None:
        raise ValueError("If-Match must contain one strong numeric ETag")
    return int(value[1:-1])


def _require_alert_write(request: Request) -> JSONResponse | None:
    session = request.state.auth_session
    context = AuthorizationContext(
        tenant_id=session.tenant_id,
        subject_id=session.user_id,
        role=session.role,
        recent_auth_at=session.recent_auth_at,
        mfa_verified_at=session.mfa_verified_at,
        channel="WEB",
    )
    try:
        authorize(context, Action.ALERT_WRITE, now=datetime.now(timezone.utc))
    except AuthorizationDenied:
        return error_response(403, "FORBIDDEN", "当前角色不能修改告警规则", request.state.trace_id)
    return None


def _require_action(request: Request, action: Action, message: str) -> JSONResponse | None:
    session = request.state.auth_session
    context = AuthorizationContext(
        tenant_id=session.tenant_id,
        subject_id=session.user_id,
        role=session.role,
        recent_auth_at=session.recent_auth_at,
        mfa_verified_at=session.mfa_verified_at,
        channel="WEB",
    )
    try:
        authorize(context, action, now=datetime.now(timezone.utc))
    except AuthorizationDenied:
        return error_response(403, "FORBIDDEN", message, request.state.trace_id)
    return None


def _require_owner(request: Request) -> JSONResponse | None:
    if request.state.auth_session.role is not Role.OWNER:
        return error_response(403, "FORBIDDEN", "仅所有者可以管理敏感接入", request.state.trace_id)
    return None


def _require_user_management(request: Request) -> JSONResponse | None:
    session = request.state.auth_session
    if session.role is not Role.OWNER or not session.fully_authenticated:
        return error_response(403, "FORBIDDEN", "仅工作区管理员可以管理成员", request.state.trace_id)
    if session.recent_auth_at is None or session.recent_auth_at < datetime.now(timezone.utc) - timedelta(minutes=10):
        return error_response(403, "RECENT_OWNER_AUTH_REQUIRED", "请确认当前密码后继续管理成员", request.state.trace_id)
    return None


def _require_credential_write(request: Request) -> JSONResponse | None:
    session = request.state.auth_session
    if session.role is not Role.OWNER:
        return error_response(
            403, "ROLE_FORBIDDEN", "当前角色不能管理敏感接入，需要 OWNER 权限", request.state.trace_id,
        )
    context = AuthorizationContext(
        tenant_id=session.tenant_id,
        subject_id=session.user_id,
        role=session.role,
        recent_auth_at=session.recent_auth_at,
        mfa_verified_at=session.mfa_verified_at,
        channel="WEB",
    )
    try:
        authorize(context, Action.CREDENTIAL_WRITE, now=datetime.now(timezone.utc))
    except AuthorizationDenied:
        return error_response(
            403,
            "RECENT_AUTH_REQUIRED",
            "密码认证已超过十分钟，请确认当前密码后继续",
            request.state.trace_id,
        )
    return None


def _mcp_client_payload(item: McpClient) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "name": item.name,
        "status": item.status,
        "etag": item.etag,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _mcp_token_payload(item: McpToken) -> dict[str, Any]:
    if item.revoked_at is not None:
        status = "REVOKED"
    elif item.expires_at <= datetime.now(timezone.utc):
        status = "EXPIRED"
    else:
        status = "ACTIVE"
    return {
        "id": str(item.id),
        "client_id": str(item.client_id),
        "label": item.label,
        "token_hint": item.token_hint,
        "scopes": list(item.scopes),
        "status": status,
        "expires_at": item.expires_at,
        "last_used_at": item.last_used_at,
        "revoked_at": item.revoked_at,
        "created_at": item.created_at,
    }


def _telegram_connection_payload(item: TelegramConnection) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "telegram_user_id": str(item.telegram_user_id),
        "telegram_chat_id": str(item.telegram_chat_id),
        "telegram_username": item.telegram_username,
        "status": item.status,
        "etag": item.etag,
        "verified_at": item.verified_at,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _coinglass_config_payload(
    item: CoinglassConfig | None,
    *,
    secret_write_allowed: bool,
    browser_submission_configured: bool,
    trace_id: str,
) -> dict[str, Any]:
    if item is None:
        return {
            "configured": False,
            "origin": COINGLASS_ORIGIN,
            "key_fingerprint": None,
            "status": "NOT_CONFIGURED",
            "last_safe_error_code": None,
            "capability": {},
            "last_tested_at": None,
            "etag": None,
            "secret_write_allowed": secret_write_allowed,
            "browser_submission_configured": browser_submission_configured,
            "trace_id": trace_id,
        }
    return {
        "configured": item.status != "DISABLED",
        "origin": item.origin,
        "key_fingerprint": item.key_fingerprint,
        "status": item.status,
        "last_safe_error_code": item.last_safe_error_code,
        "capability": item.capability,
        "last_tested_at": item.last_tested_at,
        "etag": item.etag,
        "secret_write_allowed": secret_write_allowed,
        "browser_submission_configured": browser_submission_configured,
        "trace_id": trace_id,
    }


def _coinglass_test_payload(item: CoinglassTestJob) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "status": item.status,
        "symbol": item.test_symbol,
        "attempt_count": item.attempt_count,
        "result": item.result,
        "safe_error_code": item.safe_error_code,
        "retry_after_seconds": item.retry_after_seconds,
        "created_at": item.created_at,
        "started_at": item.started_at,
        "completed_at": item.completed_at,
        "updated_at": item.updated_at,
    }


def _telegram_bot_config_payload(
    item: TelegramBotConfig | None,
    *,
    secret_write_allowed: bool,
    public_https: bool,
    wrapping_configured: bool,
    browser_submission_configured: bool,
    legacy_username: str | None,
    legacy_allowed_ids: frozenset[int],
    trace_id: str,
) -> dict[str, Any]:
    if item is not None:
        return {
            "configured": item.configured,
            "source": "WEB_ENCRYPTED",
            "bot_username": item.bot_username,
            "token_configured": True,
            "token_fingerprint": item.token_fingerprint,
            "allowed_user_ids": list(item.allowed_user_ids),
            "status": item.status,
            "last_safe_error_code": item.last_safe_error_code,
            "validated_at": item.validated_at,
            "webhook_configured_at": item.webhook_configured_at,
            "etag": item.etag,
            "secret_write_allowed": secret_write_allowed,
            "public_https": public_https,
            "wrapping_configured": wrapping_configured,
            "browser_submission_configured": browser_submission_configured,
            "trace_id": trace_id,
        }
    return {
        "configured": bool(legacy_username and legacy_allowed_ids),
        "source": "LEGACY_ENV" if legacy_username else "NONE",
        "bot_username": legacy_username,
        "token_configured": bool(legacy_username),
        "token_fingerprint": None,
        "allowed_user_ids": sorted(legacy_allowed_ids),
        "status": "LEGACY_ENV" if legacy_username else "NOT_CONFIGURED",
        "last_safe_error_code": None,
        "validated_at": None,
        "webhook_configured_at": None,
        "etag": None,
        "secret_write_allowed": secret_write_allowed,
        "public_https": public_https,
        "wrapping_configured": wrapping_configured,
        "browser_submission_configured": browser_submission_configured,
        "trace_id": trace_id,
    }


def _notification_channel_payload(item: NotificationChannel) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "type": "TELEGRAM",
        "alias": item.alias,
        "status": item.status,
        "connection_id": str(item.connection_id),
        "etag": item.etag,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _notification_event_payload(item: NotificationEvent) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "event_type": item.event_type,
        "channel_alias": item.channel_alias,
        "status": item.status,
        "telegram_message_id": item.telegram_message_id,
        "safe_error_code": item.safe_error_code,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _notification_route_payload(item: NotificationRoute) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "name": item.name,
        "status": item.status,
        "etag": item.etag,
        **item.spec,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _telegram_message_update(payload: Any) -> tuple[int, int, int, str | None, str]:
    if not isinstance(payload, dict):
        raise TypeError("Telegram update must be an object")
    update_id = _telegram_integer(payload.get("update_id"))
    message = payload.get("message")
    if not isinstance(message, dict):
        raise TypeError("Telegram update does not contain a message")
    sender = message.get("from")
    chat = message.get("chat")
    text = message.get("text")
    if not isinstance(sender, dict) or not isinstance(chat, dict) or not isinstance(text, str):
        raise TypeError("Telegram message is incomplete")
    telegram_user_id = _telegram_integer(sender.get("id"))
    telegram_chat_id = _telegram_integer(chat.get("id"))
    if chat.get("type") != "private" or telegram_user_id != telegram_chat_id:
        raise ValueError("only a matching private Telegram chat can be linked")
    clean_text = text.strip()
    if not 1 <= len(clean_text) <= 256 or not clean_text.startswith("/"):
        raise ValueError("Telegram message is not a bounded command")
    username = sender.get("username")
    if username is not None and not isinstance(username, str):
        raise TypeError("Telegram username must be text")
    return update_id, telegram_user_id, telegram_chat_id, username, clean_text


def _telegram_start_update(payload: Any) -> tuple[int, int, int, str | None, str]:
    update_id, user_id, chat_id, username, text = _telegram_message_update(payload)
    parts = text.split()
    if len(parts) != 2 or parts[0] != "/start":
        raise ValueError("Telegram message is not a link command")
    return update_id, user_id, chat_id, username, parts[1]


def _telegram_command_response(
    paper: PostgresPaperRepository,
    testnet_execution: PostgresTestnetExecutionRepository,
    tenant_id: UUID,
    user_id: UUID,
    text: str,
    public_origin: str,
) -> str:
    parts = text.split()
    command = parts[0].split("@", 1)[0].lower()
    if command in {"/help", "/start"}:
        return (
            "GateScope 行情与 PAPER 助手\n\n"
            "/watchlist — 自选币种、形态和指标按钮菜单\n"
            "/opportunities — 高置信形态筛选、历史净胜率与观察候选\n"
            "/chart BTC 1h — 自选币种带指标 K 线图\n"
            "/patterns — 查询形成中、已形成和已确认结构\n\n"
            "/accounts — 模拟账户与可用余额\n"
            "/positions — 当前持仓、已平仓记录\n"
            "/trades — 网页 / MCP 模拟委托与旧模型交易\n"
            "/order 订单编号 — 盘口委托与成交详情\n"
            "/trade paper_… — 刷新并查询一笔交易\n"
            "/signals — 最近 5 个冻结信号证据\n"
            "/testnets — 最近 5 次 TESTNET 演练\n"
            "/testnet testnet_… — 查询保护与事件状态\n\n"
            "为防误触，TG 暂不接受自由文本改价、改数量或 LIVE 指令。"
        )
    if command == "/trades" and len(parts) == 1:
        trades = paper.list_trades(tenant_id, user_id, limit=5)
        if not trades:
            return "目前没有 PAPER 交易。符合门禁的信号需先在网页生成并确认提案。"
        lines = ["最近 PAPER 交易"]
        for trade_item in trades:
            pnl = f" · 净PnL {trade_item.net_pnl}" if trade_item.net_pnl is not None else ""
            lines.append(
                f"{trade_item.public_trade_id}\n"
                f"{trade_item.symbol} {trade_item.side} · {trade_item.state}{pnl}"
            )
        return "\n\n".join(lines)
    if command == "/signals" and len(parts) == 1:
        signals = paper.list_signals(tenant_id, user_id, limit=5)
        if not signals:
            return "目前没有冻结信号证据。研究告警本身不会被冒充为盈利概率。"
        lines = ["最近信号证据"]
        for signal_item in signals:
            lines.append(
                f"{signal_item.symbol} {signal_item.timeframe} {signal_item.side}\n"
                f"{signal_item.evidence_tier} · {signal_item.eligibility_status} · "
                f"{str(signal_item.id)[:8]}"
            )
        return "\n\n".join(lines)
    if command == "/trade" and len(parts) == 2:
        trade_id = parts[1]
        if re.fullmatch(r"paper_[0-9A-HJKMNP-TV-Z]{26}", trade_id) is None:
            return "交易编号格式不正确。请使用 /trade paper_…"
        try:
            trade_item = paper.reconcile_trade(tenant_id, user_id, trade_id)
        except PaperNotFound:
            return "没有找到这笔交易，或它不属于当前绑定账号。"
        terms = trade_item.exact_terms
        pnl = str(trade_item.net_pnl) if trade_item.net_pnl is not None else "尚未结算"
        reason = trade_item.close_reason or "等待 TP / SL / time stop"
        return (
            f"{trade_item.symbol} · {trade_item.side} · PAPER\n"
            f"状态：{trade_item.state} / {reason}\n"
            f"入场：{trade_item.entry_fill_price}  数量：{trade_item.quantity}\n"
            f"止盈：{terms['take_profit_price']}  止损：{terms['stop_price']}\n"
            f"净PnL：{pnl}  已观察：{trade_item.evaluated_bars} 根\n"
            f"水位：{trade_item.last_evaluated_available_at.isoformat()}\n\n"
            f"证据与走势图：{public_origin}/\n"
            "原始证据不会按后续行情重绘。"
        )
    if command == "/testnets" and len(parts) == 1:
        drills = testnet_execution.list_drills(tenant_id, user_id, limit=5)
        if not drills:
            return "目前没有 TESTNET 演练。订单执行默认关闭，且只有认证策略可由网页显式发起。"
        lines = ["最近 Gate TESTNET 演练"]
        for drill in drills:
            frozen = " · 已冻结" if drill.frozen else ""
            lines.append(
                f"{drill.public_drill_id}\n"
                f"{drill.contract} {drill.side} · {drill.state}{frozen} · "
                f"合约数 {drill.filled_contracts}"
            )
        return "\n\n".join(lines)
    if command == "/testnet" and len(parts) == 2:
        drill_id = parts[1]
        if re.fullmatch(r"testnet_[0-9A-HJKMNP-TV-Z]{26}", drill_id) is None:
            return "演练编号格式不正确。请使用 /testnet testnet_…"
        try:
            drill = testnet_execution.get_drill(tenant_id, user_id, drill_id)
            events = testnet_execution.timeline(tenant_id, user_id, drill_id)
        except TestnetDrillNotFound:
            return "没有找到这次 TESTNET 演练，或它不属于当前绑定账号。"
        terms = drill.exact_terms
        last_event = events[-1] if events else None
        event_line = (
            f"最近事件：#{last_event['sequence']} {last_event['event_type']} · "
            f"{last_event['occurred_at'].isoformat()}"
            if last_event is not None else "最近事件：尚无可见事件"
        )
        safety = drill.safe_code or "保护监控正常/尚未产生安全码"
        next_check = drill.next_reconcile_at.isoformat() if drill.next_reconcile_at else "无需轮询"
        return (
            f"{drill.contract} · {drill.side} · Gate TESTNET\n"
            f"状态：{drill.state}  冻结：{'是' if drill.frozen else '否'}\n"
            f"来源：{drill.paper_public_trade_id}\n"
            f"入场参考：{terms.get('entry_reference', '—')}  "
            f"BASE 数量：{terms.get('quantity', '—')}\n"
            f"止盈：{terms.get('take_profit_price', '—')}  "
            f"止损：{terms.get('stop_price', '—')}\n"
            f"已成交合约数：{drill.filled_contracts}\n"
            f"安全状态：{safety}\n"
            f"下次保护对账：{next_check}\n"
            f"{event_line}\n"
            f"账本更新时间：{drill.updated_at.isoformat()}\n\n"
            f"完整只读时间线：{public_origin}/\n"
            "此查询只读，不会改价、补单或重写历史条款。"
        )
    if command in {"/approve", "/confirm", "/order"}:
        return (
            "安全提示：当前 TG 只开放查询，尚未开放原子一次性 callback 确认。"
            "请在网页核对冻结条款后确认；任何自由文本数量、价格或 LIVE 指令都会被拒绝。"
        )
    return "无法识别这个命令。发送 /help 查看可用的安全命令。"


def _telegram_integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**63:
        raise ValueError("Telegram identifier is invalid")
    return value


def _positive_id_set(value: str) -> frozenset[int]:
    if not value.strip():
        return frozenset()
    try:
        parsed = frozenset(int(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise RuntimeError("GATESCOPE_TELEGRAM_ALLOWED_USER_IDS must contain integer IDs") from exc
    if any(item <= 0 or item >= 2**63 for item in parsed):
        raise RuntimeError("GATESCOPE_TELEGRAM_ALLOWED_USER_IDS contains an invalid ID")
    return parsed


def _database_url() -> str:
    value = os.environ.get("GATESCOPE_DATABASE_URL", "")
    if value.startswith("postgresql+psycopg://"):
        value = value.replace("postgresql+psycopg://", "postgresql://", 1)
    if not value.startswith(("postgresql://", "postgres://")):
        raise RuntimeError("GATESCOPE_DATABASE_URL must be a PostgreSQL URL")
    return value


def _required_secret(name: str, *, minimum: int = 0, exact: int | None = None) -> bytes:
    encoded = os.environ.get(name, "")
    try:
        padding = "=" * (-len(encoded) % 4)
        value = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError(f"{name} must be valid base64url") from exc
    if exact is not None and len(value) != exact:
        raise RuntimeError(f"{name} must decode to exactly {exact} bytes")
    if len(value) < minimum:
        raise RuntimeError(f"{name} must decode to at least {minimum} bytes")
    return value


def _key_version() -> str:
    value = os.environ.get("GATESCOPE_IDENTITY_KEY_VERSION", "").strip()
    if not value or len(value) > 64:
        raise RuntimeError("GATESCOPE_IDENTITY_KEY_VERSION is required")
    return value


def _login_workspace_slugs() -> tuple[str, ...]:
    raw = os.environ.get("GATESCOPE_LOGIN_WORKSPACES", "default")
    values = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    if not values or len(values) > 50 or len(values) != len(set(values)):
        raise RuntimeError("GATESCOPE_LOGIN_WORKSPACES must contain 1-50 unique workspace slugs")
    if any(re.fullmatch(r"[a-z0-9][a-z0-9-]{2,62}", value) is None for value in values):
        raise RuntimeError("GATESCOPE_LOGIN_WORKSPACES contains an invalid workspace slug")
    return values


def _optional_credential_encryptor() -> RsaOaepCredentialEncryptor | None:
    encoded_key = os.environ.get("GATESCOPE_CREDENTIAL_WRAP_PUBLIC_KEY", "").strip()
    key_version = os.environ.get("GATESCOPE_CREDENTIAL_KEY_VERSION", "").strip()
    if not encoded_key and not key_version:
        return None
    if not encoded_key or not key_version:
        raise RuntimeError(
            "GATESCOPE_CREDENTIAL_WRAP_PUBLIC_KEY and GATESCOPE_CREDENTIAL_KEY_VERSION must be set together"
        )
    try:
        return RsaOaepCredentialEncryptor.from_base64_der(encoded_key, key_version)
    except ValueError as exc:
        raise RuntimeError("credential wrapping public key configuration is invalid") from exc


def _optional_telegram_config_encryptor() -> RsaOaepCredentialEncryptor | None:
    encoded_key = os.environ.get("GATESCOPE_TELEGRAM_WRAP_PUBLIC_KEY", "").strip()
    key_version = os.environ.get("GATESCOPE_TELEGRAM_KEY_VERSION", "").strip()
    if not encoded_key and not key_version:
        return None
    if not encoded_key or not key_version:
        raise RuntimeError(
            "GATESCOPE_TELEGRAM_WRAP_PUBLIC_KEY and GATESCOPE_TELEGRAM_KEY_VERSION must be set together"
        )
    try:
        return RsaOaepCredentialEncryptor.from_base64_der(encoded_key, key_version)
    except ValueError as exc:
        raise RuntimeError("Telegram wrapping public key configuration is invalid") from exc


def _optional_coinglass_config_encryptor() -> RsaOaepCredentialEncryptor | None:
    encoded_key = os.environ.get("GATESCOPE_COINGLASS_WRAP_PUBLIC_KEY", "").strip()
    key_version = os.environ.get("GATESCOPE_COINGLASS_KEY_VERSION", "").strip()
    if not encoded_key and not key_version:
        return None
    if not encoded_key or not key_version:
        raise RuntimeError(
            "GATESCOPE_COINGLASS_WRAP_PUBLIC_KEY and GATESCOPE_COINGLASS_KEY_VERSION "
            "must be set together"
        )
    try:
        return RsaOaepCredentialEncryptor.from_base64_der(encoded_key, key_version)
    except ValueError as exc:
        raise RuntimeError("Coinglass wrapping public key configuration is invalid") from exc


def _optional_submission_decryptor() -> RsaOaepSubmissionDecryptor | None:
    public_key = os.environ.get("GATESCOPE_SUBMISSION_WRAP_PUBLIC_KEY", "").strip()
    private_key = os.environ.get("GATESCOPE_SUBMISSION_WRAP_PRIVATE_KEY", "").strip()
    key_version = os.environ.get("GATESCOPE_SUBMISSION_KEY_VERSION", "").strip()
    values = (public_key, private_key, key_version)
    if not any(values):
        return None
    if not all(values):
        raise RuntimeError(
            "GATESCOPE_SUBMISSION_WRAP_PUBLIC_KEY, GATESCOPE_SUBMISSION_WRAP_PRIVATE_KEY "
            "and GATESCOPE_SUBMISSION_KEY_VERSION must be set together"
        )
    try:
        return RsaOaepSubmissionDecryptor.from_base64_der(
            public_key, private_key, key_version,
        )
    except ValueError as exc:
        raise RuntimeError("submission wrapping key configuration is invalid") from exc


def _legacy_plaintext_submission_allowed(request: Request) -> bool:
    """Allow old request bodies only on a real loopback/test harness hop."""

    client_host = (request.client.host if request.client else "").lower()
    request_host = (request.url.hostname or "").lower()
    return client_host in {"127.0.0.1", "::1", "localhost", "testclient"} and request_host in {
        "127.0.0.1", "::1", "localhost", "testserver",
    }


SensitiveModel = TypeVar("SensitiveModel", bound=BaseModel)


def _resolve_sensitive_submission(
    submitted: SensitiveModel | EncryptedSubmissionRequest,
    request: Request,
    *,
    decryptor: RsaOaepSubmissionDecryptor | None,
    expected_purpose: str,
    model_type: type[SensitiveModel],
) -> SensitiveModel | JSONResponse:
    if isinstance(submitted, EncryptedSubmissionRequest):
        if decryptor is None:
            return error_response(
                503,
                "SUBMISSION_ENCRYPTION_UNAVAILABLE",
                "应用层加密暂不可用",
                request.state.trace_id,
            )
        try:
            payload = decryptor.decrypt(
                submitted.encrypted_submission,
                expected_purpose=expected_purpose,
            )
            return model_type.model_validate(payload)
        except SubmissionError as exc:
            return error_response(
                exc.status_code,
                exc.code,
                "加密提交无效或已失效",
                request.state.trace_id,
            )
        except ValidationError:
            return error_response(
                422,
                "INVALID_SUBMISSION_PAYLOAD",
                "加密提交内容无效",
                request.state.trace_id,
            )
    if not _legacy_plaintext_submission_allowed(request):
        return error_response(
            403,
            "ENCRYPTED_SUBMISSION_REQUIRED",
            "此连接仅接受浏览器加密提交",
            request.state.trace_id,
        )
    try:
        return model_type.model_validate(submitted.model_dump())
    except ValidationError:
        return error_response(
            422,
            "INVALID_SUBMISSION_PAYLOAD",
            "请求内容无效",
            request.state.trace_id,
        )


def _safe_provider_metadata(value: Any) -> Any:
    blocked = ("key", "token", "secret", "password", "email", "phone", "user_id", "account_id")
    if isinstance(value, dict):
        return {
            str(key): _safe_provider_metadata(item)
            for key, item in value.items()
            if not any(fragment in str(key).lower() for fragment in blocked)
        }
    if isinstance(value, list):
        return [_safe_provider_metadata(item) for item in value[:100]]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
