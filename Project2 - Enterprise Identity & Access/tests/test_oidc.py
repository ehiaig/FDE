import time
from urllib.parse import urlparse, parse_qs

import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from bridge import create_app


@pytest.mark.parametrize('fault',[None,'state','nonce','issuer','audience','expiry','signature','organization','unprovisioned'])
def test_real_oidc_validation(app,monkeypatch,fault):
    config={k:app.config[k] for k in ('DATABASE_URL','SECRET_KEY','SCIM_TOKENS','BASE_URL','APP_ENV','AI_PROVIDER')}
    config.update(TESTING=True,AUTH_MODE='auth0',AUTH0_DOMAIN='example.auth0.com',AUTH0_CLIENT_ID='test-client',AUTH0_CLIENT_SECRET='test-client-secret',
        TENANTS={'acme':{'name':'Acme','org_id':'org_acme'},'globex':{'name':'Globex','org_id':'org_globex'}})
    real_app=create_app(config)
    remote=real_app.extensions['bridge_oauth'].auth0
    metadata={'issuer':'https://example.auth0.com/','authorization_endpoint':'https://example.auth0.com/authorize',
              'token_endpoint':'https://example.auth0.com/oauth/token','jwks_uri':'https://example.auth0.com/.well-known/jwks.json',
              'id_token_signing_alg_values_supported':['RS256']}
    monkeypatch.setattr(remote,'load_server_metadata',lambda:metadata)
    key=RSAKey.generate_key(2048,parameters={'kid':'test'})
    monkeypatch.setattr(remote,'fetch_jwk_set',lambda **kw:{'keys':[key.as_dict(private=False)]})
    client=real_app.test_client()
    response=client.get('/login/acme')
    assert response.status_code==302
    params=parse_qs(urlparse(response.location).query)
    assert params['organization']==['org_acme']
    assert params['code_challenge_method']==['S256']
    now=int(time.time())
    claims={'iss':metadata['issuer'],'sub':'demo|alice','aud':'test-client','iat':now,'exp':now+300,
            'nonce':params['nonce'][0],'org_id':'org_acme'}
    if fault=='nonce': claims['nonce']='wrong'
    if fault=='issuer': claims['iss']='https://attacker.example/'
    if fault=='audience': claims['aud']='other-client'
    if fault=='expiry': claims['exp']=now-3600
    if fault=='organization': claims['org_id']='org_globex'
    if fault=='unprovisioned': claims['sub']='auth0|unknown'
    signing_key=RSAKey.generate_key(2048,parameters={'kid':'test'}) if fault=='signature' else key
    encoded=jwt.encode({'alg':'RS256','kid':'test'},claims,signing_key)
    monkeypatch.setattr(remote,'fetch_access_token',lambda **kw:{'id_token':encoded,'access_token':'test-access-token','token_type':'Bearer'})
    state='wrong' if fault=='state' else params['state'][0]
    result=client.get('/callback',query_string={'code':'test-code','state':state})
    assert result.status_code==(302 if fault is None else 403 if fault=='unprovisioned' else 401)
    assert client.get('/api/me').status_code==(200 if fault is None else 401)
    assert client.post('/demo/login').status_code in (403,404)
    if fault is None:
        assert client.get('/callback',query_string={'code':'test-code','state':state}).status_code==400


def test_production_rejects_demo(app):
    with pytest.raises(RuntimeError,match='Production requires'):
        create_app({'APP_ENV':'production','AUTH_MODE':'demo','SECRET_KEY':'a'*40})
