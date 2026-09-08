/**
 * RbxScout Deno Deploy mirror — the ~20-line paste-in-browser overflow proxy.
 *
 * Deploy (no CLI, no config files needed):
 *   1. Create a free account at https://dash.deno.com (sign in with GitHub).
 *   2. "New Playground" → name it e.g. rbx-search-mirror.
 *   3. Delete the sample code, paste this whole file, Save & Deploy.
 *   4. Note the URL: https://<project-name>.deno.dev
 *
 * Wire it in as OVERFLOW ONLY (MIRRORS.md): append it to the repo variable
 * RBXSCOUT_SEARCH_PROXY_URLS AFTER the Cloudflare Worker, BEFORE `direct`:
 *
 *   RBXSCOUT_SEARCH_PROXY_URLS=
 *     https://rbx-search-proxy.<you>.workers.dev,
 *     https://rbx-search-mirror.deno.dev,
 *     direct
 *
 * The pool in scout_core.py needs no code changes: entries are tried in
 * order, each is benched independently after 3 consecutive failures, and a
 * benched mirror costs nothing. Free tier: 1M requests/month + 100 GiB
 * egress — overflow traffic (~5% of ≈58k req/day) uses well under 1% of it.
 *
 * Behavior mirrors the Cloudflare Worker's fetch handler: only the public
 * cookieless omni-search GET route, one 429 retry, no auth forwarded, plus
 * a per-isolate sliding-window limiter so stray public traffic cannot burn
 * the monthly quota.
 */

const UPSTREAM = "https://apis.roblox.com";
const RATE_LIMIT_PER_MINUTE = 120;
const RATE_LIMIT_WINDOW_MS = 60_000;

/** Per-isolate sliding-window limiter (per client IP) — quota protection. */
const rateBuckets = new Map<string, number[]>();

function clientIp(req: Request): string {
  return req.headers.get("cf-connecting-ip")
    || req.headers.get("x-forwarded-for")
    || "unknown";
}

function rateLimited(ip: string): boolean {
  const now = Date.now();
  const bucket = rateBuckets.get(ip) ?? [];
  while (bucket.length && now - bucket[0] > RATE_LIMIT_WINDOW_MS) bucket.shift();
  if (bucket.length >= RATE_LIMIT_PER_MINUTE) return true;
  bucket.push(now);
  rateBuckets.set(ip, bucket);
  if (rateBuckets.size > 10_000) {
    for (const [key, stamps] of rateBuckets) {
      if (!stamps.length || now - stamps[stamps.length - 1] > RATE_LIMIT_WINDOW_MS) {
        rateBuckets.delete(key);
      }
    }
  }
  return false;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

Deno.serve(async (req: Request) => {
  const url = new URL(req.url);

  if (req.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
      },
    });
  }
  if (req.method !== "GET") {
    return new Response("method not allowed", { status: 405 });
  }
  if (url.pathname !== "/search-api/omni-search") {
    return new Response("not found", { status: 404 });
  }
  if (!url.searchParams.get("searchQuery")) {
    return new Response("missing searchQuery", { status: 400 });
  }
  if (rateLimited(clientIp(req))) {
    return new Response("rate limited", { status: 429 });
  }

  // Forward the query string as-is (searchQuery, pageType, sessionId,
  // pageToken). Deliberately no cookies/auth forwarded — public endpoint only.
  let res = await fetch(UPSTREAM + url.pathname + url.search, {
    headers: { Accept: "application/json", "User-Agent": "rbxscout-proxy" },
  });
  if (res.status === 429) {
    await sleep(1200);
    res = await fetch(UPSTREAM + url.pathname + url.search, {
      headers: { Accept: "application/json", "User-Agent": "rbxscout-proxy" },
    });
  }

  return new Response(res.body, {
    status: res.status,
    headers: {
      "Content-Type": res.headers.get("content-type") ?? "application/json",
      "Access-Control-Allow-Origin": "*",
      "X-Rbxscout-Proxy": "deno-deploy",
      "X-Rbxscout-Upstream-Status": String(res.status),
    },
  });
});
