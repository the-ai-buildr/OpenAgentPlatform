# Docker deployment

How this stack is packaged, how the three Compose files fit together, and what
to do when you put it on a real host. For the system design and the review of
the original paste, see [ARCHITECTURE.md](ARCHITECTURE.md). Day-to-day commands
live in the [README](../README.md).

---

## Compose layout

| File | Role |
|---|---|
| `docker-compose.yml` | Base stack: Postgres + pgvector, Phoenix, AgentOS API, Open WebUI |
| `docker-compose.dev.yml` | Local only — publish ports on `127.0.0.1`, bind-mount `./app`, `--reload` |
| `docker-compose.prod.yml` | Deploy — no reload, no source mount, multiple workers, Open WebUI on loopback only |

```
make up        →  docker compose -f docker-compose.yml -f docker-compose.dev.yml …
make prod-up   →  docker compose -f docker-compose.yml -f docker-compose.prod.yml …
```

Never start with the base file alone. It builds the images and wires volumes,
but it does not publish Open WebUI (or anything else), so nothing is reachable
from the host until an overlay is applied.

Project name (from the base file): `agno-agentos-openwebui`. Volumes are
prefixed with that name — e.g. `agno-agentos-openwebui_pgdata`.

---

## Images and build

| Image | Source | Notes |
|---|---|---|
| `agno-agentos-api:local` | root `Dockerfile` | Multi-stage Python 3.12, non-root uid `10001`, no `--reload` in `CMD` |
| `agentos-db` | `db/Dockerfile` | `pgvector/pgvector:0.8.7-pg17` + `db/init/*.sql` |
| `ghcr.io/open-webui/open-webui:v0.12.3` | pinned pull | Chat UI |
| `arizephoenix/phoenix:version-20.23.0` | pinned pull | Traces UI / OTLP collector |

The API image copies `app/` and `openwebui_functions/` only. Docs, tests,
`.env`, and `workspace_files/` are excluded by `.dockerignore` — workspace
content is bind-mounted at runtime.

```bash
make build          # layer-cached
make rebuild        # --no-cache
make prod-config    # render + validate production before the first up
```

Without Make:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml config
```

---

## Services and exposure

```
                    ┌─ loopback only (prod) ─┐
  reverse proxy ───▶│ open-webui :8080       │◀── browser (via TLS proxy)
                    │   OPENAI → agentos-api │
                    └───────────┬────────────┘
                                │ compose network
          ┌─────────────────────┼──────────────────────┐
          ▼                     ▼                      ▼
   agentos-api:8000      agentos-db:5432        agentos-otel:6006
   (not published)       (not published)        (not published)
          │
          ├─ ./workspace_files  (rw)
          └─ openwebui_data     (ro, upload source)
```

| Service | Dev host ports | Prod host ports |
|---|---|---|
| `open-webui` | `127.0.0.1:3000` | `127.0.0.1:3000` |
| `agentos-api` | `127.0.0.1:8000` | none — exec or proxy |
| `agentos-db` | `127.0.0.1:5432` | none |
| `agentos-otel` | `127.0.0.1:6006` | none |

Production assumes a TLS-terminating reverse proxy on the host in front of
`127.0.0.1:3000`. Example Caddy and nginx configs are in the README
[Production deployment](../README.md#production-deployment) section.

---

## Deploy checklist

Do these in order on a fresh host.

1. **Install** Docker Engine 24+ and Compose **2.24+** (`!override` requires it).
2. **Clone** the repo and run `make env` (or `python scripts/init_env.py`).
3. **Edit `.env`:**
   - `OPENROUTER_API_KEY` — required for agents to answer
   - `EMBEDDING_API_KEY` — optional; without it uploads are stored but not indexed
   - Confirm `DB_PASS`, `OS_SECURITY_KEY`, `WEBUI_SECRET_KEY` were generated (not left as `CHANGE_ME_…`)
   - Set `PUBLIC_BASE_URL=https://your.domain` — **required** by `make prod-up`
   - Set `OTEL_ENVIRONMENT=production`
   - Optionally `UVICORN_WORKERS=2` (default in the prod overlay)
