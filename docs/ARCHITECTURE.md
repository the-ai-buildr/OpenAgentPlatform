# Architecture — Agno AgentOS + Open WebUI

This document is the **current architecture** of the stack in this repository,
plus the review of the pasted `agno-openwebui-stack` that produced it. Operator
steps for bringing the containers up (dev and production) live in
**[DOCKER.md](DOCKER.md)** and the [README](../README.md).

Reviewed against: **Agno 3.1.4** (current on PyPI), the live OpenRouter model
catalogue, Open WebUI v0.12.3 and Arize Phoenix 20.23.0. Every API claim below
was checked by importing the real package and reading the signature, or by
calling the real HTTP endpoint — not from the pasted imports.

---

## 1. What this system is

Open WebUI is the chat front end. It speaks the OpenAI chat-completions
protocol, so the AgentOS container exposes a small `/v1` bridge that translates
OpenAI requests into Agno agent and team runs. Documents the user uploads are
copied into a per-topic folder on disk; a filesystem watcher embeds them into a
per-folder pgvector table, and the agent bound to that folder retrieves from
it. Everything the API does is traced to a Phoenix container over OTLP.

```
                           ┌──────────────────────────────────────────────┐
  browser ──HTTPS──▶ reverse proxy ──▶ open-webui :8080                   │
                           │             │                                │
                           │             │ OpenAI protocol                │
                           │             │ Authorization: OS_SECURITY_KEY │
                           │             ▼                                │
                           │   ┌──────────────────────────────────────┐   │
                           │   │ agentos-api :8000                    │   │
                           │   │                                      │   │
                           │   │  /v1  bridge      (this repo)        │   │
                           │   │    ├─ GET  /v1/models                │   │
                           │   │    ├─ POST /v1/chat/completions      │   │
                           │   │    ├─ POST /v1/files/route           │   │
                           │   │    └─ GET  /v1/files/watcher-status  │   │
                           │   │  /health                             │   │
                           │   │  /docs, /mcp, /agents, /sessions …   │   │
                           │   │         (AgentOS, Agno 3.1.4)        │   │
                           │   │                                      │   │
                           │   │  agents: platform-builder            │   │
                           │   │          platform-engineer           │   │
                           │   │          platform-manager            │   │
                           │   │          + registry.json agents      │   │
                           │   │  team:   agno (coordinator)          │   │
                           │   │                                      │   │
                           │   │  watchdog ─▶ debounce ─▶ embed       │   │
                           │   └───┬─────────────┬──────────────┬─────┘   │
                           │       │             │              │         │
                           │  chat │        embeddings      OTLP/HTTP     │
                           │  LLM  │             │              │         │
                           └───────┼─────────────┼──────────────┼─────────┘
                                   ▼             ▼              ▼
                            OpenRouter     OpenAI (or any   agentos-otel
                            /api/v1        OpenAI-compatible  (Phoenix)
                                           embeddings host)    :6006

   volumes
     ./workspace_files  ──▶ /app/workspace_files   (watched, read-write)
     openwebui_data     ──▶ /app/openwebui_data    (read-only; upload source)
     pgdata             ──▶ agentos-db  postgres 17 + pgvector 0.8.7
                              ai.agno_*  AgentOS sessions, runs, metrics, …
                              ai.kb_*    one vector table per workspace folder
```

### Request path for a chat turn

1. Open WebUI posts `/v1/chat/completions` with the bearer key.
2. `require_bridge_auth` compares it to `OS_SECURITY_KEY` in constant time.
3. The bridge takes the last user message, pulls out any `#folder:<path>` tag,
   and stages attached uploads into the resolved folder (idempotent — a
   byte-identical file already in place is not re-copied or re-embedded).
4. The model id resolves to the `agno` team or a single agent.
5. `arun(...)` streams `RunContentEvent`s, which are re-encoded as OpenAI SSE
   chunks terminated by `data: [DONE]`.

### Dependency direction

The original had a cycle it worked around with function-local imports. The
modules are now layered so imports only ever point downwards:

```
  settings  ◀── telemetry/otel
     ▲              ▲
     │              │
  knowledge/paths ◀─┴── knowledge/knowledge_manager ◀── knowledge/file_watcher
     ▲                          ▲                              ▲
     │                          │                              │
  agents/store ◀──────── tools/platform_tools ──────────────────┘
     ▲                          ▲
     └──── agents/runtime ──────┘
                 ▲
           bridge/openwebui_router
                 ▲
              main
```

