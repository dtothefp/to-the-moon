# Deploy infra (Module 3)

Railway project **sysdesign** (`12dffbd4-65bd-44f7-83b7-d30238c92892`), one `production`
environment. Postgres is NOT here, it lives in the sibling Railway project
**ai-backend** (`d839d383-445b-4eae-8f49-1aec4ea6a4ba`), service `postgres`, database
`sysdesign`. Railway private networking is per-project, so these services reach it over
its TCP proxy at `altaria.proxy.rlwy.net:12563` rather than `.railway.internal`.
It moved off Supabase on 2026-09-12; see
`context/migrations/supabase-to-railway-move1.md` in the parent workspace.

| Service | Source | Runs |
|---|---|---|
| `api` | GitHub `dtothefp/to-the-moon`, root = repo root | uvicorn, public domain `sysdesign.thedefrag.ai` |
| `worker` | same repo + root | Celery worker with beat embedded, private |
| `chat` | same repo + root | Module 7 chat AGENT (the ReAct loop, code in `services/agent`), public domain `chat.thedefrag.ai` |
| `redis` | image `redis:7-alpine` + volume | broker, result backend, SSE pub/sub, and (step 2) chat fan-out, private |

All four are defined in [.railway/railway.ts](../.railway/railway.ts). Two more are defined
there but not provisioned, each behind a flag. `messaging` is the messaging drill's FastAPI +
WebSocket gateway (code in `services/chat`, NOT the same as `chat` above, that one's the
agent). `agent-ts` is the TypeScript agent port.

## Where each piece of config lives

We deliberately skipped Terraform. Railway has no official provider and the community one
is niche. Railway's own Infrastructure as Code is the codified surface.

- **Service shape** in [.railway/railway.ts](../.railway/railway.ts). Sources, watch
  patterns, start commands, healthchecks, restart policy, the production-only migration
  step, replicas, domains, redis and its volume. Reviewed in PRs like any code.
- **Env var values** in [railway-env.py](railway-env.py). railway.ts lists every var but
  marks each one `preserve()`, so it never holds a value. The manifest in the script says
  which variables each service gets and where values come from (the repo-root `.env`,
  gitignored). `python3 infra/railway-env.py list` shows what's live on Railway with secrets
  redacted, `sync --dry` diffs manifest vs remote and flags drift, `sync` pushes.
- **One-time account-level provisioning** (the GitHub connection, DNS records) is
  documented here rather than scripted. It changes rarely. When it changes, update this file
  in the same PR.

### Status: staged, not cut over

api, worker and chat still have their Railway Config File pointed at
`services/{api,worker,agent}/railway.json`. Railway reads those at deploy time, and
`railway config plan` refuses to manage a service that's still bound to one. So today
railway.ts is the reviewed target and the three railway.json files are what's live. Change
both together until the cutover below runs. The messaging and agent-ts json files are gone.
Neither service exists, and new services can't use `railway.json`.

### Using railway.ts

```
cd .railway && npm install && cd ..    # the SDK railway.ts imports, self-contained on purpose
railway link --project 12dffbd4-65bd-44f7-83b7-d30238c92892 --environment production
railway config plan                    # read-only diff against live
```

- Needs Railway CLI 5.42.1 or newer (tested on 5.54.0). Don't set `RAILWAY_IAC_TS_BIN`. The
  SDK 3.11 `railway-iac-ts` bin is a stub that always throws the upgrade error.
- Nothing applies railway.ts on push. GitHub deploys still build from the repo. Only
  `railway config apply` (or the `railwayapp/config` GitHub Action in
  `.github/workflows/railway-config.yml`) changes service settings. The Action is off until
  the cutover. Both jobs skip unless the repo variable `RAILWAY_IAC_ENABLED` is `true`.
  Its `RAILWAY_TOKEN` secret is a sysdesign production project token.
- Leaving something out means delete it, not leave it alone. That's why every live var is
  listed as `preserve()`.
- Never `apply` a plan you didn't expect. `railway config plan --detailed-exit-code` exits 0
  when there's nothing to do.

### Cutover runbook (not run yet)

Clearing the Config File is a live settings change. The service instances hold no start
command, healthcheck, restart policy or pre-deploy step of their own, so clearing it alone
would change how they deploy. In this order it's behavior-neutral.

1. Ship the railway-preview.py change first. It must stop pinning `railwayConfigFile` on
   cloned instances and must clear `preDeployCommand` on the cloned api. Step 2 puts the
   migration step on the production api instance, and previews clone that instance, so
   without this previews would start migrating the shared production database.
