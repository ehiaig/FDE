import json
import os
import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlparse

from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import Flask, abort, g, jsonify, redirect, render_template, request, session
from werkzeug.exceptions import HTTPException
from .db import check_runtime_role, transaction
from .ai import generate

DEMO_USERS = {
    'alice': ('acme', 'demo|alice'), 'morgan': ('acme', 'demo|morgan'),
    'sam': ('globex', 'demo|sam'), 'riley': ('globex', 'demo|riley'),
}


def create_app(overrides=None):
    load_dotenv()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv('SESSION_SECRET', ''), DATABASE_URL=os.getenv('DATABASE_URL', ''),
        AUTH_MODE=os.getenv('AUTH_MODE', 'auth0'), APP_ENV=os.getenv('APP_ENV', 'development'),
        BASE_URL=os.getenv('BASE_URL', 'http://localhost:5002').rstrip('/'),
        AUTH0_DOMAIN=os.getenv('AUTH0_DOMAIN', ''), AUTH0_CLIENT_ID=os.getenv('AUTH0_CLIENT_ID', ''),
        AUTH0_CLIENT_SECRET=os.getenv('AUTH0_CLIENT_SECRET', ''),
        TENANTS=json.loads(os.getenv('TENANTS_JSON', '{"acme":{"name":"Acme"},"globex":{"name":"Globex"}}')),
        SCIM_TOKENS=json.loads(os.getenv('SCIM_TOKENS_JSON', '{}')),
        AI_PROVIDER=os.getenv('AI_PROVIDER', 'extractive'),
        OLLAMA_URL=os.getenv('OLLAMA_URL', 'http://localhost:11434'), OLLAMA_MODEL=os.getenv('OLLAMA_MODEL', 'llama3.2:3b'),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8), MAX_CONTENT_LENGTH=65536,
    )
    if overrides:
        app.config.update(overrides)
    cfg = app.config
    if len(cfg['SECRET_KEY']) < 32:
        raise RuntimeError('Set SESSION_SECRET to at least 32 random characters.')
    if cfg['AUTH_MODE'] not in ('demo', 'auth0') or cfg['AI_PROVIDER'] not in ('extractive', 'ollama'):
        raise RuntimeError('Invalid AUTH_MODE or AI_PROVIDER.')
    base = urlparse(cfg['BASE_URL'])
    if base.scheme not in ('http', 'https') or not base.hostname or base.path or base.query or base.fragment or base.username:
        raise RuntimeError('BASE_URL must be an http(s) origin.')
    if cfg['APP_ENV'] != 'development' and (cfg['AUTH_MODE'] == 'demo' or base.scheme != 'https'):
        raise RuntimeError('Production requires Auth0 and HTTPS.')
    if cfg['AUTH_MODE'] == 'demo' and base.hostname not in ('localhost', '127.0.0.1'):
        raise RuntimeError('Demo mode is restricted to a loopback BASE_URL.')
    cfg['SESSION_COOKIE_SECURE'] = base.scheme == 'https'
    cfg['TRUSTED_HOSTS'] = [base.hostname]
    if any(t not in cfg['TENANTS'] or len(token) < 32 for t, token in cfg['SCIM_TOKENS'].items()):
        raise RuntimeError('SCIM credentials require known tenants and at least 32 characters.')
    if len(set(cfg['SCIM_TOKENS'].values())) != len(cfg['SCIM_TOKENS']):
        raise RuntimeError('SCIM credentials must be distinct per tenant.')
    check_runtime_role(cfg['DATABASE_URL'])
    oauth = OAuth(app)
    if cfg['AUTH_MODE'] == 'auth0':
        if not re.fullmatch(r'[a-zA-Z0-9.-]+', cfg['AUTH0_DOMAIN']) or not cfg['AUTH0_CLIENT_ID'] or not cfg['AUTH0_CLIENT_SECRET']:
            raise RuntimeError('Configure AUTH0_DOMAIN, AUTH0_CLIENT_ID and AUTH0_CLIENT_SECRET.')
        orgs = [t.get('org_id') for t in cfg['TENANTS'].values()]
        if not all(orgs) or len(set(orgs)) != len(orgs):
            raise RuntimeError('Every tenant needs a distinct Auth0 org_id.')
        oauth.register('auth0', client_id=cfg['AUTH0_CLIENT_ID'], client_secret=cfg['AUTH0_CLIENT_SECRET'],
            server_metadata_url=f"https://{cfg['AUTH0_DOMAIN']}/.well-known/openid-configuration",
            client_kwargs={'scope': 'openid profile email', 'code_challenge_method': 'S256'})
    app.extensions['bridge_oauth'] = oauth

    @app.before_request
    def protect():
        g.user = None
        if request.path.startswith('/scim/v2/'):
            return  # SCIM has its own tenant-bound bearer authentication.
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            expected = session.get('csrf', '')
            supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf', '')
            if not expected or not secrets.compare_digest(expected, supplied):
                abort(403, 'Invalid CSRF token.')
        if session.get('sid') and session.get('tenant') in cfg['TENANTS']:
            with transaction(session['tenant']) as conn:
                g.user = conn.execute('SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id AND u.tenant=s.tenant WHERE s.id=%s AND s.expires_at>now() AND u.active', (session['sid'],)).fetchone()
            if g.user is None:
                session.clear()
        if request.path.startswith('/api/') and not g.user:
            abort(401, 'Sign in to continue.')

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if cfg['SESSION_COOKIE_SECURE']:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    def establish(tenant, external_id):
        with transaction(tenant) as conn:
            user = conn.execute('SELECT * FROM users WHERE external_id=%s AND active', (external_id,)).fetchone()
            if not user:
                abort(403, 'Your account must be provisioned and active before sign-in.')
            sid = secrets.token_urlsafe(32)
            conn.execute('INSERT INTO sessions(id,tenant,user_id) VALUES (%s,%s,%s)', (sid, tenant, user['id']))
        session.clear()
        session.update(sid=sid, tenant=tenant, csrf=secrets.token_urlsafe(32))
        session.permanent = True

    @app.get('/')
    def index():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        return render_template('index.html', user=g.user, csrf=session['csrf'], tenants=cfg['TENANTS'], demo=cfg['AUTH_MODE']=='demo', ai=cfg['AI_PROVIDER'])

    @app.post('/demo/login')
    def demo_login():
        if cfg['AUTH_MODE'] != 'demo':
            abort(404)
        if request.form.get('persona') not in DEMO_USERS:
            abort(400, 'Unknown demo persona.')
        establish(*DEMO_USERS[request.form['persona']])
        return redirect('/')

    @app.get('/login/<tenant>')
    def login(tenant):
        if cfg['AUTH_MODE'] != 'auth0' or tenant not in cfg['TENANTS']:
            abort(404)
        session.clear()
        session['login_tenant'] = tenant
        params = {'organization': cfg['TENANTS'][tenant]['org_id']}
        if cfg['TENANTS'][tenant].get('connection'):
            params['connection'] = cfg['TENANTS'][tenant]['connection']
        return oauth.auth0.authorize_redirect(cfg['BASE_URL'] + '/callback', **params)

    @app.get('/callback')
    def callback():
        if cfg['AUTH_MODE'] != 'auth0':
            abort(404)
        tenant = session.get('login_tenant')
        if tenant not in cfg['TENANTS']:
            abort(400, 'No pending sign-in.')
        try:
            # Authlib checks state, nonce, issuer, signature, audience and expiry.
            token = oauth.auth0.authorize_access_token()
            claims = token['userinfo']
            if claims.get('org_id') != cfg['TENANTS'][tenant]['org_id'] or not claims.get('sub'):
                raise ValueError('Organization mismatch')
        except Exception:
            session.clear()
            abort(401, 'Sign-in could not be validated. Please try again.')
        establish(tenant, claims['sub'])
        return redirect('/')

    @app.post('/logout')
    def logout():
        if session.get('tenant') and session.get('sid'):
            with transaction(session['tenant']) as conn:
                conn.execute('DELETE FROM sessions WHERE id=%s', (session['sid'],))
        session.clear()
        if cfg['AUTH_MODE'] == 'auth0':
            return redirect(f"https://{cfg['AUTH0_DOMAIN']}/v2/logout?" + urlencode({'client_id':cfg['AUTH0_CLIENT_ID'], 'returnTo':cfg['BASE_URL']}))
        return redirect('/')

    @app.get('/api/me')
    def me():
        with transaction(g.user['tenant']) as conn:
            manager = conn.execute("SELECT 1 FROM memberships m JOIN groups g ON g.id=m.group_id AND g.tenant=m.tenant WHERE m.user_id=%s AND g.name='Managers'", (g.user['id'],)).fetchone()
        return jsonify(username=g.user['username'], tenant=g.user['tenant'], role='manager' if manager else 'employee')

    @app.post('/api/query')
    def query():
        body = request.get_json(silent=True)
        question = body.get('question') if isinstance(body, dict) else None
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 1000:
            abort(400, 'Question must contain 1–1000 characters.')
        with transaction(g.user['tenant'], g.user['id']) as conn:
            # Explicit application authorization, plus database RLS independently.
            documents = conn.execute("""SELECT d.id, d.title, d.body, d.required_role FROM documents d
                WHERE d.tenant=%s AND (d.required_role='employee' OR EXISTS (
                    SELECT 1 FROM memberships m JOIN groups g ON g.id=m.group_id AND g.tenant=m.tenant
                    WHERE m.user_id=%s AND m.tenant=d.tenant AND g.name='Managers'))
                AND (to_tsvector('english', d.title || ' ' || d.body) @@ plainto_tsquery('english', %s))
                ORDER BY d.title LIMIT 8""", (g.user['tenant'],g.user['id'],question)).fetchall()
        try:
            answer = generate(question, documents, cfg)
        except Exception:
            abort(502, 'The model is unavailable. Your access permissions have not changed.')
        # Discard an in-flight answer if SCIM revoked the account, group, or session.
        with transaction(g.user['tenant'], g.user['id']) as conn:
            live = conn.execute('SELECT 1 FROM sessions s JOIN users u ON u.id=s.user_id AND u.tenant=s.tenant WHERE s.id=%s AND s.expires_at>now() AND u.active', (session['sid'],)).fetchone()
            visible = conn.execute('SELECT id FROM documents WHERE id=ANY(%s::uuid[])', ([d['id'] for d in documents],)).fetchall()
            if not live or {d['id'] for d in visible} != {d['id'] for d in documents}:
                abort(403, 'Permissions changed during this query. Please sign in again or retry.')
        return jsonify(**answer, sources=[{'id':str(d['id']), 'title':d['title'], 'body':d['body'], 'role':d['required_role']} for d in documents])

    from .scim import bp
    app.register_blueprint(bp)
    return app
