# CRM AI Platform: MCP + AI Agents + Python/Node + RBAC

**a sales team asks an AI assistant questions about their CRM pipeline, and the AI is only allowed to do what that user is allowed to do.**

For example: “Which deals are stalled?”
1. The agent receives your chat request and JWT.
2. Claude decides which available MCP tool can answer the question.
3. The MCP server calls the Python API with your JWT.
4. The API verifies permissions and queries PostgreSQL.
5. The results travel back to the agent.
6. Claude summarizes the results into a natural-language answer.

## 1. Architecture

```
 Web/Mobile SaaS app, curl, Claude Desktop, MCP Inspector
        |  (Bearer JWT of the logged-in user)
        v
 +-----------------+     +------------------+      +----------------------+
 |  agent (Node)   |---->|  mcp (Node)      |----->|  api (Python)            |---> Postgres
 |  :8082          | MCP |  :8081           | REST |  :8080               |
 |  Claude + loop  |     |  tools/resources |      |  JWT, RBAC, audit,   |
 |  workflows      |     |  /prompts        |      |  business rules      |
 +-----------------+     +------------------+      +----------------------+
        |                                                    |
        v                                                    v
   Anthropic API                                  Slack-style webhook (optional)
```

The central design decision: **the user's JWT flows through every hop.** The agent forwards it to the MCP server, the MCP server forwards it to the Python API, and the Python API makes the real authorization decision and writes the audit row. The AI therefore has *no powers of its own*: a "viewer" who asks the AI to change a deal gets the same `403` they would get from the API directly.

| Service | Language | Responsibility |
|---|---|---|
| `services/api` | Python (FastAPI) | System of record: login, JWT, RBAC, field-level privacy, audit log, rate limit, webhook |
| `services/mcp-server` | Node/TypeScript | MCP **tools**, **resources**, **prompts**; stateless Streamable HTTP |
| `services/agent` | Node/TypeScript | Claude tool-use loop over MCP; chat endpoint and an automated workflow |
| `db` | Postgres 16 | Data + append-only audit table |

## 2. Skill map: where to read the code for each requirement