2. For api, worker and chat in production, copy each railway.json's values onto the service
   (start command, healthcheck path and timeout, restart policy, watch patterns, and api's
   pre-deploy command). The file still wins at deploy time, so this changes nothing yet.
3. Clear the Railway Config File on the three services.
4. `railway config plan --detailed-exit-code` must exit 0. Fix railway.ts until it does.
   Don't apply a non-empty plan to get there.
5. Delete `services/{api,worker,agent}/railway.json`.
6. Redeploy each service. Check the deploy is SUCCESS, the deploy logs are clean, and
   `https://sysdesign.thedefrag.ai/health` and `https://chat.thedefrag.ai/health` return 200.
7. Turn the Action on with `gh variable set RAILWAY_IAC_ENABLED --body true`. From then on
   every PR touching `.railway/` gets a plan comment and merging applies that plan.

`railway config migrate --apply` also clears the Config File, but it rebuilds railway.ts
from the json files alone. It drops watch patterns, restart policy, the pre-deploy step,
sources and redis. Don't use it here.

Redis auth is one `REDIS_PASSWORD` on the redis service (generated at provision time, not
managed by sync). api + worker consume it through Railway reference templates
(`${{redis.REDIS_PASSWORD}}` / `${{redis.RAILWAY_PRIVATE_DOMAIN}}`), so a rotation is
edit-one-place then redeploy.

### Gotcha: redis start command must run through a shell

The redis start command is

```
sh -c 'redis-server --requirepass "$REDIS_PASSWORD" --appendonly yes --dir /data'
```

The `sh -c` wrap is load-bearing. Railway exec's an image service's start command
directly, with no shell, so a bare `redis-server --requirepass $REDIS_PASSWORD` passes the
literal seven characters `$REDIS_PASSWORD` as the password (no expansion). Redis then
enforces that literal string while api + worker send the real 48-char rendered value, and
every connection fails with `WRONGPASS invalid username-password pair`. Wrapping in
`sh -c '...'` gives you the shell that expands `$REDIS_PASSWORD`. (The `bitnami/redis`
image sidesteps this by reading `REDIS_PASSWORD` from the env natively; plain
`redis:7-alpine` does not, so the flag plus a shell is required.)

### Gotcha: a custom domain needs the ownership TXT, not just the CNAME

Railway asks for two DNS records per custom domain, a `CNAME` to the assigned
`*.up.railway.app` target and a `TXT _railway-verify.<subdomain>` carrying a
`railway-verify=...` token. The CNAME alone routes traffic, but the cert sits in
`CERTIFICATE_STATUS_TYPE_VALIDATING_OWNERSHIP` until the TXT resolves; the edge keeps
serving the default `*.up.railway.app` wildcard cert in the meantime, so HTTPS to the
custom host fails SAN validation. Both records live in Cloudflare, DNS-only (proxied off),
so Railway's own Let's Encrypt challenge can reach the origin. Pull the exact token with
the `verificationDnsHost` / `verificationToken` fields on the custom domain's `status`.

### Gotcha: uvicorn behind Railway's proxy needs `--forwarded-allow-ips='*'`

The api start command (`.railway/railway.ts`, and `services/api/railway.json` until the
cutover) carries
`--proxy-headers --forwarded-allow-ips='*'`. Railway terminates TLS at its edge and forwards
plain HTTP to the container, so every request arrives with `X-Forwarded-Proto: https` but a
raw `http` scheme on the socket. Uvicorn only honors that header from IPs listed in
`--forwarded-allow-ips`, which defaults to `127.0.0.1`. Railway's proxy is not `127.0.0.1`
from inside the container, so without the flag uvicorn ignores the header and treats requests
as HTTP. That breaks any generated absolute URL, most visibly the `/mcp` mount: a request to
`/mcp` (no trailing slash) 307-redirects to `/mcp/` with an `http://` Location, and the
Managed Agents MCP client blocks the redirect (`server redirected to an insecure (non-HTTPS)
URL and was blocked`), so the digest agent's MCP tool never initializes and it silently falls
back to bash curl against the REST API.

This is exactly why the local (cloudflared tunnel) path worked while prod failed: `cloudflared`
connects to `localhost:8000`, so the socket peer IS `127.0.0.1`, which uvicorn trusts by
default. Same code, different peer IP, opposite result. `--forwarded-allow-ips='*'` is safe
here because the container is only reachable through Railway's proxy (no direct ingress).

## Migrations

The api runs migrations before it takes traffic, via its pre-deploy command

