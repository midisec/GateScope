"""Deny-by-default RBAC and strong-auth policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from uuid import UUID

from gatescope.domain.market import utc


class Role(str, Enum):
    VIEWER = "VIEWER"
    RESEARCHER = "RESEARCHER"
    OPERATOR = "OPERATOR"
    OWNER = "OWNER"


class Action(str, Enum):
    MARKET_READ = "market:read"
    ANALYSIS_READ = "analysis:read"
    ALERT_WRITE = "alert:write"
    STRATEGY_DRAFT = "strategy:draft"
    STRATEGY_PUBLISH = "strategy:publish"
    PROPOSAL_CREATE = "proposal:create"
    PROPOSAL_APPROVE = "proposal:approve"
    RISK_REDUCE = "risk:reduce"
    POLICY_PAUSE = "policy:pause"
    POLICY_WRITE = "policy:write"
    CREDENTIAL_WRITE = "credential:write"
    USER_MANAGE = "user:manage"
    SAFE_KILL = "trading:safe-kill"
    FLATTEN = "trading:flatten"
    LIVE_RESUME = "trading:live-resume"


ROLE_ACTIONS: dict[Role, frozenset[Action]] = {
    Role.VIEWER: frozenset({Action.MARKET_READ, Action.ANALYSIS_READ}),
    Role.RESEARCHER: frozenset({
        Action.MARKET_READ, Action.ANALYSIS_READ, Action.ALERT_WRITE,
        Action.STRATEGY_DRAFT, Action.PROPOSAL_CREATE,
    }),
    Role.OPERATOR: frozenset({
        Action.MARKET_READ, Action.ANALYSIS_READ, Action.ALERT_WRITE,
        Action.PROPOSAL_CREATE, Action.PROPOSAL_APPROVE, Action.RISK_REDUCE,
        Action.POLICY_PAUSE, Action.SAFE_KILL,
    }),
    Role.OWNER: frozenset(Action),
}

RECENT_AUTH_ACTIONS = frozenset({
    Action.STRATEGY_PUBLISH, Action.POLICY_WRITE, Action.CREDENTIAL_WRITE,
    Action.USER_MANAGE, Action.FLATTEN, Action.LIVE_RESUME,
})
MFA_ACTIONS = frozenset({Action.FLATTEN, Action.LIVE_RESUME})
WEB_ONLY_ACTIONS = frozenset({Action.LIVE_RESUME})


class AuthorizationDenied(PermissionError):
    pass


@dataclass(frozen=True)
class AuthorizationContext:
    tenant_id: UUID
    subject_id: UUID
    role: Role
    recent_auth_at: datetime | None
    mfa_verified_at: datetime | None
    channel: str


def authorize(
    context: AuthorizationContext,
    action: Action,
    *,
    now: datetime,
    recent_window: timedelta = timedelta(minutes=10),
) -> None:
    current = utc(now)
    if action not in ROLE_ACTIONS.get(context.role, frozenset()):
        raise AuthorizationDenied("action is not allowed")
    if action in RECENT_AUTH_ACTIONS:
        if context.recent_auth_at is None or utc(context.recent_auth_at) < current - recent_window:
            raise AuthorizationDenied("recent authentication required")
    if action in MFA_ACTIONS:
        if context.mfa_verified_at is None or utc(context.mfa_verified_at) < current - recent_window:
            raise AuthorizationDenied("recent MFA required")
    if action in WEB_ONLY_ACTIONS and context.channel != "WEB":
        raise AuthorizationDenied("action is restricted to the Web channel")


def authorize_tenant(context: AuthorizationContext, resource_tenant_id: UUID) -> None:
    if context.tenant_id != resource_tenant_id:
        raise AuthorizationDenied("resource is outside the authenticated tenant")