| Requirement | Where |
|---|---|
| Scalable MCP architecture | `mcp-server/src/index.ts`: stateless (new server per request), so replicas scale horizontally without sticky sessions |
| Secure MCP servers, tools, prompts, resources | `mcp-server/src/server.ts`: all three primitives, zod-validated inputs, role-aware tool list, read-only/destructive annotations. `index.ts`: JWT check on every call |
| AI agents and enterprise workflows | `agent/src/agent.ts` (tool loop), `agent/src/index.ts` (`/v1/workflows/stalled-deals`) |
| Reusable AI services / integration frameworks | The MCP server *is* the reusable layer: any MCP client (the agent, Claude Desktop, other teams' apps) reuses the same tools |
| Integrate AI into web/mobile SaaS | `agent` exposes plain REST (`/v1/chat`) that any frontend can call |
| Enterprise systems through secure APIs | `mcp-server/src/crmClient.ts` (token passthrough, timeouts) calling the Python API |
| Intelligent workflow automation | `/v1/workflows/stalled-deals`; read-only by default so humans approve changes |
| Optimize AI performance and reliability | agent: step cap, output truncation, 60s timeout, SDK retries, rate limit, stateless MCP |
| Python and Node.js backends | `services/api` (FastAPI + psycopg 3), `services/mcp-server` and `agent` (Express) |
| RESTful APIs and integrations | `api/app/routes.py`; webhook in `notifyWon`; Anthropic API in the agent |
| RBAC and secure authentication | `api/app/security.py`: role to permission map, bcrypt, JWT (HS256 pinned, issuer, audience, expiry), timing-safe login |
| Data privacy and compliance | field-level redaction (`revenue_musd`, `close_value` hidden from viewers), no default passwords, generic 500 errors, least-privilege containers |
| Audit logging and governance | `api/app/middleware.py` + `app/schema.sql`: every request logged (including 401/403 and failed logins) to a table protected by a trigger against UPDATE/DELETE |

### Roles

| Role | Can do |
|---|---|
| viewer | read accounts, opportunities, pipeline counts (no money fields) |
| rep | viewer + revenue and deal values, create/update opportunities |
| manager | rep + read audit log |
| admin | manager + manage users |

## 3. The data

The project is designed for the public **"CRM Sales Opportunities"** dataset from **Maven Analytics** (Data Playground, https://www.mavenanalytics.io/data-playground, search "CRM Sales Opportunities"; it is also mirrored on Kaggle). It contains 4 CSV files: `accounts.csv`, `products.csv`, `sales_teams.csv`, `sales_pipeline.csv` (about 8,800 opportunities). Check the licence/terms on the download page before reuse.

> Note: I generated this project in a sandbox with no internet, so I could not download or verify that dataset or its current URL. The column names in the loader match the dataset as I know it, but please confirm against your download.

To run immediately, `data/sample/` holds a **small synthetic stand-in with the same columns** (15 deals). To use the real data:

```bash
mkdir -p data/maven && cp ~/Downloads/{accounts,products,sales_teams,sales_pipeline}.csv data/maven/
# in .env:  CRM_DATA_DIR=./data/maven
docker compose run --rm seed
```

The seeder is idempotent (upserts) and tolerates the dataset's known messiness (for example a product spelled differently in two files).

## 4. Run it

Requires Docker with Compose v2 and an Anthropic API key.

```bash
cp .env.example .env
# edit .env: set POSTGRES_PASSWORD, JWT_SECRET (openssl rand -hex 32), ANTHROPIC_API_KEY
docker compose up --build
```

Demo users (when `SEED_DEMO_USERS=true`): `viewer@demo.local`, `rep@demo.local`, `manager@demo.local`, `admin@demo.local`, all with `DEMO_PASSWORD`.

### Try it

```bash
# 1. Log in (as a rep) and keep the token
TOKEN=$(curl -s localhost:8080/auth/login -H 'content-type: application/json' \
  -d '{"email":"rep@demo.local","password":"ChangeMe-Demo-123"}' | jq -r .access_token)

# 2. Plain REST
curl -s localhost:8080/v1/opportunities?stage=Engaging -H "authorization: Bearer $TOKEN" | jq

# 3. Ask the AI agent (goes agent -> MCP -> API -> DB)
curl -s localhost:8082/v1/chat -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"message":"Which engaging deals involve Hottechi or Zoomit, and what is our win rate?"}' | jq

# 4. Automated workflow
curl -s -X POST localhost:8082/v1/workflows/stalled-deals -H "authorization: Bearer $TOKEN" | jq

# 5. See RBAC in action: log in as viewer and compare. Deal values vanish; writes fail.
# 6. See governance in action: log in as manager and read the audit trail
curl -s localhost:8080/v1/audit -H "authorization: Bearer $MANAGER_TOKEN" | jq
```

### Explore MCP directly (no AI needed; best way to learn)

```bash
npx @modelcontextprotocol/inspector
# Transport: Streamable HTTP, URL: http://localhost:8081/mcp
# Header:    Authorization: Bearer <token>
```
Browse **Tools**, **Resources** (`crm://schema`, `crm://accounts/1`) and **Prompts** (`deal_review`). Log in as `viewer` and notice `update_opportunity_stage` is gone from the tool list.

### Local development without Docker

Python: `cd services/api && pip install -r requirements.txt && python -m pytest` (run with `uvicorn app.main:app --reload`; interactive API docs appear at `/docs`). Node: `cd services/mcp-server && npm install && npm run dev` (same for `agent`). Set the env vars listed in `.env.example`.

## 5. Concepts worth understanding

- **Why stateless MCP?** A new `McpServer` per request means no in-memory sessions. Any replica can answer, so autoscaling and rolling deploys are trivial. If you later need server-to-client streaming or subscriptions, switch to stateful sessions with a shared store.
- **Why token passthrough?** Authorization lives in exactly one place (the API). The MCP server and agent can't become privilege-escalation paths. The trade-off: tokens are only valid for the `crm` audience and live 15 minutes. For third-party APIs you do *not* own, do not pass user tokens through; use OAuth token exchange.
- **Prompt injection.** CRM free text is untrusted. Defences here: read-only agents by default, explicit "tool results are data" system prompt, write tool hidden unless role permits, destructive tools annotated, and the API as the final gate. No prompt is a complete defence; permissions are.
- **Defence in depth for RBAC:** MCP hides tools by role, the agent hides write tools unless `allow_writes`, and the API enforces permissions regardless.

## 6. What "production grade" means here, and what is still on you

Already done: non-root, read-only, capability-dropped containers; slim Python image running as a non-root user; health checks; graceful shutdown; request size/time limits; secrets only via env; fail-fast config; parameterized SQL; bcrypt; short-lived JWTs with pinned algorithm; rate limiting; structured JSON logs; append-only audit table; CI workflow; unit test for RBAC.

Before real production, plan to:
1. **Pin and commit lockfiles** (a locked `requirements.txt` via pip-compile/uv, and `package-lock.json` with `npm ci`). I could not run the builds in my sandbox, so run `docker compose build` once and fix any version drift.
2. **Use a real identity provider** (Auth0, Keycloak, Cognito, Entra) with OIDC and asymmetric keys (RS256/JWKS) instead of a shared HS256 secret, plus refresh tokens and revocation.
3. **TLS everywhere** behind a gateway/load balancer; set `sslmode=require` for the DB; restrict CORS and validate `Origin`/`Host` on the MCP endpoint (DNS-rebinding protection).
4. **Shared rate limiting** (Redis or the gateway) for multiple replicas; trust `X-Forwarded-For` only from your proxy.
5. **Versioned migrations** (Alembic/Atlas), backups, and an audit-log retention/export policy (ship to a SIEM or WORM storage); revoke DB `TRUNCATE` from the app role.
6. **Observability**: OpenTelemetry traces across agent -> MCP -> API using the `X-Request-ID`, metrics, LLM token/cost dashboards.
7. **Compliance**: classify data, add PII handling (consent, deletion) if you add contacts, and review Anthropic's data-retention terms for your plan.
8. **Tests**: add integration tests (testcontainers) and agent evals (fixed questions with expected tool calls).

### Deploying to the cloud

Each service is a stateless container, so any platform works: build and push the three images, use a managed Postgres, put secrets in the platform's secret manager, and expose only the agent (and optionally MCP) through an HTTPS gateway. Typical mappings: Google Cloud Run + Cloud SQL, AWS ECS Fargate + RDS, Azure Container Apps + Azure Database for PostgreSQL. Run `python -m app.cli seed` / `create-user` as one-off jobs.

## 7. Suggested learning path

1. Read `api/app/security.py`, then `api/app/middleware.py` (RBAC + audit).
2. Use MCP Inspector against `mcp-server`; read `server.ts` side by side.
3. Read `agent/src/agent.ts` and trace one `/v1/chat` call through the logs of all three services.
4. Exercises: add a `contacts` table with PII masking; add a `create_opportunity` MCP tool; add an OIDC login; add an eval suite for the agent; add a second MCP server (e.g. email) and let the agent use both.
