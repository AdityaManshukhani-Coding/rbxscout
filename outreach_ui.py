#!/usr/bin/env python3
"""Standalone outreach dashboard — test before wiring into app.py.

Run:  streamlit run outreach_ui.py

Tabs:
  * Authorize queue — one click-able link per queued server (the human step)
  * Sent / waiting  — DMs out, polling for replies
  * Replied         — the whole point: owner, game, message, timestamp

Nothing here touches the main scout; this page reads/writes outreach.sqlite
only. No token needed until the runner actually sends.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import streamlit as st

import outreach

st.set_page_config(page_title="Outreach — UpScale", page_icon="📨",
                   layout="wide")
st.title("📨 Outreach")

DB = outreach.OUTREACH_DB
if "refresh" not in st.session_state:
    st.session_state.refresh = 0

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    return c

def _table(sql: str) -> list:
    c = _conn()
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()

st.caption(f"Ledger: `{DB.name}` — " + ", ".join(
    f"{k}={v}" for k, v in sorted(outreach.status_counts(_conn()).items())) or "empty")

tab_queue, tab_sent, tab_replied = st.tabs(["🔐 Authorize queue", "📤 Sent / waiting", "✅ Replied"])

with tab_queue:
    rows = _table("SELECT universe_id, game_name, guild_name, authorize_url, "
                  "last_error FROM outreach WHERE status IN ('queued','dead_invite')")
    if not rows:
        st.info("Nothing queued. Use `python outreach.py test --invite <link>` "
                "to queue your test server, then `python outreach.py run --once` "
                "so the runner fills the authorize links.")
    for r in rows:
        with st.container(border=True):
            st.markdown(f"**{r['game_name'] or 'Unknown game'}** · "
                        f"`{r['guild_name'] or 'unknown guild'}`")
            if r["status"] == "dead_invite":
                st.error(f"Invite did not resolve: {r['last_error']}")
            elif r["authorize_url"]:
                st.link_button("Open Discord authorize page", r["authorize_url"],
                               use_container_width=True)
                st.caption("Click Authorize on that page — the bot joins the "
                           "server and the runner picks it up on the next tick.")
            else:
                st.info("Authorize link pending — run the runner once with a token.")

with tab_sent:
    rows = _table("SELECT universe_id, game_name, guild_name, owner_name, "
                  "sent_at, last_message FROM outreach WHERE status='sent'")
    if not rows:
        st.info("No outbound DMs waiting on a reply yet.")
    for r in rows:
        with st.container(border=True):
            st.markdown(f"**{r['game_name'] or 'Unknown game'}** · "
                        f"to **{r['owner_name'] or 'owner'}** "
                        f"({r['guild_name'] or 'guild'}) · sent {r['sent_at']}")
            st.text(r["last_message"] or "(message body not stored)")

with tab_replied:
    rows = _table("SELECT universe_id, game_name, guild_name, owner_name, "
                  "replied_at, replied_text FROM outreach WHERE status='replied' "
                  "ORDER BY replied_at DESC")
    if not rows:
        st.info("No replies yet. This tab is the payoff — every owner who "
                "responds shows up here automatically.")
    for r in rows:
        with st.container(border=True,
                          **{"type": "success"} if False else {}):
            st.success(f"**{r['owner_name'] or 'Owner'}** replied about "
                       f"**{r['game_name'] or 'your game'}** · {r['replied_at']}")
            st.text(r["replied_text"] or "(empty message)")
            st.caption(f"Guild: {r['guild_name']} · universe {r['universe_id']}")

st.divider()
c1, c2 = st.columns(2)
if c1.button("🔄 Refresh", use_container_width=True):
    st.session_state.refresh += 1
    st.rerun()
if c2.button(f"Run one tick (needs token in discord_bot.token)",
             use_container_width=True):
    token = outreach._bot_token()
    if not token:
        st.error("No token found. Save it in discord_bot.token (gitignored).")
    else:
        with st.spinner("Runner tick…"):
            s = outreach.runner_tick(token, once=True)
        st.success("Tick done: " + ", ".join(f"{k}={v}" for k, v in s.items()))
        st.rerun()
