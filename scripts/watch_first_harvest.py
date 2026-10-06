#!/usr/bin/env python3
"""One-shot watcher for the first 3h-cadence cloud Atlas harvest.

Sleeps until the atlas_last_run + 3h window is open, dispatches expander.yml,
polls the run to completion, then writes the harvest summary lines to
logs/first_3h_harvest_check.txt. Exits 0 when the run succeeds, 1 otherwise.
The GitHub token comes from the environment (db_sync.gh_token), never hardcoded.
"""

import io
import json
import re
import sqlite3
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timezone

sys.path.insert(0, ".")
import catalog_fetch  # noqa: E402
import db_sync  # noqa: E402

REPO = f"{catalog_fetch.GITHUB_OWNER}/{catalog_fetch.GITHUB_REPO}"
OUT = "logs/first_3h_harvest_check.txt"
THROTTLE_S = 3 * 3600


def token():
    tok = db_sync.gh_token()
    if not tok:
        raise SystemExit("no GitHub token in environment (RBXSCOUT_GITHUB_TOKEN)")
    return tok


def hdr(tok):
    return {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {tok}"}


def api(path, tok, method="GET", data=None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/{path}", headers=hdr(tok), method=method
    )
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
        return json.loads(body) if body else {}


def main():
    tok = token()
    lines = []

    def note(msg):
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        lines.append(f"[{stamp}] {msg}")
        print(lines[-1], flush=True)

    note("watcher started; reading atlas_last_run from local store")
    conn = sqlite3.connect("file:rbx_scout.db?mode=ro", uri=True)
    row = conn.execute(
        "SELECT last_universe_id FROM scan_pointers WHERE id='atlas_last_run'"
    ).fetchone()
    conn.close()
    if not row or row[0] is None:
        note("no atlas_last_run pointer found — dispatching immediately")
    last = float(row[0]) if row and row[0] is not None else 0.0
    fire_at = last + THROTTLE_S
    now = time.time()
    if now < fire_at:
        wait = fire_at - now
        note(f"window opens {datetime.fromtimestamp(fire_at, timezone.utc):%H:%M:%SZ} — sleeping {wait/60:.0f} min")
        time.sleep(wait + 120)  # 2 min margin
    else:
        note("window already open")

    note("dispatching expander run")
    api("actions/workflows/expander.yml/dispatches", tok, method="POST", data={"ref": "main"})
    time.sleep(45)

    run = None
    for _ in range(40):  # up to ~40 min of polling
        runs = api("actions/workflows/expander.yml/runs?per_page=3", tok)["workflow_runs"]
        for cand in runs:
            if cand["event"] == "workflow_dispatch" and cand["status"] in ("in_progress", "queued", "completed"):
                run = cand
                break
        if run and run["status"] == "completed":
            break
        time.sleep(60)
    if not run:
        note("ERROR: never saw the dispatched run")
        raise SystemExit(1)
    note(f"run #{run['run_number']}: {run['status']}/{run['conclusion']}")

    zdata = urllib.request.urlopen(
        urllib.request.Request(run["logs_url"], headers=hdr(tok)), timeout=180
    ).read()
    pat = re.compile(r"atlas dev |queue drain|queue depth|catalog  |Expansion complete|validated proxies|harvesting", re.I)
    for name in zipfile.ZipFile(io.BytesIO(zdata)).namelist():
        if not name.endswith("expand.txt"):
            continue
        for line in zipfile.ZipFile(io.BytesIO(zdata)).read(name).decode("utf-8", "replace").splitlines():
            text = line.split("Z ", 1)[-1].strip()
            if pat.search(text):
                lines.append(text)
    verdict = "SUCCESS" if run["conclusion"] == "success" else "FAILURE"
    lines.append(f"VERDICT: {verdict}")
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    raise SystemExit(0 if run["conclusion"] == "success" else 1)


if __name__ == "__main__":
    main()
