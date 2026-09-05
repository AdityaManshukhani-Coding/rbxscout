/**
 * RbxScout Cloudflare Worker.
 *
 * This Worker has two jobs:
 *
 * 1. Search proxy: mirrors the public Roblox omni-search endpoint so the
 *    keyword crawler can use Cloudflare's IP pool instead of the shared
 *    GitHub Actions runner IP range.
 * 2. Scheduler: every five minutes, dispatches the Hydrator workflow through
 *    GitHub's workflow_dispatch API. At UTC minute 00 and 30 it also
 *    dispatches Finder. GitHub's internal `schedule` triggers are disabled
 *    in this repository, so Cloudflare is the only automatic clock.
 *
 * The GitHub token is a Worker secret (`GITHUB_TOKEN`) and is never returned
 * in a response or written to logs.
 */

const UPSTREAM = "https://apis.roblox.com";
const RATE_LIMIT_PER_MINUTE = 120;
const RATE_LIMIT_WINDOW_MS = 60_000;

const GITHUB_OWNER = "AdityaManshukhani-Coding";
const GITHUB_REPO = "rbxscout";
const GITHUB_REF = "main";
const GITHUB_API_BASE = "https://api.github.com";
const GITHUB_API_VERSION = "2022-11-28";
const GITHUB_WORKFLOWS = {
  hydrator: "hydrator.yml",
  finder: "finder.yml",
};
const GITHUB_DISPATCH_ATTEMPTS = 3;
const GITHUB_RETRY_MAX_DELAY_MS = 30_000;
const GITHUB_RETRYABLE_STATUSES = new Set([408, 429, 500, 502, 503, 504]);

/** Simple per-isolate sliding-window limiter (per data-center, per client IP). */
const rateBuckets = new Map();

function clientIp(request) {
  return (
    request.headers.get("cf-connecting-ip") ||
    request.headers.get("x-forwarded-for") ||
    "unknown"
  );
}

function rateLimited(ip) {
  const now = Date.now();
  const bucket = rateBuckets.get(ip) || [];
  while (bucket.length && now - bucket[0] > RATE_LIMIT_WINDOW_MS) {
    bucket.shift();
  }
  if (bucket.length >= RATE_LIMIT_PER_MINUTE) {
    return true;
  }
  bucket.push(now);
  rateBuckets.set(ip, bucket);
  // Opportunistic cleanup so the map cannot grow unbounded.
  if (rateBuckets.size > 10_000) {
    for (const [key, stamps] of rateBuckets) {
      if (!stamps.length || now - stamps[stamps.length - 1] > RATE_LIMIT_WINDOW_MS) {
        rateBuckets.delete(key);
      }
    }
  }
  return false;
}

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
};

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function retryDelayMs(response, attempt) {
  const retryAfterSeconds = Number.parseInt(response.headers.get("Retry-After") || "", 10);
  if (Number.isFinite(retryAfterSeconds) && retryAfterSeconds >= 0) {
    return Math.min(GITHUB_RETRY_MAX_DELAY_MS, retryAfterSeconds * 1_000);
  }
  return Math.min(GITHUB_RETRY_MAX_DELAY_MS, 500 * (2 ** attempt));
}

async function dispatchWorkflow(workflow, env) {
  if (!env.GITHUB_TOKEN) {
    throw new Error("GITHUB_TOKEN Worker secret is not configured");
  }

  const url = `${GITHUB_API_BASE}/repos/${GITHUB_OWNER}/${GITHUB_REPO}`
    + `/actions/workflows/${workflow}/dispatches`;
  for (let attempt = 0; attempt < GITHUB_DISPATCH_ATTEMPTS; attempt += 1) {
    let response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers: {
          Accept: "application/vnd.github+json",
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          "Content-Type": "application/json",
          "User-Agent": "rbxscout-cloudflare-scheduler",
          "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
        body: JSON.stringify({ ref: GITHUB_REF }),
      });
    } catch (error) {
      if (attempt === GITHUB_DISPATCH_ATTEMPTS - 1) {
        throw new Error(`GitHub dispatch ${workflow} network failure: ${error}`);
      }
      await sleep(500 * (2 ** attempt));
      continue;
    }

    if (response.ok) {
      return response.status;
    }

    const detail = (await response.text()).slice(0, 500);
    if (!GITHUB_RETRYABLE_STATUSES.has(response.status)
      || attempt === GITHUB_DISPATCH_ATTEMPTS - 1) {
      throw new Error(`GitHub dispatch ${workflow} failed (${response.status}): ${detail}`);
    }
    await sleep(retryDelayMs(response, attempt));
  }

  throw new Error(`GitHub dispatch ${workflow} failed after retries`);
}