4. **Validate:** `make prod-config`
5. **Start:** `make prod-up`
6. **Verify** (API is not on the host network — use exec):

   ```bash
   make prod-ps
   make prod-health
   # or:
   docker compose -f docker-compose.yml -f docker-compose.prod.yml \
     exec -T agentos-api curl -fsS http://localhost:8000/health
   ```

7. **Put TLS in front** of `127.0.0.1:3000` and open that hostname in a browser.
8. **Create the first Open WebUI admin**, install the Pipe (see README), then
   **disable open sign-ups**.
9. **Backups:** schedule `make db-dump` (or the prod compose equivalent) and
   snapshot `workspace_files/` + the `openwebui_data` volume — see README
   [Backups](../README.md#backups).

### Secrets that are baked in on first start

`DB_PASS` is applied only when Postgres creates the data directory. Changing it
in `.env` later does **not** change the role password inside an existing
`pgdata` volume. Fix with `ALTER USER` (README troubleshooting) — do not
`destroy-volumes` to “fix” a password mismatch.

---

## Production overlay behaviour

What `docker-compose.prod.yml` changes relative to the base:

| Change | Why |
|---|---|
| API `command` → uvicorn with `--workers`, `--proxy-headers` | Multi-process, correct client IPs behind a proxy |
| API volumes `!override` without `./app` | Image is the artefact; no live source mount |
| Phoenix `ports: !override []` | Trace UI stays off the host |
| Open WebUI published on `127.0.0.1` only | Reverse proxy is the single ingress |
| `WEBUI_URL` ← `PUBLIC_BASE_URL` | Absolute links in Open WebUI |
| `restart: always`, log rotation | Survive reboot; bound log growth |
| `deploy.resources.limits.memory` | Soft documentation of intended caps |

> **Memory limits and Compose.** The `deploy.resources` block is honoured by
> Docker Swarm and by Compose in compatibility modes that understand it. Plain
> `docker compose up` on a single host often **ignores** those limits. If you
> need hard cgroup caps without Swarm, set them with your orchestrator or a
> wrapper (e.g. systemd/`MemoryMax`, Kubernetes limits, or Compose
> `mem_limit` where your engine supports it). The values in the overlay still
> document the intended size of each service.

---

## Without Make

Every Make target is a thin wrapper. Equivalent production commands:

```bash
# env
python3 scripts/init_env.py

# validate / up / down / logs
export COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
$COMPOSE config
$COMPOSE up -d --build
$COMPOSE ps
$COMPOSE logs -f --tail=100
$COMPOSE exec -T agentos-api curl -fsS http://localhost:8000/health
$COMPOSE down --remove-orphans
```

Development (bind mount + reload):

```bash
export COMPOSE="docker compose -f docker-compose.yml -f docker-compose.dev.yml"
$COMPOSE up -d --build
```

---

## Health and degraded mode

The API container’s healthcheck is `curl http://localhost:8000/health`. A
missing `OPENROUTER_API_KEY` or `EMBEDDING_API_KEY` does **not** fail the
healthcheck — `/health` returns HTTP 200 with `"status": "degraded"` and a
list of reasons. That is intentional so the stack can boot before keys are
pasted.

`make prod-up` still requires `DB_PASS`, `OS_SECURITY_KEY`, `WEBUI_SECRET_KEY`,
and `PUBLIC_BASE_URL` — Compose uses `${VAR:?…}` and refuses to start rather
than falling back to a published default.

---

## Upgrades

1. `make db-dump` (and snapshot `workspace_files/` / Open WebUI data).
2. Change the image tags you intend to move (API rebuilds from this repo;
   Open WebUI and Phoenix are pinned in `docker-compose.yml`).
3. `make build` then `make prod-up`.
4. `make prod-health` and smoke-test a chat turn through the proxy.

---

## What not to publish

Do not map `agentos-api`, `agentos-db`, or `agentos-otel` to `0.0.0.0`.

- The API bearer key (`OS_SECURITY_KEY`) is effectively root for agents, MCP,
  and spend.
- Postgres holds sessions, embeddings and metrics.
- Phoenix has no auth of its own and stores prompt/response text in traces.

If you need the API or Phoenix from outside, put each on its own hostname
behind the same TLS proxy with authentication in front.