`agents/store` never imports `runtime`. Instead `runtime` calls
`store.subscribe(...)` at import time, so a tool that edits the registry
triggers an agent rebuild through a callback rather than an upward import.

---

## 2. Review findings

Each hypothesis from the brief, confirmed or rejected, with what was done.

### Confirmed and fixed

| # | Finding | Severity | Fix |
|---|---|---|---|
| 1 | **Circular imports** between `platform_tools`, `platform_agents` and `file_watcher`, papered over with in-function imports | Required | Split the registry into `app/agents/store.py` (pure, no Agno) and `app/agents/runtime.py` (builds Agno objects). `runtime` subscribes to store mutations. All imports are now module-level and one-directional. |
| 2 | **`@app.on_event`** is deprecated in Starlette/FastAPI | Required | `AgentOS(..., lifespan=lifespan)` with an `asynccontextmanager`. Verified `AgentOS.__init__` accepts `lifespan`. |
| 3 | **`uvicorn --reload` baked into the image** | Required | Image `CMD` has no `--reload`. Reload lives only in `docker-compose.dev.yml`, which is also the only place the source is bind-mounted. Reload breaks FastAPI lifespan when MCP tools are attached, and doubles memory. |
| 4 | **OTel initialised after the DB engines exist**, so SQLAlchemy was untraced | Required | `init_tracing()` is the first statement in `app/main.py`, before `agno` or any app module is imported. `SQLAlchemyInstrumentor` patches `create_engine` at class level, so engines built earlier are invisible to it. The import ordering is enforced by `# noqa: E402` comments that explain why. |
| 5 | **`/v1` bridge had no auth check** even though Open WebUI sends the key | **Critical** | `require_bridge_auth` on the router as a router-level dependency, `hmac.compare_digest`, accepts `Authorization: Bearer` or `X-API-Key`. Empty key = open with a one-shot warning (documented local mode); production compose refuses to start without one. Covered by 8 tests. |
| 6 | **Path traversal check used a string prefix** (`str(target).startswith(str(root))`) | **Critical** | `resolve_within()` uses `Path.resolve()` + `is_relative_to`. The prefix form accepts `/app/workspace_files_evil/...`; there is a regression test for exactly that, and for symlink escape. Mutating the fix back to `startswith` makes the suite fail. |
| 7 | **`read_source_file` could expose secrets** — it was scoped to all of `/app` | **Critical** | Allowlisted to `app/` and `openwebui_functions/`, restricted to source extensions, dotfiles refused, 200 KB cap. |
| 8 | **OTLP endpoint semantics** | Required | `OTEL_EXPORTER_OTLP_ENDPOINT` is a *base* URL per the OpenTelemetry spec — the SDK appends `/v1/traces`. The original put the full path in it, so any component doing standard autoconfiguration would post to `/v1/traces/v1/traces`. It now holds `http://agentos-otel:6006` and the app appends the path. |
| 9 | **Phoenix `4317:4317` host mapping** | Nit | Removed. 4317 is Phoenix's gRPC port and is correct *inside* the network, but the app exports over OTLP/HTTP on 6006, so publishing 4317 to the host only invited a port clash. The UI port is now bound to `127.0.0.1`. |
| 10 | **OpenRouter does not serve embeddings** | **Critical** | Verified against the live catalogue: `https://openrouter.ai/api/v1/models` returns 470 models, none of them embedding models, and `openai/text-embedding-3-small` is absent. Embedding every document through OpenRouter would have failed on the first upload. Embeddings are now a separate provider (`EMBEDDING_BASE_URL`, default `https://api.openai.com/v1`, model `text-embedding-3-small` — the OpenAI-native id, not the `openai/`-prefixed OpenRouter form). The OpenRouter key is only reused as an embedding key when the embedding base URL really is OpenRouter. |
| 11 | **Embedding dimension vs PgVector** | Required | `EMBEDDING_DIMENSIONS` is explicit (1536) and passed to `OpenAIEmbedder`; PgVector sizes its column from the embedder. `.env.example` and the README state that changing the model invalidates existing `kb_*` tables. |
| 12 | **Outdated default model ids** | Required | `anthropic/claude-3.7-sonnet` is no longer in the OpenRouter catalogue — a run with it 404s. Defaults are now `anthropic/claude-sonnet-4.5` and `openai/gpt-4o-mini`, both verified present. |
| 13 | **Default secrets in compose fallbacks** (`${DB_PASS:-ai_secret_password}`, `${OS_SECURITY_KEY:-agno-local-dev-secret-key}`) | **Critical** | Secrets use `${VAR:?message}`: compose refuses to start rather than booting with a password published in a template. `make env` generates random values with `secrets.token_urlsafe`. |
| 14 | **Files routed up to three times** (Filter → Pipe → bridge) | Required | The bridge is the single routing authority. The Pipe's `auto_route_uploads` now defaults to `False`, and the Filter is documented as "only when AgentOS is a plain OpenAI connection — leave off with the Pipe". Belt and braces: `stage_openwebui_upload` compares SHA-256 digests and skips the copy and the re-embed when the file is already in place, so double routing is cheap rather than wrong. |
| 15 | **Dynamic agents never registered with AgentOS** — only the bridge saw them | Required | `AgentOS.resync(app)` exists in 3.1.4 and is now called from a listener: a registry change rebuilds the agents and re-registers them, so `/agents` and the Control Plane see them too. |
| 16 | **Unpinned `:latest` / `:main` images** | Required | `arizephoenix/phoenix:version-20.23.0`, `ghcr.io/open-webui/open-webui:v0.12.3`, `pgvector/pgvector:0.8.7-pg17`. |

