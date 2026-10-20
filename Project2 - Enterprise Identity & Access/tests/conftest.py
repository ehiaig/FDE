import os
from pathlib import Path
from uuid import uuid4
import pytest
import psycopg
from psycopg import sql
from dotenv import dotenv_values
from bridge import create_app

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture(scope='session')
def database():
    values=dotenv_values(ROOT/'.env')
    admin=os.getenv('TEST_ADMIN_URL')
    if not admin and (ROOT/'.runtime/test-admin-url').exists():
        admin=(ROOT/'.runtime/test-admin-url').read_text().strip()
    if not admin:
        pytest.fail('Set TEST_ADMIN_URL or run scripts/local_lab.py. Real PostgreSQL is required.')
    name='bridge_test_'+uuid4().hex[:12]
    with psycopg.connect(admin,autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    test_admin=psycopg.conninfo.make_conninfo(admin,dbname=name)
    runtime=psycopg.conninfo.make_conninfo(os.getenv('DATABASE_URL') or values['DATABASE_URL'],dbname=name)
    with psycopg.connect(test_admin) as conn:
        schema=(ROOT/'sql/001_schema.sql').read_text().replace('CREATE ROLE bridge_app LOGIN NOSUPERUSER NOBYPASSRLS;','')
        conn.execute(schema)
    yield test_admin,runtime
    with psycopg.connect(admin,autocommit=True) as conn:
        conn.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


@pytest.fixture
def app(database):
    admin,runtime=database
    with psycopg.connect(admin) as conn:
        conn.execute('TRUNCATE users,groups,memberships,sessions,documents CASCADE')
        conn.execute((ROOT/'sql/002_demo_seed.sql').read_text())
    return create_app({'TESTING':True,'DATABASE_URL':runtime,'SECRET_KEY':'test-secret-'*8,
        'AUTH_MODE':'demo','APP_ENV':'development','BASE_URL':'http://localhost',
        'TENANTS':{'acme':{'name':'Acme'},'globex':{'name':'Globex'}},
        'SCIM_TOKENS':{'acme':'a'*40,'globex':'g'*40},'AI_PROVIDER':'extractive'})


@pytest.fixture
def client(app):
    return app.test_client()


def login(client,persona):
    client.get('/')
    with client.session_transaction() as s:
        csrf=s['csrf']
    assert client.post('/demo/login',data={'persona':persona,'csrf':csrf}).status_code==302


def query(client,question='budget',**extra):
    with client.session_transaction() as s:
        csrf=s.get('csrf','')
    return client.post('/api/query',json={'question':question,**extra},headers={'X-CSRF-Token':csrf})


def scim(client,path='',method='get',tenant='acme',body=None):
    return getattr(client,method)('/scim/v2/'+path,headers={'Authorization':'Bearer '+('a'*40 if tenant=='acme' else 'g'*40)},json=body)
