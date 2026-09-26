/**
 * UpScale Scouting Tool Cloudflare Worker.
 *
 * This Worker has two jobs:
 *
 * 1. Search proxy: mirrors the public Roblox omni-search endpoint so the
 *    keyword crawler can use Cloudflare's IP pool instead of the shared
 *    GitHub Actions runner IP range.
 * 2. Scheduler: every five minutes, dispatches the Hydrator workflow through
 *    GitHub's workflow_dispatch API. At :15/:45 it also dispatches the
 *    Expander (Atlas Dev harvest + discovery-queue drain). GitHub's internal
 *    `schedule` triggers are disabled in this repository, so Cloudflare is
 *    the only automatic clock.
 * 3. Doorbell (GET /app): the permanent link users bookmark. If the Codespace
 *    dashboard is healthy it 302s straight into it; if the codespace has
 *    idle-stopped, it dispatches the codespace_wake.yml workflow (Actions
 *    starts the machine + relaunches Streamlit) and serves a splash page that
 *    auto-refreshes until the app is live.
 *
 * The GitHub token is a Worker secret (`GITHUB_TOKEN`) and is never returned
 * in a response or written to logs.
 */

const UPSTREAM = "https://apis.roblox.com";
// Path-mirror allowlist for the Atlas harvester: GET requests to
// /atlas/<target-url> are relayed to atlasdev.gg through Cloudflare's IP
// pool (the harvester's `direct` leg can be throttled from datacenter IP
// ranges). Host-locked, method-locked, and rate-limited like omni-search.
const ATLAS_MIRROR_HOST = "atlasdev.gg";
const ATLAS_RATE_LIMIT_PER_MINUTE = 30;
const ATLAS_RATE_LIMIT_WINDOW_MS = 60_000;
const atlasBuckets = new Map();

function atlasRateLimited(ip) {
  const now = Date.now();
  const bucket = atlasBuckets.get(ip) || [];
  while (bucket.length && now - bucket[0] > ATLAS_RATE_LIMIT_WINDOW_MS) {
    bucket.shift();
  }
  if (bucket.length >= ATLAS_RATE_LIMIT_PER_MINUTE) {
    return true;
  }
  bucket.push(now);
  atlasBuckets.set(ip, bucket);
  return false;
}
const RATE_LIMIT_PER_MINUTE = 120;
const RATE_LIMIT_WINDOW_MS = 60_000;

const GITHUB_OWNER = "AdityaManshukhani-Coding";
const GITHUB_REPO = "rbxscout";
const GITHUB_REF = "main";
const GITHUB_API_BASE = "https://api.github.com";
const GITHUB_API_VERSION = "2022-11-28";
const GITHUB_WORKFLOWS = {
  hydrator: "hydrator.yml",
  expander: "expander.yml",
  wake: "codespace_wake.yml",
};
const GITHUB_DISPATCH_ATTEMPTS = 3;
const GITHUB_RETRY_MAX_DELAY_MS = 30_000;
const GITHUB_RETRYABLE_STATUSES = new Set([408, 429, 500, 502, 503, 504]);

// Keep-alive: ping the hosted Streamlit dashboard every 11 hours so
// Community Cloud never hibernates it (its idle timeout is 12 h; a ping
// every 11 h always lands before the timer expires). Fires on the tick in
// hours divisible by 11 — 00:00, 11:00 and 22:00 UTC, 3 pings/day instead
// of the ~144 a every-10-min ping would cost. The URL is a Worker VARIABLE
// (not a secret — it is public anyway) so it can be managed without
// redeploying code:
//   npx wrangler vars put DASHBOARD_KEEPALIVE_URL
// Leave the variable unset to disable the ping entirely.
const DASHBOARD_KEEPALIVE_URL_KEY = "DASHBOARD_KEEPALIVE_URL";

