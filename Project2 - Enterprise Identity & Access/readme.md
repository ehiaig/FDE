# Project2 — Enterprise SSO & RBAC Bridge

Goal: demonstrate OAuth2/OIDC, SAML federation, SCIM provisioning, PostgreSQL row-level security (RLS), and isolation between customers. A small knowledge application is included, so no existing AI app or stack is required.

The implementation follows five stages: a query app with fictional data; Auth0 sign-in; roles and database isolation; SCIM lifecycle management; optional local AI answers. All stages have code. Live Auth0/SAML and a real model require the external configuration described below.

## Run the local lab

Python 3.12+ and PostgreSQL 14+ are required. Run commands from this directory:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/python scripts/local_lab.py
.venv/bin/flask --app bridge:create_app run --host 127.0.0.1 --port 5002
```

Open <http://localhost:5002>. The bootstrap script detects Homebrew PostgreSQL 14/16 or `initdb` on PATH; otherwise set `PG_BIN` to the PostgreSQL bin directory. It creates a separate database cluster under `.runtime`, listens only on `127.0.0.1:55432`, and generates passwords, session signing secrets, and distinct customer provisioning tokens. Secrets are saved in ignored, owner-readable files. It refuses to overwrite an existing `.env` or cluster. This is a local development server.

To restart the already initialized database on this machine:

```sh
/opt/homebrew/opt/postgresql@14/bin/pg_ctl -D .runtime/pgdata -l .runtime/postgres.log -w start
```

To stop it, use the same `pg_ctl` with `-D .runtime/pgdata -m fast -w stop`. Stop Flask separately with Ctrl+C. Do not run bootstrap again to restart.

### Docker alternative

Instead of the local bootstrap, copy `.env.example` to `.env`, generate a session secret and a separate database owner password, and set `POSTGRES_PASSWORD` in your shell or `.env`. Run `docker compose up -d --wait`. The SQL files initialize a fresh volume only. Set the web role's password interactively:

```sh
docker compose exec db psql -U postgres -d bridge
```

At the psql prompt run `\password bridge_app`, then `\q`. Put that password into `DATABASE_URL` in `.env` (URL-encode special characters, or use a random hex password). The web app must use `bridge_app`, never `postgres`. Start Flask with the command above. Keep the database volume; initialization SQL is not a migration system and should not be reapplied to a populated database.

## 1. Try the permissions scenario

Demo mode is explicitly labeled, disabled in production, and limited to a loopback base URL. It is a local simulation, not authentication. The four personas are:

| User | Company | Role | Query `leave` | Query `budget` |
| --- | --- | --- | --- | --- |
| Alice | Acme | Employee | Acme's 25 days | No result |
| Morgan | Acme | Manager | Acme's 25 days | Acme's £120,000 |
| Sam | Globex | Employee | Globex's 30 days | No result |
| Riley | Globex | Manager | Globex's 30 days | Globex's £80,000 |

Sign out and repeat the same query with another persona. Employee access is the baseline for active provisioned users. Membership in the exact, case-sensitive `Managers` group adds management access within that user's tenant. Other group names grant no extra permissions. Tenant identity and permissions are never accepted from the query body.

Retrieval uses PostgreSQL English full-text search (all search terms must match), not semantic embeddings. Start with `leave` and `budget`. In the default `extractive` mode the UI shows original matching excerpts and sources; it does not pretend to generate an AI answer.

## 2. Connect Auth0 with OIDC

Auth0 is the application's identity provider and the federation broker for customer identity providers. The application uses Authlib with the authorization-code flow and PKCE; it validates signed ID tokens and requires the organization selected at sign-in to match `org_id`. OAuth2 access tokens are not accepted as application login cookies or SCIM credentials.

1. Create an Auth0 **Regular Web Application**. Set Allowed Callback URLs to `http://localhost:5002/callback` and Allowed Logout URLs to `http://localhost:5002`.
2. Configure the application for organization-based login. Create two Auth0 Organizations: Acme and Globex. Give each organization an enabled login connection, and assign test users to the correct organization. The required Organizations/enterprise features must be available on your account.
3. Edit the ignored `.env` file locally. Set `AUTH_MODE=auth0`, `AUTH0_DOMAIN` (hostname only), `AUTH0_CLIENT_ID`, and `AUTH0_CLIENT_SECRET`. Preserve your random `SESSION_SECRET` and restricted `DATABASE_URL`.
4. Set `TENANTS_JSON` using your real organization IDs:

```dotenv
TENANTS_JSON={"acme":{"name":"Acme","org_id":"org_REAL_ACME"},"globex":{"name":"Globex","org_id":"org_REAL_GLOBEX"}}
```

5. Provision the same users into this app with the exact Auth0 `user_id` as SCIM `externalId`. Copy the `user_id` from the Auth0 dashboard. Run this while the application is running:

```sh
.venv/bin/python scripts/provision.py --tenant acme create-user --username alice@your-company.example --subject 'auth0|YOUR_ACTUAL_USER_ID'
.venv/bin/python scripts/provision.py --tenant acme list
```

Use the actual subject value, including its real prefix; enterprise identities often have a different prefix from `auth0|`. Creating a local SCIM user does not create an Auth0 account or add Auth0 Organization membership. Both sides must be set up. Email is only a display/login name here, never a trusted account-linking key.

6. Restart Flask, choose the company, and complete Auth0 login. Unprovisioned/inactive users receive 403. Provision a manager with the command in stage 4.

The four seeded `demo|…` identities cannot sign in through Auth0. No Auth0 tenant or remote application has been created automatically, and no credentials belong in Git or chat.

### Add the SAML customer path

In Auth0 create a SAML Enterprise Connection for a test Okta/Entra ID customer. Exchange metadata, signing certificate, entity ID, and callback/ACS URL according to Auth0's connection setup. Enable the connection for this application and the matching Organization. Add its name to that customer's config:

```dotenv
TENANTS_JSON={"acme":{"name":"Acme","org_id":"org_REAL_ACME","connection":"acme-saml"},"globex":{"name":"Globex","org_id":"org_REAL_GLOBEX","connection":"globex-saml"}}
```

The browser flow is **app → Auth0 → customer SAML IdP → Auth0 → app OIDC callback**. Auth0 validates SAML; this project does not implement an XML/SAML parser. Keep login service-provider initiated. Local logout invalidates the app session and redirects to Auth0 logout; customer IdP-wide single logout requires separate IdP configuration and is not assumed.

## 3. Understand the authorization boundary

- The signed browser cookie contains an opaque session identifier and tenant, never bearer/ID tokens. Sessions expire after eight hours in PostgreSQL and are deleted on logout.
- Each protected request joins its server session to a current, active directory user. Roles come from current group membership, not stale cookie/token role claims.
- Every database operation sets transaction-local tenant context. Document operations also set the provisioned user's UUID. No context persists after the transaction ends.
- Every table has enabled and forced RLS. Documents additionally check the active user and live `Managers` membership. Directory and session tables have tenant policies, and composite foreign keys prevent cross-tenant memberships.
- The restricted web database role cannot write documents and is checked at startup. Schema/seed operations use a separate owner connection.
- The application filters retrieval explicitly; database policy independently rejects unfiltered cross-tenant/role reads. Missing context returns no rows.
- The model receives only authorized excerpts. After generation, the app rechecks the session and all returned document permissions; an in-flight revocation discards the answer. Already disclosed data cannot be recalled.

RLS is protection against omitted query filters, not a sandbox against a fully compromised application server or arbitrary SQL execution: the trusted backend controls transaction settings. The directory-writing credential is limited by tenant policies, but the provisioning application itself is trusted. Use a separate provisioning service/DB role, audit events, rate limiting, managed secrets, migration tooling, backups, and deployment controls before operating this as a real enterprise service.

## 4. Provision users and groups with SCIM

The customer simulator talks directly to **this app's** `/scim/v2` endpoints. Each customer's random bearer token maps to exactly one configured tenant. A client cannot select a tenant in its request body or URL. Keep tokens in `.env` under `SCIM_TOKENS_JSON`; rotate a token by replacing it and restarting the app.

```sh
.venv/bin/python scripts/provision.py --tenant acme list
.venv/bin/python scripts/provision.py --tenant acme promote --user-id 10000000-0000-0000-0000-000000000002
.venv/bin/python scripts/provision.py --tenant acme demote --user-id 10000000-0000-0000-0000-000000000002
.venv/bin/python scripts/provision.py --tenant acme deactivate --user-id 10000000-0000-0000-0000-000000000002
.venv/bin/python scripts/provision.py --tenant acme activate --user-id 10000000-0000-0000-0000-000000000002
```

