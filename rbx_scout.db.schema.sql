-- RbxScout catalog schema reference.
-- The catalog itself (rbx_scout.db) is the `rbx_scout.db` asset of the
-- rolling Release tag `catalog-latest` in this repo — pull it with:
--   python db_sync.py pull
-- Regenerate this file with:
--   sqlite3 rbx_scout.db .schema > rbx_scout.db.schema.sql

CREATE TABLE ccu_history (
                    universe_id INTEGER NOT NULL,
                    ts          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    ccu         INTEGER,
                    PRIMARY KEY (universe_id, ts)
                );;

CREATE TABLE contact_diagnostics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    universe_id INTEGER NOT NULL,
                    diagnostics_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );;

CREATE TABLE game_analytics (
                    universe_id      INTEGER PRIMARY KEY,
                    root_place_id    INTEGER,
                    title            TEXT,
                    ccu              INTEGER,
                    peak_ccu         INTEGER,
                    visits           INTEGER,
                    favorites        INTEGER,
                    genre            TEXT,
                    creator_name     TEXT,
                    creator_type     TEXT,
                    creator_id       INTEGER,
                    icon_url         TEXT,
                    has_discord      BOOLEAN,
                    discord_url      TEXT,
                    twitter_url      TEXT,
                    status           TEXT,
                    found_via        TEXT,
                    has_social_links BOOLEAN,
                    contacts_checked_at TIMESTAMP,
                    last_updated     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                , description TEXT, contact_schema_version INTEGER DEFAULT 0, tier INTEGER DEFAULT 0, prev_tier INTEGER DEFAULT 0, tier_since TIMESTAMP, blowup_flag INTEGER DEFAULT 0, blowup_at TIMESTAMP);;

CREATE TABLE keyword_crawl_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    next_index INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );;

CREATE TABLE place_map (
                    place_id    INTEGER PRIMARY KEY,
                    universe_id INTEGER NOT NULL,
                    resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );;

CREATE TABLE rolimons_catalog (
                    place_id    INTEGER PRIMARY KEY,
                    name        TEXT,
                    playing     INTEGER DEFAULT 0,
                    icon_url    TEXT,
                    cached_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );;

CREATE TABLE scan_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    finished_at TIMESTAMP,
                    status TEXT,
                    source_count INTEGER DEFAULT 0,
                    metrics_count INTEGER DEFAULT 0,
                    contacts_attempted INTEGER DEFAULT 0,
                    contacts_completed INTEGER DEFAULT 0,
                    contact_errors INTEGER DEFAULT 0,
                    error TEXT
                , candidate_count INTEGER DEFAULT 0, matched_count INTEGER DEFAULT 0, candidate_limit INTEGER DEFAULT 0, min_visits INTEGER DEFAULT 0, min_ccu INTEGER DEFAULT 0);;

CREATE INDEX idx_ga_blowup ON game_analytics(blowup_flag);;

CREATE INDEX idx_ga_ccu ON game_analytics(ccu);;

CREATE INDEX idx_ga_tier ON game_analytics(tier);;

CREATE INDEX idx_ga_visits ON game_analytics(visits);;

CREATE INDEX idx_roli_playing ON rolimons_catalog(playing);
