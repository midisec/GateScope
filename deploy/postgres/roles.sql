-- Run once as a PostgreSQL cluster administrator. Login roles should inherit
-- exactly one workload role; passwords/certificates are provisioned externally.
DO $$
DECLARE role_name text;
BEGIN
  FOREACH role_name IN ARRAY ARRAY[
    'gatescope_api', 'gatescope_auth', 'gatescope_market', 'gatescope_strategy',
    'gatescope_notification', 'gatescope_executor', 'gatescope_mcp', 'gatescope_chart',
    'gatescope_paper'
  ] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS', role_name);
    END IF;
  END LOOP;
END
$$;
