"""Every operation has its own transaction; tenant context cannot leak via pooling."""
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row
from flask import current_app


@contextmanager
def transaction(tenant, user_id=None):
    with psycopg.connect(current_app.config['DATABASE_URL'], row_factory=dict_row) as conn:
        conn.execute("SELECT set_config('app.tenant', %s, true)", (tenant,))
        conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id or ''),))
        yield conn


def check_runtime_role(url):
    with psycopg.connect(url, row_factory=dict_row) as conn:
        role = conn.execute('SELECT current_user AS name, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
        if role['name'] != 'bridge_app' or role['rolsuper'] or role['rolbypassrls']:
            raise RuntimeError('DATABASE_URL must use the restricted bridge_app role.')
        count = conn.execute("SELECT count(*) AS n FROM pg_class WHERE oid IN ('users'::regclass, 'groups'::regclass, 'memberships'::regclass, 'sessions'::regclass, 'documents'::regclass) AND relrowsecurity AND relforcerowsecurity").fetchone()['n']
        if count != 5:
            raise RuntimeError('All five application tables must enforce RLS.')