```
uv run --package sysdesign-core python packages/core/db/upgrade.py
```

The command is scoped to the production environment so PR preview environments never
migrate. In `.railway/railway.ts` that's the `isProd` guard. In `services/api/railway.json`,
still live until the cutover, it's nested under `environments.production`. Previews share the production
Railway database, and a PR branch carrying a new migration must not rewrite the
shared schema just by deploying a preview. See the preview section below for the
flip side of that hazard.

Railway runs `preDeployCommand` once, inside the freshly built image, before the new
version goes live. So a deploy can't serve code that expects a column the database doesn't
have yet, the schema is caught up first, or the deploy fails and the old version keeps
serving. It reads `DATABASE_URL` (the api's, the Railway TCP proxy, a direct connection that
supports the DDL a migration needs).

[migrate.py](../packages/core/db/migrate.py) is a ~50-line applier over dbmate's own file
format. dbmate is a Go binary and this image is a uv/Python build, so rather than wrangle
the binary into the image it reads the same `packages/core/db/migrations/*.sql` files and writes the same
`schema_migrations` table dbmate uses, with psycopg (already a dependency). dbmate stays the
local tool (`moon run core:migrate`, `NAME=... moon run core:new`), this is the prod applier, and the two are
interchangeable, `dbmate status` reads the table the runner wrote and reports every
migration applied. It's idempotent (already-applied versions skip), each migration commits
atomically, and it honors a `transaction:false` marker for the `CREATE INDEX CONCURRENTLY`
case.

Local dev is unchanged, keep using `moon run core:migrate`. A `transaction:false` migration must be
a single statement (Postgres won't run `CONCURRENTLY` inside an implicit transaction block).

## Deploys

Push to `main` deploys api + worker (Railway GitHub integration, PR environments off).
The api's deploy runs the migration step above first. Redis redeploys only when its
image/config changes. The project-scoped token in the repo-root `.env`
(`RAILWAY_PROJECT_TOKEN`) drives `railway` CLI status/logs/redeploys and the GraphQL calls
in `railway-env.py`. It cannot touch other projects.

Connecting a service to GitHub is the one thing the token cannot do (it's a user-identity
+ GitHub-app grant), so that step happens once in the dashboard.

### Provisioning the messaging gateway (one-time, in progress)

The codified surface is merged. The `messaging` service in `.railway/railway.ts` (build/run
plus a production pre-deploy step that applies the `msg_*` migration) sits behind
`PROVISION_MESSAGING`, next to the `messaging` entry in [railway-env.py](railway-env.py)'s
`SERVICES` + `MANIFEST` and the lazy id injection in `main`. It needs the IaC cutover first,
since plan won't run until then. What's left, in order.

1. **Create the service.** Flip `PROVISION_MESSAGING` to `true`, run `railway config plan`
   and check it shows only the `messaging` create, then `railway config apply`. If apply
   can't connect the GitHub source, create the service in the dashboard first (GitHub
   `dtothefp/to-the-moon`, root = repo root, named `messaging`) and plan again. There's no
   config path to set, new services can't use `railway.json`.
2. **Generate a public domain** if you want to reach `/ws` from outside (WebSocket upgrade
   works over the normal HTTPS domain; the `--proxy-headers --forwarded-allow-ips='*'` in
   the start command is already there for the proxy). Keep it private if only in-cluster.
3. **Record the id.** Copy the new service id into the repo-root `.env` as
   `RAILWAY_MESSAGING_SERVICE_ID=...` so the sync script stops skipping it.
4. **Push env vars.** `python3 infra/railway-env.py sync --dry` to preview
   (`DATABASE_URL` from `DATABASE_URL_RAILWAY`, `REDIS_URL` from the shared reference
   template), then drop `--dry` to push.
5. **Deploy.** It auto-deploys on the next push to `main`, or trigger one manually. The
   `preDeployCommand` runs `migrate.py` first, so the `msg_*` tables are created before the
   gateway starts. Healthcheck is `GET /health`.

Until step 1 happens, `railway-env.py` lists/syncs the four existing services and prints
`messaging: skipped (no service id ...)`, so the script stays valid and runnable now.

## PR preview environments (opt-in, label-gated)

Not every PR gets an environment. Add the **`preview`** label to a PR and
[.github/workflows/preview-env.yml](../.github/workflows/preview-env.yml) creates a Railway
environment named `pr-<number>` cloned from production, with api + worker pointed at the PR
branch and a fresh Railway-provided domain on the api (commented on the PR). Remove the
label, or close/merge the PR, and the environment is deleted. Further pushes to the branch
auto-deploy with no Action run, because the cloned deployment triggers track the branch.