These IDs refer to seeded Morgan. For a real Auth0 user, use the app's UUID from the create/list response. While signed in as Morgan, demote and retry `budget`: it disappears without a new login. Deactivate and retry: the session is denied. Reactivating does not revive the old session; a new sign-in is required.

Supported SCIM subset:

- `/Users` and `/Groups`: list, read, create, replace, patch, delete.
- User `userName`, `externalId`, and boolean `active`; group `displayName` and membership updates, including `members[value eq "UUID"]` removal. Read-only user `groups` are computed from membership.
- Equality filters on `userName`/`externalId` (users) and `displayName` (groups), plus `startIndex` and capped `count` pagination.
- `/ServiceProviderConfig` and `/ResourceTypes` discovery. Bearer authentication, transactional patches, tenant isolation, and SCIM error envelopes.

This is a bounded teaching implementation, not a certified universal SCIM server. `/Schemas`, extended profile attributes, complex filters, bulk operations, ETags, sorting, attribute selection, and arbitrary PATCH paths are not implemented. Configure a customer connector for this subset and test its actual payloads before integration. `externalId` is an explicit lab mapping to Auth0's immutable subject; a real IdP connector may need an identity mapping adapter if it supplies an upstream object ID instead.

Auth0 inbound SCIM is a separate path for provisioning Auth0's own directory. Enabling it alone does **not** update this application's database or revoke this app's sessions. Use the direct app SCIM path demonstrated here; an Auth0 event-stream-to-app synchronizer would be additional work.

## 5. Turn on local AI when ready

Install and start Ollama separately, then download a suitable model, for example:

```sh
ollama pull llama3.2:3b
```

Set these local `.env` values and restart Flask:

```dotenv
AI_PROVIDER=ollama
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2:3b
```

The app sends the question and only permitted matching excerpts to Ollama's `/api/chat`. Keep `OLLAMA_URL` pointed at a trusted service because it receives company excerpts. Model errors return 502 rather than relaxing permissions. Responses and source text are rendered as text, not executable HTML. Generated answers are not guaranteed factual; the displayed excerpts remain the evidence. No model installation/download is required for the default lab.

## Verify

```sh
.venv/bin/python -m pytest -q --tb=short
```

Tests use a newly created temporary database and remove it afterward; they do not reset the running demo. Local bootstrap stores the test owner's connection string in `.runtime/test-admin-url`. For Docker/another database, set `TEST_ADMIN_URL` to an owner connection with permission to create databases and set `DATABASE_URL` to the restricted app connection. The `bridge_app` role must already exist from initial setup.

The 28-test suite exercises real PostgreSQL RLS, four-persona access, forged tenant/role inputs, cross-tenant SCIM requests, membership lifecycle, transactional rollback, session revocation and cookie replay, CSRF, safe model input, in-flight revocation, and real RSA-signed OIDC tokens. OIDC discovery/JWKS/token-exchange transport is simulated; signature/state/nonce/issuer/audience/expiry/org checks run through Authlib. Real Auth0 login and upstream SAML interoperability remain external acceptance checks. A separate smoke check successfully generated an answer with the locally installed `llama3.2:3b` model using only Morgan's authorized Acme budget excerpt; the default lab still runs in extractive mode.

Implementation map: `bridge/__init__.py` contains web/OIDC/query routes; `bridge/db.py` database transactions; `bridge/scim.py` provisioning; `bridge/ai.py` the optional model adapter; `sql/` policies and fixtures; `scripts/` setup and the customer simulator; `tests/` integration checks.

## References

- [Auth0 Flask setup](https://auth0.com/docs/quickstart/webapp/python)
- [Auth0 Organizations and token validation](https://auth0.com/docs/manage-users/organizations/using-tokens)
- [Auth0 SAML enterprise connections](https://auth0.com/docs/authenticate/identity-providers/enterprise-identity-providers/saml)
- [Auth0 SCIM support](https://support.auth0.com/center/s/article/SCIM-support-at-Auth0)
- [PostgreSQL row security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)
- [SCIM protocol, RFC 7644](https://www.rfc-editor.org/rfc/rfc7644)
