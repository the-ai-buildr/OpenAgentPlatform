# OpenAgentPlatform

A self-hosted agent platform: **Agno AgentOS** (FastAPI) behind **Open WebUI**,
with per-folder **pgvector** knowledge bases, automatic document indexing, an
**OpenAI-compatible bridge**, an **MCP** endpoint, and **OpenTelemetry** traces
in **Arize Phoenix**. Everything runs in Docker with one command.

```
Open WebUI  →  AgentOS /v1 bridge  →  Agno agents + team  →  OpenRouter (chat)
                       │                      │
                       │                      └──────────→  pgvector knowledge
                       └─ uploads → workspace_files/<folder> → auto-indexed
                                             │
                                             └──────────→  Phoenix (traces)
```

| | |
|---|---|
| **Agents** | `platform-builder` (creates agents and folders), `platform-engineer` (explains the codebase), `platform-manager` (health, routing, indexing) |
| **Team** | `agno` — routes a request to the right specialist |
| **Chat models** | Any model on [OpenRouter](https://openrouter.ai/models) |
| **Embeddings** | OpenAI or any OpenAI-compatible embeddings server (**not** OpenRouter — see [Why two providers](#why-two-providers)) |
| **Storage** | PostgreSQL 17 + pgvector 0.8.7 |
| **Tracing** | OpenTelemetry → Arize Phoenix |

Architecture notes and the review of the original stack: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## Contents

- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [Getting an OpenRouter key](#getting-an-openrouter-key)
- [Why two providers](#why-two-providers)
- [Local development](#local-development)
- [Running the tests](#running-the-tests)
- [Connecting Open WebUI](#connecting-open-webui)
- [File routing and `#folder:` tags](#file-routing-and-folder-tags)
- [Creating your own agents](#creating-your-own-agents)
- [Observability](#observability)
- [MCP endpoint](#mcp-endpoint)
- [Production deployment](#production-deployment)
- [Backups](#backups)
- [Configuration reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Docker Engine | 24+ | `docker --version` |
| Docker Compose | **2.24+** | The override files use `!override`, added in 2.24.4. `docker compose version` |
| GNU Make | any | Optional but every command below assumes it |
| Python | 3.12+ | Only for running the tests on the host |
| RAM | 4 GB free | Postgres + Phoenix + Open WebUI + API |
| Disk | 6 GB free | Images are ~4 GB together |
| An OpenRouter key | — | [Getting an OpenRouter key](#getting-an-openrouter-key) |
| An OpenAI key | — | Optional. Needed only for document knowledge |

---

## Quick start

```bash
git clone https://github.com/the-ai-buildr/OpenAgentPlatform.git
cd OpenAgentPlatform

make env            # writes .env and generates DB_PASS, OS_SECURITY_KEY, WEBUI_SECRET_KEY
```

Open `.env` and set at least:

```ini
OPENROUTER_API_KEY=sk-or-v1-...   # required for agents to answer
EMBEDDING_API_KEY=sk-proj-...     # optional; required for document knowledge
```

Then:

```bash
make up             # builds and starts everything
make status         # containers, /health, and the advertised models
```

| Service | URL |
|---|---|
| Open WebUI | <http://localhost:3000> |
| AgentOS API docs | <http://localhost:8000/docs> |
| Health | <http://localhost:8000/health> |
| Phoenix traces | <http://localhost:6006> |
| MCP | <http://localhost:8000/mcp> |

First boot takes a few minutes while images build. `make up` is safe to re-run.

Create your Open WebUI account at <http://localhost:3000> — the first account
registered becomes the admin. Then follow
[Connecting Open WebUI](#connecting-open-webui).

**Starting without keys works.** The API comes up, `/health` returns
`"status": "degraded"` and names each missing variable, and the container stays
healthy. Add the key to `.env` and run `make restart`.

---

## Getting an OpenRouter key

OpenRouter is one API in front of most commercial and open models, which is why
this stack uses it instead of wiring up each provider.

1. Sign up at <https://openrouter.ai>.
2. Add credit under **Settings → Credits**. Pay-as-you-go; a few dollars is
   plenty for testing. (Some models have free tiers, but rate limits are tight.)
3. Create a key at <https://openrouter.ai/keys>. It starts with `sk-or-v1-`.
4. Paste it into `.env` as `OPENROUTER_API_KEY` and run `make restart`.

Pick models from <https://openrouter.ai/models>. The id in `.env` must match the
catalogue exactly — `make health` will show the configured default, and a wrong
id surfaces as a 404 from OpenRouter on the first message.

Defaults:

```ini
AGNO_DEFAULT_MODEL=anthropic/claude-sonnet-4.5
AGNO_BUILDER_MODEL=anthropic/claude-sonnet-4.5
AGNO_ENGINEER_MODEL=anthropic/claude-sonnet-4.5
AGNO_MANAGER_MODEL=openai/gpt-4o-mini
```

To check an id is live:

```bash
curl -s https://openrouter.ai/api/v1/models | grep -o '"id":"anthropic/claude-sonnet-4.5"'
```

### Why two providers

**OpenRouter serves chat models only.** Its catalogue contains no embedding
models and it exposes no `/embeddings` endpoint, so embeddings must come from
somewhere else. `.env` therefore has a separate block:

```ini
EMBEDDING_API_KEY=sk-proj-...               # an OpenAI key, or any compatible server
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_MODEL=text-embedding-3-small      # OpenAI-native id, no "openai/" prefix
EMBEDDING_DIMENSIONS=1536
```

Any OpenAI-compatible embeddings server works — a local
[text-embeddings-inference](https://github.com/huggingface/text-embeddings-inference)
or an Ollama proxy, for example. Point `EMBEDDING_BASE_URL` at it and set
`EMBEDDING_DIMENSIONS` to that model's output size.

> **Changing the embedding model invalidates existing vectors.** The dimension
> is baked into each `kb_*` table. After a change, drop the tables and re-index
> (see [Troubleshooting](#troubleshooting)).

Leave `EMBEDDING_API_KEY` empty and the stack still runs: uploads are stored in
`workspace_files/` but not indexed, and agents have no retrieval.

---

## Local development

```bash
make help            # every target, grouped
make up              # start in the background (dev overlay: bind mount + reload)
make dev             # same, in the foreground with logs attached
make logs-api        # tail one service
make restart
make down            # stop; data is kept
```

`make up` uses `docker-compose.dev.yml`, which bind-mounts `./app` into the
container and runs uvicorn with `--reload`, so Python edits apply in about a
second. Changes to `requirements.txt` or the `Dockerfile` need `make build`.

Ports are published on `127.0.0.1` only. To reach the stack from another
machine, put a reverse proxy in front of it — see
[Production deployment](#production-deployment).

Useful during development:

```bash
make health          # /health as formatted JSON
make models          # what the bridge advertises to Open WebUI
make watcher-status  # is the indexer running, and what did it index
make files-tree      # the workspace tree
make kb-list         # the pgvector knowledge tables
make db-shell        # psql
make config          # render the dev compose config
```

---

## Running the tests

The suite covers the pure logic — path containment, table naming, the agent
registry, and the bridge's auth and OpenAI-shaped responses. It needs no
Docker, no database and no API keys.

```bash
make venv            # one-off: .venv with requirements-dev.txt
make test
make lint            # ruff check + ruff format --check
make format          # apply fixes
```

Or directly:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
pytest tests/test_paths.py -v          # one file
pytest -k traversal                    # one concern
```

CI (`.github/workflows/ci.yml`) runs lint, the test suite, `docker compose
config` on both overlays, and boots the built image with no provider keys to
assert it reports `degraded` and rejects an unauthenticated `/v1` call.

---

## Connecting Open WebUI

There are two ways to wire Open WebUI to AgentOS. **Use the Pipe.** It is one
step, it surfaces every agent as its own model, and it keeps file routing in
one place.

### Option A — the AgentOS Pipe (recommended)

1. Open <http://localhost:3000> and sign in as the admin.
2. **Workspace → Functions → `+`**.
3. Paste the contents of
   [`openwebui_functions/agentos_pipe_function.py`](openwebui_functions/agentos_pipe_function.py)
   and save it.
4. Toggle it **on**.
5. Click the gear next to the function and set the valves:
   - `agentos_base_url`: `http://agentos-api:8000` (the compose service name —
     *not* `localhost`, which inside the Open WebUI container means itself)
   - `api_key`: your `OS_SECURITY_KEY` from `.env`
   - `auto_route_uploads`: leave **off** — the bridge already routes uploads
6. Start a new chat. The model picker now lists:

   | Model | What it is |
   |---|---|
   | `AgentOS/Agno Platform Coordinator` | the `agno` team — routes to a specialist |
   | `AgentOS/Platform Builder` | creates agents and knowledge folders |
   | `AgentOS/Platform Engineer` | explains this codebase |
   | `AgentOS/Platform Manager` | health, routing, re-indexing |
   | `AgentOS/<your agent>` | anything you create |

Start with the Coordinator unless you know which specialist you want.

### Option B — a plain OpenAI connection

Compose already configures this (`OPENAI_API_BASE_URLS` and `OPENAI_API_KEYS`),
so the agents appear as models without installing anything. To set it up by
hand: **Settings → Admin → Connections → OpenAI API**, URL
`http://agentos-api:8000/v1`, key = `OS_SECURITY_KEY`.

In this mode Open WebUI does not forward uploads to the bridge, so install
[`openwebui_functions/agentos_file_router_filter.py`](openwebui_functions/agentos_file_router_filter.py)
as a **Filter** to route them. Set its `api_key` valve to `OS_SECURITY_KEY`.

> Do not enable the Filter *and* the Pipe's `auto_route_uploads` at the same
> time. Staging is idempotent — a byte-identical file is not re-copied or
> re-embedded — so nothing breaks, but you pay for the work twice.

---

## File routing and `#folder:` tags

Documents live in `workspace_files/`, one folder per topic. Each folder gets
its own pgvector table named `kb_<slug>`:

```
workspace_files/
├── general/            → ai.kb_general
├── sops/               → ai.kb_sops
├── contracts/          → ai.kb_contracts
└── agents/research/    → ai.kb_agents_research
```

### Three ways in

**Drop a file on disk.** Anything you copy into `workspace_files/<folder>/` is
picked up by the watcher and indexed about two seconds later
(`WATCHER_DEBOUNCE_SECONDS`). Check it with `make watcher-status`.

**Attach a file in chat.** Open WebUI stores the upload; the bridge copies it
into the right folder and indexes it. Where it lands:

1. a `#folder:` tag in your message, otherwise
2. the folder bound to the agent you are talking to, otherwise
3. `agents/<agent-id>` for a custom agent, or `general` for a core agent.

**Tag the destination explicitly:**

```
#folder:contracts Summarise the attached MSA and flag the liability cap.
```

```
#folder:sops/onboarding Add this checklist to the onboarding SOPs.
```

The tag is stripped from the prompt before the agent sees it. Folder names are
lowercased and slugified (`Acme Corp` → `acme-corp`), and anything that tries
to escape `workspace_files/` is rejected.

**Call the API directly:**

```bash
curl -X POST http://localhost:8000/v1/files/route \
  -H "Authorization: Bearer $OS_SECURITY_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"filename_query": "msa.pdf", "target_folder": "contracts"}'
```

### Supported types

`.pdf` `.txt` `.md` `.csv` `.json` `.docx`. Anything else is stored but not
indexed, and the response says so.

---

## Creating your own agents

Ask the Builder in chat:

> Create an agent called `research` named "Research Analyst". It should
> summarise market research, use the `research` knowledge folder, and have web
> search enabled.

The Builder writes the spec to `app/agents/registry.json`, creates the folder,
rebuilds the agent and re-registers it with AgentOS, so it shows up in
`/v1/models`, in `/agents` and in the Open WebUI model picker without a
restart.

The registry is a plain file you can edit and commit:

```json
{
  "custom_agents": [
    {
      "agent_id": "research",
      "name": "Research Analyst",
      "role": "Summarises market research",
      "instructions": "Cite the source document for every claim.",
      "model_id": "anthropic/claude-sonnet-4.5",
      "knowledge_folder": "research",
      "enable_web_search": true
    }
  ]
}
```

`platform-builder`, `platform-engineer`, `platform-manager` and `agno` are
reserved and cannot be overwritten.

---

## Observability

Every request, agent run, tool call, model call and SQL query is traced to the
Phoenix container.

```bash
open http://localhost:6006
make logs-otel
```

In Phoenix, look at **Traces** and filter by service `agno-agentos-api`. A
chat turn looks like: FastAPI request → `openwebui.chat_stream` → the Agno
agent run → the OpenRouter HTTP call → any pgvector query.

| Variable | Purpose |
|---|---|
| `OTEL_ENABLED` | Master switch |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | **Base** collector URL; the app appends `/v1/traces` |
| `OTEL_EXPORTER_OTLP_HEADERS` | `k=v,k2=v2` — for a hosted collector that needs auth |
| `OTEL_ENVIRONMENT` | Tags spans; set to `production` when deployed |
| `OTEL_SERVICE_NAME` | Service name in Phoenix |

To send traces to a hosted backend (Arize, Honeycomb, Grafana Cloud) instead,
point `OTEL_EXPORTER_OTLP_ENDPOINT` at its OTLP/HTTP base URL, add
`OTEL_EXPORTER_OTLP_HEADERS`, and drop the `agentos-otel` service.

Phoenix keeps traces in the `otel_data` volume. It is not a system of record —
`make destroy-volumes` discards them.

---

## MCP endpoint

AgentOS exposes itself as an MCP server at `/mcp`, so an MCP client (Claude
Desktop, Cursor, another agent) can drive the agents directly.

```
URL:    http://localhost:8000/mcp
Auth:   Authorization: Bearer <OS_SECURITY_KEY>
```

The server card is at `GET /mcp/server-card`. Set `ENABLE_MCP_SERVER=false` to
turn it off.

For a remote client, expose `/mcp` through the same TLS reverse proxy as the
rest of the API and keep the bearer key secret — it is a trusted root
credential that grants access to every endpoint.

---

## Production deployment

The production overlay removes the source bind mount and `--reload`, runs
multiple uvicorn workers, publishes only Open WebUI (on loopback), caps
container memory and rotates logs.

```bash
make prod-config     # render and validate first
make prod-up
make prod-logs
```

### Before you deploy

**1. Secrets.** Regenerate everything the template ships as a placeholder:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

| Variable | Why |
|---|---|
| `OS_SECURITY_KEY` | Bearer token for the whole API, including `/mcp`. Treat it as root. |
| `WEBUI_SECRET_KEY` | Signs Open WebUI sessions. Rotating it logs everyone out. |
| `DB_PASS` | Baked into the pgdata volume on first start — set it before the first `up`. |
| `OPENROUTER_API_KEY` | Scope and budget it in the OpenRouter dashboard. |
| `EMBEDDING_API_KEY` | Separate key, restricted to embeddings if your provider allows. |

`.env` is in `.gitignore` and `make env` writes it `0600`. For anything beyond
a single host, inject these from your orchestrator's secret store (Docker
secrets, SOPS, Vault, your platform's env vars) rather than a file on disk.

**2. Set `PUBLIC_BASE_URL`.** Required by `make prod-up`; Open WebUI uses it to
build absolute links.

```ini
PUBLIC_BASE_URL=https://agents.example.com
OTEL_ENVIRONMENT=production
UVICORN_WORKERS=2
```

**3. Terminate TLS in a reverse proxy.** Nothing in this stack serves HTTPS.
Only Open WebUI is published, on `127.0.0.1:3000`. Caddy is the shortest path:

```caddyfile
agents.example.com {
    reverse_proxy 127.0.0.1:3000
}
```

nginx:

```nginx
server {
    listen 443 ssl http2;
    server_name agents.example.com;

    ssl_certificate     /etc/letsencrypt/live/agents.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/agents.example.com/privkey.pem;

    add_header Strict-Transport-Security "max-age=31536000" always;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade           $http_upgrade;   # streaming
        proxy_set_header Connection        "upgrade";
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;                            # long agent runs
        proxy_buffering off;                                # token streaming
    }
}
```

`proxy_buffering off` and a long `proxy_read_timeout` matter: without them,
streamed responses arrive in one lump or get cut off mid-run.

**Do not publish the API, Postgres or Phoenix.** If you need the API from
outside, proxy it on a separate hostname and keep the bearer check on. If you
need Phoenix, put it behind the proxy with its own authentication — it has
none of its own, and traces contain prompt and response text.

**4. Know what this stack does not do.** It has no rate limiting, no per-user
cost caps and no WAF. Open WebUI's own auth is the only thing between a visitor
and your OpenRouter spend, so disable open sign-up
(**Settings → Admin → General → Enable New Sign Ups: off**) once your accounts
exist, and set a spend limit on the OpenRouter key.

**5. Upgrades.** Every image is pinned. To move up, change the tag, read that
project's release notes, `make build`, `make prod-up`, and check `make health`.
Take a database dump first.

---

## Backups

What is worth backing up:

| Volume | Contents | Recoverable without a backup? |
|---|---|---|
| `pgdata` | Sessions, runs, traces, embeddings | No |
| `openwebui_data` | Accounts, chats, uploaded files | No |
| `./workspace_files` | Source documents | It is a host directory — back it up like any other |
| `otel_data` | Phoenix traces | Disposable |

```bash
make db-dump                                    # → db/backups/<timestamp>.sql
make db-restore FILE=db/backups/20260101T120000Z.sql
```

A minimal nightly job:

```bash
0 3 * * *  cd /srv/OpenAgentPlatform && make db-dump && \
           tar czf "/backups/workspace-$(date -u +\%F).tgz" workspace_files/
```

Open WebUI's volume is not covered by `pg_dump`. Snapshot it separately:

```bash
docker run --rm -v agno-agentos-openwebui_openwebui_data:/data -v "$PWD:/out" \
  alpine tar czf /out/openwebui-data.tgz -C /data .
```

Embeddings can always be rebuilt from `workspace_files/` — ask the Manager to
re-sync a folder, or `POST /v1/files/route` again.

---

## Configuration reference

Every variable is documented inline in
[`.env.example`](.env.example), grouped by concern. The ones that matter most:

| Variable | Default | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Required. Without it the API runs degraded. |
| `AGNO_DEFAULT_MODEL` | `anthropic/claude-sonnet-4.5` | Must exist in the OpenRouter catalogue |
| `EMBEDDING_API_KEY` | — | Optional. Without it nothing is indexed. |
| `EMBEDDING_BASE_URL` | `https://api.openai.com/v1` | Not OpenRouter |
| `EMBEDDING_DIMENSIONS` | `1536` | Must match the model |
| `OS_SECURITY_KEY` | generated | Bearer token for the API, `/v1` and `/mcp` |
| `ENABLE_FILE_WATCHER` | `true` | Auto-index `workspace_files/` |
| `WATCHER_DEBOUNCE_SECONDS` | `2.0` | Quiet period before indexing |
| `MAX_STAGED_UPLOAD_BYTES` | `67108864` | 64 MB cap on staged uploads |
| `OTEL_ENABLED` | `true` | Tracing master switch |
| `ENABLE_MCP_SERVER` | `true` | Serve `/mcp` |
| `DB_PASS` | generated | Baked into the volume on first start |
| `WEBUI_SECRET_KEY` | generated | Open WebUI session signing |
| `PUBLIC_BASE_URL` | — | Required by `make prod-up` |

---

## Troubleshooting

### `make up` fails with `DB_PASS must be set in .env`

You have no `.env`. Run `make env`. The compose file deliberately refuses to
start with a default password instead of silently using one.

### `/health` says `degraded`

That is the design — it tells you what is missing:

```bash
make health
```

| Reason | Fix |
|---|---|
| `OPENROUTER_API_KEY is not set` | Add it to `.env`, `make restart` |
| `EMBEDDING_API_KEY is not set` | Add an OpenAI key, or accept no document knowledge |
| `database unreachable: ...` | `make logs-db`; usually the container is still starting |

### Password authentication failed for user "ai"

The `pgdata` volume was initialised with a *different* `DB_PASS`. Postgres only
reads `POSTGRES_PASSWORD` when it creates the data directory, so editing `.env`
afterwards changes the client side only.

**Do not run `make destroy-volumes`.** That deletes every session, embedding,
account and chat to fix a password mismatch. Change the password in the
database instead:

```bash
# Start only the database, then set the role's password to the new value
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d agentos-db
docker compose -f docker-compose.yml -f docker-compose.dev.yml exec agentos-db \
  psql -U ai -d agentos -c "ALTER USER ai WITH PASSWORD 'the-value-now-in-your-env';"
make restart
```

If you cannot authenticate at all, start the container once with
`POSTGRES_HOST_AUTH_METHOD=trust`, run the `ALTER USER`, then remove it.

### No models in Open WebUI

1. `make models` — if that fails, the API is down. `make logs-api`.
2. If it works from the host but Open WebUI shows nothing, the Pipe's
   `agentos_base_url` is probably `http://localhost:8000`. Inside the Open WebUI
   container `localhost` is Open WebUI. Use `http://agentos-api:8000`.
3. 401 in the logs means the Pipe's `api_key` valve does not match
   `OS_SECURITY_KEY`.

### Uploaded files are not indexed

```bash
make watcher-status
```

| Symptom | Cause |
|---|---|
| `"watcher_running": false` | `ENABLE_FILE_WATCHER=false`, or it failed to start — check `make logs-api` |
| `recent_events` shows an `EMBEDDING_API_KEY` error | No embeddings configured |
| The file never appears | Unsupported extension, or the upload is not in the mounted `openwebui_data` volume |
| `make kb-list` shows no table | Nothing has been indexed into that folder yet |

### Open WebUI cannot find an attached file

The API reads uploads from Open WebUI's volume, mounted read-only at
`/app/openwebui_data`. If you replaced the `open-webui` service or changed its
volume, that mount no longer points at the uploads directory.

### A model id 404s

The id is not in the OpenRouter catalogue, or your key has no access to it.
Check it at <https://openrouter.ai/models>, and confirm you have credit.

### Everything is slow on the first message

Cold start: the container builds the agents, connects to Postgres and
provisions tables. The healthcheck allows 60 seconds. Later messages are only
as slow as the model.

### Streaming responses arrive all at once

A proxy is buffering. Set `proxy_buffering off` (nginx) or use Caddy, which
does the right thing by default.

### Resetting everything

```bash
make destroy-volumes     # asks you to type "destroy"
```

Deletes the database, Open WebUI accounts and chats, and all traces.
`workspace_files/` is untouched, so re-indexing restores your documents.

---

## Project layout

```
.
├── app/
│   ├── settings.py              Typed configuration from the environment
│   ├── main.py                  Entrypoint: tracing, AgentOS, lifespan, /health
│   ├── telemetry/otel.py        Tracer provider and library instrumentation
│   ├── knowledge/
│   │   ├── paths.py             Path containment and table naming (pure)
│   │   ├── knowledge_manager.py Per-folder pgvector knowledge bases
│   │   └── file_watcher.py      Debounced auto-indexer
│   ├── agents/
│   │   ├── store.py             registry.json CRUD and folder rules (pure)
│   │   ├── runtime.py           Builds the Agno agents and the team
│   │   └── registry.json        Your custom agents
│   ├── tools/platform_tools.py  The tools the agents call
│   └── bridge/
│       ├── security.py          Bearer auth for /v1
│       ├── schemas.py           Request/response models
│       └── openwebui_router.py  The OpenAI-compatible bridge
├── openwebui_functions/         Pipe and Filter to paste into Open WebUI
├── workspace_files/             Documents, one folder per topic
├── db/                          pgvector image and first-run SQL
├── tests/                       pytest suite
├── docs/ARCHITECTURE.md         Design and the review of the original stack
├── docker-compose.yml           Base stack
├── docker-compose.dev.yml       Bind mount, reload, published ports
├── docker-compose.prod.yml      No reload, workers, loopback only
├── Dockerfile                   Multi-stage, non-root
├── Makefile                     Every command
└── .env.example                 Every variable, documented
```

---

## License

MIT — see [LICENSE](LICENSE).