// Doorbell (24/7 concierge for the Codespace dashboard). GET /app is the ONE
// permanent link handed to users. Behavior:
//   app healthy   -> 302 straight to the dashboard (users never notice)
//   app down      -> dispatch codespace_wake.yml (Actions starts the machine,
//                    relaunches Streamlit) + serve a splash page that
//                    auto-refreshes every 20 s until the app is up
// The first user of the day waits ~1-2 min on the splash; everyone after
// walks straight in. The machine still closes itself via GitHub's idle
// timeout (60 min) — nothing here pings the codespace, so no hours burn
// while nobody is using the app.
// Guards keep scrapers or a stuck refresh from burning quota: at most one
// wake dispatch per 2 min and 10 per hour (per isolate — best effort).
const DOORBELL_PATH = "/app";
const APP_PUBLIC_URL_KEY = "APP_PUBLIC_URL";
const FALLBACK_APP_URL_KEY = "FALLBACK_APP_URL";
const WAKE_COOLDOWN_MS = 2 * 60_000;
const WAKE_MAX_PER_HOUR = 10;

let lastWakeDispatchMs = 0;
const wakeTimestamps = [];

async function handleDoorbell(request, env, url) {
  if (rateLimited(clientIp(request))) {
    return new Response("rate limited", { status: 429, headers: CORS_HEADERS });
  }

  const appUrl = String(env[APP_PUBLIC_URL_KEY] || "").replace(/\/+$/, "");
  const fallbackUrl = String(
    env[FALLBACK_APP_URL_KEY] || env[DASHBOARD_KEEPALIVE_URL_KEY] || "",
  ).replace(/\/+$/, "");

  // Probe the app's health endpoint first: when it answers, the machine is up
  // and the user should not even see the splash page. The health endpoint does
  // not create a Streamlit session, so this probe never counts as app usage
  // and never keeps the machine alive artificially.
  if (appUrl) {
    try {
      const probe = await fetch(`${appUrl}/_stcore/health`, {
        signal: AbortSignal.timeout(5_000),
        cf: { cacheTtl: 0 },
      });
      if (probe.ok) {
        return Response.redirect(appUrl, 302);
      }
    } catch {
      // Down (machine stopped or app dead) — fall through to the wake path.
    }
  }

  const now = Date.now();
  while (wakeTimestamps.length && now - wakeTimestamps[0] > 3_600_000) {
    wakeTimestamps.shift();
  }
  const canWake =
    appUrl &&
    now - lastWakeDispatchMs >= WAKE_COOLDOWN_MS &&
    wakeTimestamps.length < WAKE_MAX_PER_HOUR;
  if (canWake) {
    lastWakeDispatchMs = now;
    wakeTimestamps.push(now);
    try {
      await dispatchWorkflow(GITHUB_WORKFLOWS.wake, env);
      console.log("doorbell: wake workflow dispatched");
    } catch (error) {
      // The splash page refresh loop is the recovery path: the next hit
      // re-probes and re-dispatches once the cooldown has passed.
      console.error(`doorbell wake dispatch failed: ${error}`);
    }
  }

  const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="20">
<title>Studio Scouts — waking up</title>
<style>
 body{font-family:system-ui,sans-serif;background:#0e1117;color:#fafafa;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}
 main{text-align:center;max-width:34rem;padding:2rem;line-height:1.5}
 h1{font-size:1.35rem;font-weight:600}
 .dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:#4ade80;animation:p 1s infinite alternate;margin-right:.5rem}
 @keyframes p{to{opacity:.25}}
 a{color:#7dd3fc}
</style></head><body><main>
 <h1><span class="dot"></span>Waking the dashboard…</h1>
 <p>The app powers down automatically when nobody has used it for a while,
 which keeps it free to run. It is restarting now — this page retries every
 20 seconds and will drop you straight in, usually within 1–2 minutes.</p>
 <p><b>Keep this tab open.</b></p>
 ${fallbackUrl ? `<p>In a hurry? A lightweight backup copy is always online:
 <a href="${fallbackUrl}">open the backup app</a>.</p>` : ""}
</main></body></html>`;
  return new Response(html, {
    status: canWake || wakeTimestamps.length ? 200 : 503,
    headers: { "Content-Type": "text/html; charset=utf-8", ...CORS_HEADERS },
  });
}

function keepAliveDue(scheduledAt) {
  return scheduledAt.getUTCHours() % 11 === 0 && scheduledAt.getUTCMinutes() < 5;
}

async function pingDashboard(env) {
  const url = env[DASHBOARD_KEEPALIVE_URL_KEY];
  if (!url) return; // not configured — keep-alive disabled
  try {
    const response = await fetch(url, {
      method: "GET",
      headers: { "User-Agent": "upscalescouting-keepalive" },
      // Streamlit's HTTP layer answers health checks without rendering a
      // session; a plain GET (no _stcore stream upgrade) is enough to count
      // as app traffic for hibernation purposes.
      redirect: "follow",
    });
    console.log(`dashboard keep-alive ping -> ${response.status}`);
  } catch (error) {
    // Never let a dashboard hiccup fail the Cron Event: the workflow
    // dispatches above are the job that matters.
    console.error(`dashboard keep-alive ping failed: ${error}`);
  }
}

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
          "User-Agent": "upscalescouting-cloudflare-scheduler",
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

  // Expander (Atlas Dev harvest + discovery-queue drain, EXPANSION_PILOT.md)
  // gets the odd clean minute :15/:45 — its own 30-min slot. The Actions
  // concurrency group still serializes any overlap with a long hydrator run
  // still holding the mutex at :15.
  if (minute === 15 || minute === 45) {
    due.push(["expander", GITHUB_WORKFLOWS.expander]);
  }

  const results = await Promise.allSettled(
    due.map(async ([name, workflow]) => {
      const status = await dispatchWorkflow(workflow, env);
      console.log(`dispatched ${name} via workflow_dispatch (${status})`);
    }),
  );

  // On the 11-hour boundary ticks, also ping the hosted dashboard so it
  // never sleeps. Fire-and-forget: its outcome is logged, never thrown.
  if (keepAliveDue(scheduledAt)) {
    await pingDashboard(env);
  }

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
    if (url.pathname === DOORBELL_PATH) {
      return handleDoorbell(request, env, url);
    }
    // Atlas path-mirror: /https://atlasdev.gg/analyze?... — the full target
    // URL is the path suffix plus this request's query string. The target
    // body is returned verbatim; only atlasdev.gg is allowed.
    if (url.pathname.startsWith("/http")) {
      if (rateLimited(clientIp(request))) {
        return new Response("rate limited", { status: 429, headers: CORS_HEADERS });
      }
      if (atlasRateLimited(clientIp(request))) {
        return new Response("rate limited", { status: 429, headers: CORS_HEADERS });
      }
      const target = `${url.pathname.slice(1)}${url.search}`;
      let targetUrl;
      try {
        targetUrl = new URL(target);
      } catch {
        return new Response("bad target", { status: 400, headers: CORS_HEADERS });
      }
      if (targetUrl.hostname !== ATLAS_MIRROR_HOST) {
        return new Response("forbidden host", { status: 403, headers: CORS_HEADERS });
      }
      const upstreamHeaders = new Headers({
        Accept: "text/html,*/*",
        "Accept-Language": "en",
        "User-Agent": request.headers.get("user-agent") || "upscalescouting-atlas-mirror",
      });
      const upstream = await fetch(targetUrl, {
        headers: upstreamHeaders,
        cf: { cacheTtl: 0 },
        redirect: "follow",
      });
      const body = await upstream.arrayBuffer();
      const out = new Headers(CORS_HEADERS);
      out.set("Content-Type", upstream.headers.get("content-type") || "text/html; charset=utf-8");
      out.set("X-Rbxscout-Proxy", "cf-worker-atlas");
      out.set("X-Rbxscout-Upstream-Status", String(upstream.status));
      return new Response(body, { status: upstream.status, headers: out });
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
    headers.set("User-Agent", request.headers.get("user-agent") || "upscalescouting-proxy");
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