### Confirmed, found during the review (not in the brief)

| # | Finding | Severity | Fix |
|---|---|---|---|
| 17 | **`agno>=2.0.0` resolves to 3.1.4**, whose APIs are not source-compatible with the 2.x code in the paste | **Critical** | The whole application is written against 3.1.4 and pinned exactly. This is the single largest change: the paste would not have imported. |
| 18 | `mcp_server=True` is deprecated | Required | `mcp=True`, and `agno[os,mcp]` so the extra is actually installed. |
| 19 | `Knowledge(...)` positional construction and `contents_db=` | Required | `Knowledge` is keyword-only since 3.0.7; `content_db=` is the current field name. |
| 20 | `docx2txt` in requirements | Required | Agno's `DocxReader` imports `python-docx`; `docx2txt` would have produced an `ImportError` on the first `.docx`. |
| 21 | No non-root user in the image | Required | Runs as uid 10001. Multi-stage build also drops `build-essential` and `libpq-dev` from the final image. |
| 22 | Every port published on `0.0.0.0` | Required | Dev ports bind `127.0.0.1`; production publishes only Open WebUI, on loopback, behind a proxy. |
| 23 | Missing `start_period` on the API healthcheck | Nit | 60 s, so a cold start with model downloads is not reported unhealthy. |
| 24 | No request validation on `/v1/chat/completions` | Required | Pydantic models in `app/bridge/schemas.py`. |
| 25 | No tests, no CI | Required | 101 pytest cases plus a GitHub Actions workflow that lints, tests, validates both compose files and boots the image to assert it degrades cleanly without keys. |
| 26 | A missing API key crashed the app at import | Required | Without `OPENROUTER_API_KEY` the app serves a degraded FastAPI app: `/health` explains why, `/v1/models` still answers, and the container stays healthy instead of crash-looping. |
| 27 | `make clean` silently ran `down -v` | Required | Renamed `destroy-volumes`, spells out what is lost, requires typing `destroy`, and explicitly says it is **not** the fix for a password error. |

### Found only by running it

These three were invisible to static review and were caught by booting the
stack. Each had a passing-looking implementation beforehand.

