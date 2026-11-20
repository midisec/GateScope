"""Explicit optional research scope and owner-scoped SELECT-only evidence access."""

from alembic import op

revision = "0045_mcp_research_read"
down_revision = "0044_coinglass_queue_progress"
branch_labels = depends_on = None

TABLES = (
    "watchlists",
    "watchlist_items",
    "research_backtests",
    "research_backtest_results",
    "research_structure_inputs",
    "research_structure_events",
    "research_structure_cases",
    "market_pattern_tracking",
    "market_pattern_studies",
    "market_pattern_events",
    "market_structure_stages",
)


def _scope_checks(research):
    # Locate only these legacy unnamed scope CHECKs; never drop the required
    # mcp:connect check or unrelated ownership/lifetime constraints.
    for table in ("public.mcp_tokens", "public.oauth_grants", "gatescope_security.oauth_registered_clients"):
        rows = (
            op.get_bind()
            .exec_driver_sql(
                "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=%s::regclass AND contype='c'",
                (table,),
            )
            .fetchall()
        )
        selected = [
            (name, definition)
            for name, definition in rows
            if "scopes <@" in definition or "cardinality(scopes)" in definition
        ]
        if len(selected) != (2 if table == "public.mcp_tokens" else 1):
            raise RuntimeError("UNEXPECTED_MCP_SCOPE_CONSTRAINTS")
        for name, _ in selected:
            op.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{name.replace(chr(34), chr(34) * 2)}"')
        scopes = ["mcp:connect", "market:read", "analysis:read"]
        if table == "public.mcp_tokens":
            scopes.append("alerts:read")
            op.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT mcp_scope_cardinality CHECK(cardinality(scopes) BETWEEN 1 AND {5 if research else 4})"
            )
        if research:
            scopes.append("research:read")
        allowed = ",".join("'" + scope + "'" for scope in scopes)
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT mcp_scope_allowlist CHECK(scopes <@ ARRAY[{allowed}]::text[])"
        )


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    _scope_checks(True)  # No existing client, grant or token scopes are expanded.
    op.execute("""
      CREATE POLICY mcp_watchlist_owner ON watchlists AS RESTRICTIVE FOR SELECT TO gatescope_mcp
        USING(owner_user_id=gatescope_security.current_subject_id());
      CREATE POLICY mcp_watchlist_item_owner ON watchlist_items AS RESTRICTIVE FOR SELECT TO gatescope_mcp
        USING(EXISTS(SELECT 1 FROM public.watchlists w WHERE w.id=watchlist_id AND w.tenant_id=watchlist_items.tenant_id
          AND w.owner_user_id=gatescope_security.current_subject_id()));
    """)
    op.execute("GRANT SELECT ON " + ",".join(TABLES) + " TO gatescope_mcp")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    # Explicit fail-closed rollback: revoke only credentials which actually
    # carried research permission. Never silently turn a research token into a
    # still-working base token; preserve unrelated base grants and all evidence.
    op.execute("""
      UPDATE mcp_tokens t SET revoked_at=coalesce(t.revoked_at,now())
        WHERE 'research:read'=ANY(t.scopes) OR EXISTS(
          SELECT 1 FROM oauth_access_tokens a JOIN oauth_grants g ON g.id=a.grant_id
          WHERE a.token_id=t.id AND 'research:read'=ANY(g.scopes));
      UPDATE oauth_codes c SET used_at=coalesce(c.used_at,now()) FROM oauth_grants g
        WHERE c.grant_id=g.id AND 'research:read'=ANY(g.scopes);
      UPDATE oauth_refresh_tokens r SET used_at=coalesce(r.used_at,now()) FROM oauth_grants g
        WHERE r.grant_id=g.id AND 'research:read'=ANY(g.scopes);
      UPDATE gatescope_security.oauth_pending_requests SET consumed_at=coalesce(consumed_at,now())
        WHERE 'research:read'=ANY(scopes);
      UPDATE oauth_grants SET revoked_at=coalesce(revoked_at,now()),scopes=array_remove(scopes,'research:read')
        WHERE 'research:read'=ANY(scopes);
      UPDATE mcp_tokens SET scopes=array_remove(scopes,'research:read') WHERE 'research:read'=ANY(scopes);
      UPDATE gatescope_security.oauth_registered_clients SET scopes=array_remove(scopes,'research:read')
        WHERE 'research:read'=ANY(scopes);
    """)
    _scope_checks(False)
    op.execute("REVOKE SELECT ON " + ",".join(TABLES) + " FROM gatescope_mcp")
    op.execute(
        "DROP POLICY mcp_watchlist_item_owner ON watchlist_items; DROP POLICY mcp_watchlist_owner ON watchlists"
    )
