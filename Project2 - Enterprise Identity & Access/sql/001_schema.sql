-- Run once as the database owner. The web process must use bridge_app.
CREATE ROLE bridge_app LOGIN NOSUPERUSER NOBYPASSRLS;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE TABLE users (
    id uuid PRIMARY KEY,
    tenant text NOT NULL,
    external_id text NOT NULL,
    username text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    UNIQUE (tenant, external_id), UNIQUE (tenant, username), UNIQUE (tenant, id)
);
CREATE TABLE groups (
    id uuid PRIMARY KEY,
    tenant text NOT NULL,
    name text NOT NULL,
    UNIQUE (tenant, name), UNIQUE (tenant, id)
);
CREATE TABLE memberships (
    tenant text NOT NULL,
    group_id uuid NOT NULL,
    user_id uuid NOT NULL,
    PRIMARY KEY (group_id, user_id),
    FOREIGN KEY (tenant, group_id) REFERENCES groups(tenant, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant, user_id) REFERENCES users(tenant, id) ON DELETE CASCADE
);
CREATE TABLE sessions (
    id text PRIMARY KEY,
    tenant text NOT NULL,
    user_id uuid NOT NULL,
    expires_at timestamptz NOT NULL DEFAULT now() + interval '8 hours',
    FOREIGN KEY (tenant, user_id) REFERENCES users(tenant, id) ON DELETE CASCADE
);
CREATE TABLE documents (
    id uuid PRIMARY KEY,
    tenant text NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    required_role text NOT NULL CHECK (required_role IN ('employee', 'manager'))
);
-- Even an accidentally unfiltered directory query is tenant scoped.
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['users','groups','memberships','sessions'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY tenant_scope ON %I USING (tenant = current_setting(''app.tenant'', true)) WITH CHECK (tenant = current_setting(''app.tenant'', true))', t);
  END LOOP;
END $$;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
CREATE POLICY permitted_documents ON documents FOR SELECT USING (
    tenant = current_setting('app.tenant', true)
    AND EXISTS (
      SELECT 1 FROM users u WHERE u.id::text = current_setting('app.user_id', true)
      AND u.tenant = documents.tenant AND u.active
      AND (documents.required_role = 'employee' OR EXISTS (
        SELECT 1 FROM memberships m JOIN groups g ON g.id = m.group_id AND g.tenant = m.tenant
        WHERE m.user_id = u.id AND m.tenant = u.tenant AND g.name = 'Managers'
      ))
    )
);
GRANT USAGE ON SCHEMA public TO bridge_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON users, groups, memberships, sessions TO bridge_app;
GRANT SELECT ON documents TO bridge_app;
