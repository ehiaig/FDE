import pytest
import psycopg
from bridge.db import transaction, check_runtime_role
from conftest import login,query,scim

MORGAN='10000000-0000-0000-0000-000000000002'
GROUP='10000000-0000-0000-0000-000000000010'


@pytest.mark.parametrize('persona,tenant,role,amount',[('alice','acme','employee',None),('morgan','acme','manager','120,000'),('sam','globex','employee',None),('riley','globex','manager','80,000')])
def test_same_query_different_permissions(client,persona,tenant,role,amount):
    login(client,persona)
    assert client.get('/api/me').json['role']==role
    response=query(client,tenant='globex',role='manager')
    assert response.status_code==200
    sources=response.json['sources']
    assert len(sources)==(1 if amount else 0)
    if amount:
        assert amount in response.json['answer']
        assert tenant in sources[0]['title'].lower()
    leave=query(client,'leave').json
    assert len(leave['sources'])==1
    assert tenant in leave['sources'][0]['title'].lower()


def test_database_rls_without_application_filters(app):
    with app.app_context():
        with transaction('acme','10000000-0000-0000-0000-000000000001') as conn:
            docs=conn.execute('SELECT * FROM documents').fetchall()
            assert len(docs)==1 and docs[0]['tenant']=='acme' and docs[0]['required_role']=='employee'
            assert all(u['tenant']=='acme' for u in conn.execute('SELECT * FROM users'))
        with transaction('globex',MORGAN) as conn:
            assert conn.execute('SELECT * FROM documents').fetchall()==[]
        with psycopg.connect(app.config['DATABASE_URL']) as conn:
            assert conn.execute('SELECT * FROM documents').fetchall()==[]
            assert conn.execute('SELECT * FROM users').fetchall()==[]


def test_runtime_cannot_write_documents_or_other_tenant(app):
    with app.app_context():
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with transaction('acme') as conn:
                conn.execute("UPDATE documents SET body='compromised'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with transaction('acme') as conn:
                conn.execute("INSERT INTO users(id,tenant,external_id,username) VALUES (gen_random_uuid(),'globex','x','x')")


def test_owner_role_rejected(database):
    with pytest.raises(RuntimeError):
        check_runtime_role(database[0])


def test_auth_csrf_logout_replay(client):
    assert client.get('/api/me').status_code==401
    assert client.post('/demo/login',data={'persona':'morgan'}).status_code==403
    login(client,'morgan')
    assert client.post('/api/query',json={'question':'budget'}).status_code==403
    old_cookie=client.get_cookie('session').value
    with client.session_transaction() as s:
        csrf=s['csrf']
    assert client.post('/logout',data={'csrf':csrf}).status_code==302
    client.set_cookie('session',old_cookie)
    assert client.get('/api/me').status_code==401


def test_deactivation_revokes_session_permanently(client):
    login(client,'morgan')
    old_cookie=client.get_cookie('session').value
    assert scim(client,'Users/'+MORGAN,'patch',body={'Operations':[{'op':'replace','path':'active','value':False}]}).status_code==200
    assert query(client).status_code==401
    assert scim(client,'Users/'+MORGAN,'patch',body={'Operations':[{'op':'replace','path':'active','value':True}]}).status_code==200
    client.set_cookie('session',old_cookie)
    assert client.get('/api/me').status_code==401


def test_group_removal_applies_without_new_login(client):
    login(client,'morgan')
    assert len(query(client).json['sources'])==1
    response=scim(client,'Groups/'+GROUP,'patch',body={'Operations':[{'op':'remove','path':f'members[value eq "{MORGAN}"]'}]})
    assert response.status_code==200
    assert client.get('/api/me').json['role']=='employee'
    assert query(client).json['sources']==[]


def test_model_never_receives_hidden_documents(client,monkeypatch):
    seen=[]
    def fake(question,docs,config):
        seen.extend(docs)
        return {'mode':'test','answer':'test'}
    monkeypatch.setattr('bridge.generate',fake)
    login(client,'alice')
    assert query(client).status_code==200
    assert seen==[]
    query(client,'leave')
    assert len(seen)==1 and 'Acme' in seen[0]['title']


def test_revocation_during_generation_discards_answer(app,client,monkeypatch):
    admin_client=app.test_client()
    def fake(question,docs,config):
        # Simulate a concurrent request with its own application context.
        with app.app_context():
            assert scim(admin_client,'Users/'+MORGAN,'patch',body={'Operations':[{'op':'replace','path':'active','value':False}]}).status_code==200
        return {'mode':'test','answer':'secret output'}
    monkeypatch.setattr('bridge.generate',fake)
    login(client,'morgan')
    response=query(client)
    assert response.status_code==403
    assert 'secret output' not in response.text


def test_invalid_question_and_headers(client):
    login(client,'alice')
    assert query(client,' ').status_code==400
    assert query(client,'a'*1001).status_code==400
    assert "frame-ancestors 'none'" in client.get('/').headers['Content-Security-Policy']