async function dispatchDueWorkflows(controller, env) {
  const scheduledAt = new Date(controller.scheduledTime || Date.now());
  const minute = scheduledAt.getUTCMinutes();
  const due = [
    ["hydrator", GITHUB_WORKFLOWS.hydrator],
  ];

  // Finder runs on the same five-minute clock, but only at :00 and :30 UTC.
  // Keeping one Cloudflare trigger avoids two independent scheduler clocks.
  if (minute % 30 === 0) {
    due.push(["finder", GITHUB_WORKFLOWS.finder]);
  }

  const results = await Promise.allSettled(
    due.map(async ([name, workflow]) => {
      const status = await dispatchWorkflow(workflow, env);
      console.log(`dispatched ${name} via workflow_dispatch (${status})`);
    }),
  );

  const failures = [];
  for (let i = 0; i < results.length; i += 1) {
    const result = results[i];
    if (result.status === "rejected") {
      // Keep the other dispatch independent: a GitHub API failure for Finder
      // must not prevent the Hydrator dispatch in the same five-minute tick.
      const message = String(result.reason);
      console.error(`dispatch ${due[i][0]} failed: ${message}`);
      failures.push(`${due[i][0]}: ${message}`);
    }
  }
  if (failures.length) {
    // Make the Cron Event visibly failed after retries are exhausted. The next
    // five-minute tick remains the recovery path and will dispatch again.
    throw new Error(failures.join("; "));
  }
}

export default {
  async scheduled(controller, env, ctx) {
    // Keep the scheduled handler itself tiny; network waiting happens in the
    // waitUntil task and does not count against the free CPU time in the same
    // way as JavaScript execution. A rejected task marks this Cron Event
    // failed in Cloudflare's history after the built-in dispatch retries.
    ctx.waitUntil(dispatchDueWorkflows(controller, env));
  },

  async fetch(request) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          ...CORS_HEADERS,
          "Access-Control-Allow-Methods": "GET, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type",
        },
      });
    }
    if (request.method !== "GET") {
      return new Response("method not allowed", { status: 405, headers: CORS_HEADERS });
    }
    if (url.pathname !== "/search-api/omni-search") {
      return new Response("not found", { status: 404, headers: CORS_HEADERS });
    }
    if (!url.searchParams.get("searchQuery")) {
      return new Response("missing searchQuery", { status: 400, headers: CORS_HEADERS });
    }
    if (rateLimited(clientIp(request))) {
      return new Response("rate limited", { status: 429, headers: CORS_HEADERS });
    }

    // Forward the query string as-is (searchQuery, pageType, sessionId).
    const upstreamUrl = UPSTREAM + url.pathname + url.search;

    const headers = new Headers({ Accept: "application/json" });
    headers.set("User-Agent", request.headers.get("user-agent") || "rbxscout-proxy");
    // Deliberately no cookies/auth forwarded — public endpoint only.

    let response;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      response = await fetch(upstreamUrl, { headers, cf: { cacheTtl: 0 } });
      if (response.status !== 429 || attempt === 1) break;
      await new Promise((resolve) => setTimeout(resolve, 1200 * (attempt + 1)));
    }

    const body = await response.arrayBuffer();
    const out = new Headers(CORS_HEADERS);
    out.set("Content-Type", response.headers.get("content-type") || "application/json");
    out.set("X-Rbxscout-Proxy", "cf-worker");
    out.set("X-Rbxscout-Upstream-Status", String(response.status));
    return new Response(body, { status: response.status, headers: out });
  },
};
