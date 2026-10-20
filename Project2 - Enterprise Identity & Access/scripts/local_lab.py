"""Initialize an isolated local PostgreSQL lab; never touches an existing cluster."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'


def main():
    os.chdir(ROOT)
    if (ROOT / '.env').exists() or (RUNTIME / 'pgdata').exists():
        sys.exit('Existing .env or lab cluster found. See README for restart instructions; no files changed.')
    pg_bin = os.getenv('PG_BIN')
    if not pg_bin:
        candidates = [Path('/opt/homebrew/opt/postgresql@14/bin'), Path('/opt/homebrew/opt/postgresql@16/bin')]
        pg_bin = next((str(p) for p in candidates if (p / 'initdb').exists()), None)
    if not pg_bin and shutil.which('initdb'):
        pg_bin = str(Path(shutil.which('initdb')).parent)
    if not pg_bin:
        sys.exit('Set PG_BIN to your PostgreSQL bin directory, or follow the Docker setup in README.')
    port = int(os.getenv('PG_PORT', '55432'))
    RUNTIME.mkdir(exist_ok=True, mode=0o700)
    admin_password, app_password = secrets.token_hex(32), secrets.token_hex(32)
    pwfile = RUNTIME / 'bootstrap-password'
    pwfile.write_text(admin_password)
    pwfile.chmod(0o600)
    try:
        subprocess.run([pg_bin+'/initdb','-D',str(RUNTIME/'pgdata'),'-U','postgres','--auth=scram-sha-256','--pwfile='+str(pwfile),'--no-locale','-E','UTF8'], check=True)
    finally:
        pwfile.unlink(missing_ok=True)
    subprocess.run([pg_bin+'/pg_ctl','-D',str(RUNTIME/'pgdata'),'-l',str(RUNTIME/'postgres.log'),'-o',f"-h 127.0.0.1 -p {port} -k ''",'-w','start'],check=True)
    admin_url = f'postgresql://postgres:{admin_password}@127.0.0.1:{port}/postgres'
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute('CREATE DATABASE bridge')
    with psycopg.connect(admin_url.rsplit('/',1)[0]+'/bridge') as conn:
        conn.execute((ROOT/'sql/001_schema.sql').read_text())
        conn.execute(sql.SQL('ALTER ROLE bridge_app PASSWORD {}').format(sql.Literal(app_password)))
        conn.execute((ROOT/'sql/002_demo_seed.sql').read_text())
    values = {
        'APP_ENV':'development','AUTH_MODE':'demo','BASE_URL':'http://localhost:5002',
        'SESSION_SECRET':secrets.token_hex(32),
        'DATABASE_URL':f'postgresql://bridge_app:{app_password}@127.0.0.1:{port}/bridge',
        'TENANTS_JSON':json.dumps({'acme':{'name':'Acme'},'globex':{'name':'Globex'}}),
        'SCIM_TOKENS_JSON':json.dumps({t:secrets.token_hex(32) for t in ('acme','globex')}),
        'AI_PROVIDER':'extractive',
    }
    env = ROOT/'.env'
    env.write_text('\n'.join(f'{key}={value}' for key,value in values.items())+'\n')
    env.chmod(0o600)
    admin_file=RUNTIME/'test-admin-url'
    admin_file.write_text(admin_url)
    admin_file.chmod(0o600)
    print('Local lab ready. Secrets are stored in ignored local files, not printed.')
    print('Run: .venv/bin/flask --app bridge:create_app run --host 127.0.0.1 --port 5002')


if __name__ == '__main__':
    main()