| # | Finding | Severity | Fix |
|---|---|---|---|
| 28 | **Routes added after `AgentOS.get_app()` are unreachable.** AgentOS appends a catch-all `Mount('')` to the app, so the whole `/v1` router 404'd even though it appeared in `/openapi.json`. The custom `/health` was shadowed by AgentOS's own, which returns only `{"status": "ok"}` — the degraded report was never visible. | **Critical** | Register `/health` and the bridge on a `FastAPI` *base app*, then hand it to `AgentOS(base_app=..., on_route_conflict="preserve_base_app")`. This is the documented way to compose AgentOS with your own routes. Confirmed by dumping `app.routes` in the running container. |
| 29 | **SQLAlchemy was still untraced after the ordering fix.** `opentelemetry-instrumentation-sqlalchemy` declares `sqlalchemy<2.1.0` and, outside that range, logs an error and instruments *nothing*. The pin resolved to SQLAlchemy 2.1.5, so every database query was invisible. Agno sets no upper bound, so this would silently recur on any unpinned rebuild. | Required | Pinned `SQLAlchemy==2.0.55` with the reason in `requirements.txt`. Verified by reading SQL spans out of Phoenix's API. So hypothesis #4 had *two* independent causes; fixing the import order alone would have looked correct and changed nothing. |
| 30 | **A provider failure reached the user as a normal assistant reply.** Agno does not raise when OpenRouter rejects a key — it completes the run with `status=RunStatus.error` and puts the provider's text in `content`. The bridge passed that straight through, so an invalid key produced an HTTP 200 and a chat bubble reading "User not found.". Streaming was worse: an empty message and no error at all. | **Critical** | `run_failed()` inspects the run status; non-streaming answers `502 Agent run failed: <message>`, streaming emits a visible `[agentos error] …` chunk before `[DONE]`. Verified live against a deliberately invalid key. |
| 31 | **`/v1/models` returned 500 with no LLM key.** Building the catalogue constructed the agents, which constructed a model, which raised. The degraded mode that was supposed to be graceful returned `Internal Server Error`. | Required | `describe_agents()` returns an empty list and `get_runner()` returns `None` when no key is set; `/v1/chat/completions` answers `503` naming the missing variable. Covered by `tests/test_degraded_mode.py`. |

### Rejected or downgraded

| # | Hypothesis | Verdict |
|---|---|---|
| A | Phoenix port `4317` is the wrong port | **Rejected as stated.** 4317 is correct for Phoenix's gRPC receiver. The actual defect was the OTLP *HTTP* endpoint carrying `/v1/traces` in a variable that the spec says is a base URL (#8). Publishing 4317 to the host was unnecessary, which is a separate, minor point (#9). |
| B | The `openwebui_data` read-only mount is a layering violation | **Accepted as-is.** It couples the API to Open WebUI's storage, but it is the only way to get the original bytes of an upload without Open WebUI re-posting them. The mount is read-only and the staging path sanitises filenames, resolves inside the workspace and caps file size. Documented as a known coupling rather than changed. |
| C | `GRANT ALL PRIVILEGES ON SCHEMA ai TO CURRENT_USER` in the init SQL is a bug | **Rejected.** It is a harmless no-op: the init script already runs as the owning superuser. Replaced with `CREATE SCHEMA ... AUTHORIZATION CURRENT_USER`, which states the intent in one statement. |
| D | The `/v1` bridge duplicates AgentOS's own API and should be dropped | **Rejected.** AgentOS speaks its own protocol; Open WebUI speaks OpenAI's. Something has to translate, and a thin router is the smallest thing that can. |
| E | `search_type=hybrid` is wrong for small corpora | **Rejected.** Hybrid is a good default here: workspace documents are keyword-heavy (SOPs, contracts) and pure vector search misses exact identifiers. |

---

## 3. Decisions worth knowing

**Degraded mode over crash-loop.** `AgentOS` cannot be constructed without a
model, and a model cannot be constructed without a key. Rather than exiting,
`app/main.py` serves a reduced FastAPI app whose `/health` names the missing
variable. An operator who starts the stack before pasting their key gets an
answer instead of a restart loop, and the Docker healthcheck still passes.

**Embeddings are optional.** Without `EMBEDDING_API_KEY` the stack still runs:
uploads land in `workspace_files/`, the watcher logs that it cannot index, and
`/health` lists the reason. Agents simply have no knowledge tools.

**`#folder:` tagging lives in the bridge, not in the agent prompt.** Folder
selection is deterministic routing, not something to spend a model call on.

**One vector table per folder.** `kb_<slug>` keeps retrieval naturally scoped
and makes "re-index just the contracts" a one-table operation. The cost is a
table per folder; at this scale that is cheaper than filter predicates on a
single large table.

**The registry is a JSON file, not a table.** It is small, operator-editable
and belongs to the deployment rather than to the data. Writes are atomic
(`mkstemp` + `os.replace`).

---

## 4. Docker packaging (deploy topology)

The runtime unit is four Compose services. Source of truth for ports, overlays
and the production checklist is **[DOCKER.md](DOCKER.md)**; this section is the
architectural view.