Railway's native "PR environments" feature can't do this. It's a project-level toggle that
fires on every PR, with no per-PR or label-based gating (verified against the docs
2026-07-10, only enterprise "Focused PR environments" narrows anything, and by changed
services, not by PR). So the workflow drives the GraphQL API directly through
[railway-preview.py](railway-preview.py), which documents the exact mutation sequence
(environmentCreate with `sourceEnvironmentId` + `skipInitialDeploys`, deploymentTriggerUpdate
to the PR branch, serviceDomainCreate for the api, serviceInstanceDeployV2 per service,
environmentDelete on teardown).

### Token: this needs the WORKSPACE token, not the project token

The project-scoped token (`RAILWAY_PROJECT_TOKEN`) is scoped to the production environment.
Empirically (2026-07-10) it CAN run `environmentCreate`, but it can't read, retarget, or
delete the environment it just created, which is worse than useless (it strands an
environment only a broader token can remove). Environment management runs on a workspace
token instead, stored as

- the `RAILWAY_WORKSPACE_TOKEN` repo secret (what the Action uses), and
- `RAILWAY_WORKSPACE_TOKEN` in the repo-root `.env` for local runs of the script.

Rotating it means updating both. `railway-env.py` (env-var sync) stays on the
narrower project token; only preview create/teardown needs the wide one. Fork PRs never
receive the secret (GitHub's rule), so previews only work for branches pushed to this repo,
which is the intended scope.

### The shared-database hazard (read this before trusting a preview)

Preview environments clone production's env vars, including `DATABASE_URL`. There's no
preview database. That means

- **Migrations don't run in previews** (production-scoped `preDeployCommand`, above). A PR
  that adds a migration deploys preview code against the OLD shared schema, so endpoints
  touching the new column/table will 500 in the preview. That's the accepted trade-off;
  apply the migration locally to test it, the preview verifies everything else.
- **Preview writes are production writes.** A `demo` or `live` run started against a
  preview api inserts real rows into the shared Railway database. The worker's beat
  backstops also run (the matview refresh is idempotent and harmless, the unrated sweep
  stays inert while `RATING_MODEL` is unset in the cloned vars).
- Redis IS per-preview (each environment gets its own instance), so queues and SSE
  pub/sub don't cross between preview and prod.

If previews ever need real isolation, the move is a per-PR database on the same Railway
Postgres instance (a `CREATE DATABASE` in the preview workflow, dropped on merge), punted
for now.

### One-time setup already done (dashboard-free, for the record)

The `preview` GitHub label and the `RAILWAY_WORKSPACE_TOKEN` repo secret were created with
`gh` (2026-07-10). No Railway dashboard steps were needed. The cloned services reuse the
existing GitHub connection, so the "token can't connect GitHub repos" constraint never
bites; connecting the repo happened once for production and clones inherit it.

## Local testing with the Managed Agent (Cloudflare tunnel)

The Module 5 agent's sandbox runs in Anthropic's cloud, so it can't reach `localhost`. To
test the digest loop against a laptop dev server, a named Cloudflare tunnel publishes the
local API at a stable hostname the vault credential already allows.

- Hostname: `https://sysdesign-local.thedefrag.ai` -> `localhost:8000` (tunnel
  `sysdesign-local`, id `dd6113aa-...`, config in `~/.cloudflared/config.yml` on David's
  machine, created 2026-07-11 via `cloudflared tunnel login` + `create` + `route dns`).
- The vault credential's `allowed_hosts` includes this hostname, so the agent's
  `X-API-Key` is substituted toward it exactly like prod. Keep `services/managed-agents/vault/api-key.yaml`
  and the live credential in sync when hosts change.
- Run it with `cloudflared tunnel run sysdesign-local`. Nothing listens until the local
  API is up; the tunnel itself only ever holds an OUTBOUND connection to Cloudflare's
  edge, which is why it works behind NAT with no router config.
- Full local loop: local api + redis + worker running (dev container), digests migration
  applied locally, then `POST /digests {"base_url": "https://sysdesign-local.thedefrag.ai"}`
  against the LOCAL api. The worker babysits the session, the sandbox curls back in
  through the tunnel, and the digest row completes in the local database.
- The tunnel exposes the local API to the whole internet, not just Anthropic. Data routes
  all require `X-API-Key`, which is the thing making that acceptable. Never tunnel an
  unauthenticated service.
