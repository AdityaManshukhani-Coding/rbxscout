/**
 * RbxScout search proxy — Cloudflare Worker.
 *
 * Mirrors the Roblox omni-search endpoint so the keyword crawler can reach
 * it from Cloudflare's IP pool instead of the runner's (GitHub Actions
 * runners share a small egress range and get 429-throttled). Mirrors only
 * this one public, cookieless GET route; nothing else can be reached
 * through it.
 *
 *   GET /search-api/omni-search?searchQuery=KW&pageType=all&sessionId=...
 *     -> proxied to https://apis.roblox.com/search-api/omni-search?...
 *
 * Hardening:
 * - Only exact-path GETs are forwarded; everything else gets 404.
 * - The forwarded request carries no cookies or auth headers (the endpoint
 *   is public; the scout's .ROBLOSECURITY cookie is scoped to *.roblox.com
 *   and is never sent to a third-party host anyway).
 * - Per-IP rate limit (default 120 req/min) so the worker can't be abused
 *   as a public proxy, and Roblox's own limits shield the origin.
 * - Retries one time on 429 from the origin, then forwards the status.
 */

const UPSTREAM = "https://apis.roblox.com";
const RATE_LIMIT_PER_MINUTE = 120;
const RATE_LIMIT_WINDOW_MS = 60_000;

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

export default {
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
    for (let attempt = 0; attempt < 2; attempt++) {
      response = await fetch(upstreamUrl, { headers, cf: { cacheTtl: 0 } });
      if (response.status !== 429 || attempt === 1) break;
      await new Promise((r) => setTimeout(r, 1200 * (attempt + 1)));
    }

    const body = await response.arrayBuffer();
    const out = new Headers(CORS_HEADERS);
    out.set("Content-Type", response.headers.get("content-type") || "application/json");
    out.set("X-Rbxscout-Proxy", "cf-worker");
    out.set("X-Rbxscout-Upstream-Status", String(response.status));
    return new Response(body, { status: response.status, headers: out });
  },
};