| Service | Image | Role |
|---|---|---|
| `agentos-db` | `db/Dockerfile` → `pgvector/pgvector:0.8.7-pg17` | Sessions, runs, metrics (`ai.agno_*`) and per-folder vectors (`ai.kb_*`) |
| `agentos-otel` | `arizephoenix/phoenix:version-20.23.0` | OTLP/HTTP collector and Phoenix UI |
| `agentos-api` | root `Dockerfile` → `agno-agentos-api:local` | FastAPI + AgentOS + `/v1` bridge + watcher |
| `open-webui` | `ghcr.io/open-webui/open-webui:v0.12.3` | Chat UI; talks to the bridge over the compose network |

**Overlays, not profiles.** Development and production are separate Compose
files layered on the base (`docker-compose.dev.yml` / `docker-compose.prod.yml`).
The base file alone builds and wires services but publishes no host ports —
an overlay is always required.

**Image contract.** The API image is multi-stage, runs as uid `10001`, and its
`CMD` has no `--reload`. Reload and the `./app` bind mount exist only in the
dev overlay. Production overrides the command to multiple uvicorn workers with
`--proxy-headers`, keeps `./workspace_files` and the read-only Open WebUI
uploads mount, and publishes only Open WebUI on `127.0.0.1`.

**Secret refusal.** Compose uses `${VAR:?message}` for `DB_PASS`,
`OS_SECURITY_KEY`, `WEBUI_SECRET_KEY`, and (prod) `PUBLIC_BASE_URL`. Missing
values abort startup instead of substituting a published default.

**Volumes.**

| Name / path | Mount | Survives `down`? |
|---|---|---|
| `pgdata` | Postgres data | Yes (until `down -v` / `destroy-volumes`) |
| `openwebui_data` | Open WebUI accounts, chats, uploads; also mounted read-only on the API | Yes |
| `otel_data` | Phoenix working dir | Yes (disposable) |
| `./workspace_files` | Host bind — source documents | Host-managed |

**Network trust boundary.** Inside the compose network, Open WebUI reaches the
API as `http://agentos-api:8000` with `OS_SECURITY_KEY`. Outside, only the
reverse proxy should touch Open WebUI. The API, database and Phoenix stay off
the public interface in production.

---

## 5. Verification

| Check | How | Result |
|---|---|---|
| Agno API surface | Imported `agno==3.1.4` and read signatures for `AgentOS`, `Knowledge`, `PgVector`, `OpenAIEmbedder`, `OpenRouter`, `Agent`, `Team` | Every call site in this repo matches |
| `AgentOS.resync` exists | `inspect.signature(AgentOS.resync)` | `(self, app: FastAPI) -> None` |
| OpenRouter has no embeddings | `GET https://openrouter.ai/api/v1/models` | 470 models, zero embedding models |
| `anthropic/claude-3.7-sonnet` is gone | Same catalogue | Absent; `claude-sonnet-4.5` present |
| OpenInference instrumentor works on Agno 3.x | `AgnoInstrumentor().instrument()` against 3.1.4 | Succeeds (it declares `agno>=2.7.2`) |
| Traversal guard | Reverted the fix to `startswith` and re-ran the suite | `test_rejects_sibling_with_shared_prefix` fails, then passes again |
| Compose validity | `docker compose config` on dev and prod overlays | Both valid |
| Full stack boots | `docker compose up` with Postgres, Phoenix and the API | API healthy; `/health` reports `degraded` only for the missing embeddings key |
| Bridge auth | `GET /v1/models` with and without the key | 401 then 200, team plus three agents |
| MCP | `GET /mcp` and `/mcp/server-card` | 401 unauthenticated, 200 with the key |
| Tracing really works | Queried Phoenix's own API for stored spans | SQL spans present with `db.statement` attributes |
| Watcher | Wrote a file into `workspace_files/sops/` | Debounced event logged ~2 s later with the honest "no embedding key" reason |
| Open WebUI integration | Ran Open WebUI v0.12.3 against the bridge as an OpenAI connection | Its `/api/models` lists all four agents with the right display names |
| Provider errors surface | Chat with a deliberately invalid OpenRouter key | `502 Agent run failed: User not found.`, and `[agentos error] …` in the stream |
| Degraded boot | Container started with no provider keys and an unreachable database | `/health` → `degraded` listing all three reasons, `/v1/models` → `[]`, `/v1/chat/completions` → 503 naming the variable, 401 still enforced |
| Test suite size | `pytest --collect-only` against `tests/` | 101 test functions |

See the "Verified running vs statically checked" section of the original stack
pull request for what was executed end to end on the build machine.
