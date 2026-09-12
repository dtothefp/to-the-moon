import { defineRailway, github, preserve, project, redis, service, volume } from "railway/iac";

// Railway IaC for the sysdesign project (replaces the deprecated services/*/railway.json).
// Plan from the repo root with `railway config plan`. See infra/README.md for the setup.
//
// STAGED. api, worker and chat still have their Railway Config File pointed at
// services/{api,worker,agent}/railway.json, so `railway config plan` refuses them until the
// cutover in infra/README.md runs. Until then this file is the reviewed target and the
// railway.json files are what Railway reads. Keep the two in sync.
//
// Values below mirror live (imported with `railway config pull`) plus the build/deploy
// settings that only exist in railway.json today. Secrets are preserve(), never literals.
// railway-env.py still writes the values.

// Not provisioned. Flipping one to true makes `railway config apply` CREATE that service.
const PROVISION_MESSAGING = false;
const PROVISION_AGENT_TS = false;

const REPO = "dtothefp/to-the-moon";

// Railway terminates TLS at its edge, so uvicorn must trust X-Forwarded-Proto from its proxy.
// See the uvicorn gotcha in infra/README.md.
const uvicorn = (pkg: string, app: string) =>
  `uv run --package ${pkg} uvicorn ${app} --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'`;

const restart = { restartPolicyType: "ON_FAILURE", restartPolicyMaxRetries: 10 } as const;

export default defineRailway((ctx) => {
  // Previews clone production and share its database, so migrations only run in production.
  const isProd = ctx.isEnvironment("production");
  const toTheMoon = github(REPO, { checkSuites: false, rootDirectory: "/" });

  const redisDatabase = redis("redis", { region: "us-west2" });
  // The helper defaults to redis:8. Pin what's live so an apply can never swap the broker's image.
  redisDatabase.image = "redis:7-alpine";
  redisDatabase.source = { type: "image", image: "redis:7-alpine" };
  // sh -c is load-bearing, see the redis gotcha in infra/README.md.
  redisDatabase.deploy = { startCommand: "sh -c 'redis-server --requirepass \"$REDIS_PASSWORD\" --appendonly yes --dir /data'" };
  redisDatabase.networking = { privateNetworkEndpoint: "redis-44401968" };
  const redisVolume = volume("redis-volume-CXMm", {
    alerts: { usage: { "100": {}, "80": {}, "95": {} } },
    allowOnlineResize: true,
    region: "us-west2",
    sizeMB: 50000,
  });

  const api = service("api", {
    source: toTheMoon,
    build: { watchPatterns: ["services/api/**", "packages/core/**", "packages/task-contract/**", "pyproject.toml", "uv.lock"] },
    start: uvicorn("sysdesign-api", "api.main:app"),
    ...(isProd ? { preDeploy: "uv run --package sysdesign-core python packages/core/db/upgrade.py" } : {}),
    healthcheck: "/health",
    healthcheckTimeout: 120,
    deploy: restart,
    replicas: { "us-west2": 1 },
    domains: ["sysdesign.thedefrag.ai"],
    env: {
      ANTHROPIC_API_KEY: preserve(),
      CELERY_BROKER_URL: preserve(),
      CELERY_RESULT_BACKEND: preserve(),
      DATABASE_URL: preserve(),
      DATABASE_URL_SUPABASE_ROLLBACK: preserve(),
      EMBEDDING_MODEL: preserve(),
      GROQ_API_KEY: preserve(),
      LANGSMITH_API_KEY: preserve(),
      LANGSMITH_ENDPOINT: preserve(),
      LANGSMITH_PROJECT: preserve(),
      LANGSMITH_TRACING: preserve(),
      OPENAI_API_KEY: preserve(),
      REDIS_URL: preserve(),
      SYSDESIGN_API_KEY: preserve(),
      SYSDESIGN_PUBLIC_URL: preserve(),
    },
  });

  const worker = service("worker", {
    source: toTheMoon,
    build: { watchPatterns: ["services/worker/**", "packages/core/**", "packages/task-contract/**", "pyproject.toml", "uv.lock"] },
    start: "uv run --package sysdesign-worker celery -A worker.celery_app worker --beat --loglevel INFO --concurrency 4",
    deploy: restart,
    replicas: { "us-west2": 1 },
    env: {
      ANTHROPIC_API_KEY: preserve(),
      APIFY_API_KEY: preserve(),
      CELERY_BROKER_URL: preserve(),
      CELERY_RESULT_BACKEND: preserve(),
      DATABASE_URL: preserve(),
      DATABASE_URL_SUPABASE_ROLLBACK: preserve(),
      EMBEDDING_MODEL: preserve(),
      GROQ_API_KEY: preserve(),
      LANGSMITH_API_KEY: preserve(),
      LANGSMITH_ENDPOINT: preserve(),
      LANGSMITH_PROJECT: preserve(),
      LANGSMITH_TRACING: preserve(),
      OPENAI_API_KEY: preserve(),
      RATING_MODEL: preserve(),
      REDIS_URL: preserve(),
    },
  });

  // The Module 7 chat AGENT. Code lives in services/agent, the service is named chat.
  const chat = service("chat", {
    source: toTheMoon,
    build: { watchPatterns: ["services/agent/**", "packages/core/**", "pyproject.toml", "uv.lock"] },
    start: uvicorn("sysdesign-agent", "agent.server:app"),
    healthcheck: "/health",
    healthcheckTimeout: 120,
    deploy: restart,
    replicas: { "us-west2": 1 },
    domains: ["chat.thedefrag.ai"],
    networking: { privateNetworkEndpoint: "agentic-sysdesign" },
    env: {
      ANTHROPIC_API_KEY: preserve(),
      LANGSMITH_API_KEY: preserve(),
      LANGSMITH_ENDPOINT: preserve(),
      LANGSMITH_PROJECT: preserve(),
      LANGSMITH_TRACING: preserve(),
      SYSDESIGN_API_KEY: preserve(),
      SYSDESIGN_API_URL: preserve(),
    },
  });

  // Messaging drill gateway (code in services/chat). Named messaging so it can't collide with chat.
  // Env comes from railway-env.py's messaging entry after the service exists.
  const messaging = () =>
    service("messaging", {
      source: toTheMoon,
      build: { watchPatterns: ["services/chat/**", "packages/core/**", "pyproject.toml", "uv.lock"] },
      start: uvicorn("sysdesign-chat", "chat.main:app"),
      ...(isProd ? { preDeploy: "uv run --package sysdesign-core python packages/core/db/migrate.py" } : {}),
      healthcheck: "/health",
      healthcheckTimeout: 120,
      deploy: restart,
      replicas: { "us-west2": 1 },
    });

  // TypeScript port of the chat agent. Builds against its own package.json, see its README.
  const agentTs = () =>
    service("agent-ts", {
      source: github(REPO, { checkSuites: false, rootDirectory: "/services/agent-ts" }),
      build: { buildCommand: "npm ci && npm run build", watchPatterns: ["services/agent-ts/**"] },
      start: "npm run start",
      healthcheck: "/health",
      healthcheckTimeout: 120,
      deploy: restart,
      replicas: { "us-west2": 1 },
    });

  return project("sysdesign", {
    resources: [
      redisDatabase,
      redisVolume,
      api,
      worker,
      chat,
      ...(PROVISION_MESSAGING ? [messaging()] : []),
      ...(PROVISION_AGENT_TS ? [agentTs()] : []),
    ],
  });
});
