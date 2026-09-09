"""Hosted catalog loader tests.

Every test runs with RBXSCOUT_CATALOG_CACHE_DIR pointed at a tmp_path so the
developer's real ~/.cache/rbxscout is never touched, and the module's
constants are rebound to match (module-level constants are resolved at import
time; tests rebind them explicitly for clarity).
"""

import pytest

import catalog_fetch


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RBXSCOUT_CATALOG_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(catalog_fetch, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(catalog_fetch, "CACHE_DB_PATH", tmp_path / "cache" / catalog_fetch.ASSET_DB)
    monkeypatch.setattr(
        catalog_fetch, "CACHE_STATE_PATH", tmp_path / "cache" / "catalog_state.json"
    )


def _seed_cache(tmp_path, *, newer_than_state: bool = False, valid: bool = True) -> None:
    """Write a plausible cache DB + state."""
    cache = catalog_fetch.CACHE_DB_PATH
    cache.parent.mkdir(parents=True, exist_ok=True)
    if valid:
        cache.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200_000)
    else:
        cache.write_bytes(b"garbage-not-sqlite")
    state = {
        "checked_at": 1_000_000,
        "remote": {"size": 200_016, "updated_at": "2026-09-09T00:00:00Z"},
    }
    if newer_than_state:
        # Cache mtime newer than the state's checked_at keeps _fresh-enough
        # semantics irrelevant; the state drives the decision.
        pass
    catalog_fetch._save_state(state)


def _fresh_state(monkeypatch, updated_at: str = "2026-09-09T00:00:00Z") -> None:
    """Make the state look like a metadata check just succeeded."""
    catalog_fetch._save_state(
        {"checked_at": catalog_fetch.time.time(), "remote": {"size": 200_016, "updated_at": updated_at}}
    )


def test_is_hosted_false_when_local_db_exists(monkeypatch, tmp_path):
    repo_db = tmp_path / "repo" / "rbx_scout.db"
    repo_db.parent.mkdir(parents=True)
    repo_db.write_bytes(b"x")
    monkeypatch.setattr(catalog_fetch, "APP_DIR", repo_db.parent)
    assert catalog_fetch.is_hosted() is False


def test_is_hosted_true_without_local_db(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog_fetch, "APP_DIR", tmp_path)  # empty dir
    monkeypatch.delenv("RBXSCOUT_LOCAL_DB", raising=False)
    assert catalog_fetch.is_hosted() is True


def test_is_hosted_local_override(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog_fetch, "APP_DIR", tmp_path)  # empty dir
    monkeypatch.setenv("RBXSCOUT_LOCAL_DB", "1")
    assert catalog_fetch.is_hosted() is False


def test_ensure_catalog_downloads_valid_sqlite(tmp_path, monkeypatch):
    blob = b"SQLite format 3\x00" + b"\x00" * 300_000
    monkeypatch.setattr(
        catalog_fetch, "release_asset_info",
        lambda timeout=15.0: {"size": len(blob), "updated_at": "2026-09-09T12:00:00Z"},
    )
    monkeypatch.setattr(catalog_fetch, "_get_bytes", lambda url, timeout: blob)

    path = catalog_fetch.ensure_catalog()

    assert path == catalog_fetch.CACHE_DB_PATH
    assert path.read_bytes() == blob
    state = catalog_fetch._load_state()
    assert state["remote"]["updated_at"] == "2026-09-09T12:00:00Z"


def test_ensure_catalog_refuses_non_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr(
        catalog_fetch, "release_asset_info",
        lambda timeout=15.0: {"size": 10, "updated_at": "2026-09-09T12:00:00Z"},
    )
    monkeypatch.setattr(catalog_fetch, "_get_bytes", lambda url, timeout: b"definitely not sqlite")

    with pytest.raises(catalog_fetch.CatalogFetchError, match="not a SQLite"):
        catalog_fetch.ensure_catalog()
    assert not catalog_fetch.CACHE_DB_PATH.exists()


def test_fresh_check_skips_metadata_request(tmp_path, monkeypatch):
    _seed_cache(tmp_path)
    _fresh_state(monkeypatch)

    def _boom(*args, **kwargs):
        raise AssertionError("metadata request should not fire within the throttle window")

    monkeypatch.setattr(catalog_fetch, "release_asset_info", _boom)
    assert catalog_fetch.ensure_catalog() == catalog_fetch.CACHE_DB_PATH


def test_unchanged_remote_skips_download(tmp_path, monkeypatch):
    _seed_cache(tmp_path)
    catalog_fetch._save_state(
        {
            "checked_at": 0.0,  # throttle window elapsed
            "remote": {"size": 200_016, "updated_at": "2026-09-09T00:00:00Z"},
        }
    )
    monkeypatch.setattr(
        catalog_fetch, "release_asset_info",
        lambda timeout=15.0: {"size": 200_016, "updated_at": "2026-09-09T00:00:00Z"},
    )

    def _boom(*args, **kwargs):
        raise AssertionError("download should not fire when remote is unchanged")

    monkeypatch.setattr(catalog_fetch, "_get_bytes", _boom)
    assert catalog_fetch.ensure_catalog() == catalog_fetch.CACHE_DB_PATH


def test_changed_remote_redownloads(tmp_path, monkeypatch):
    _seed_cache(tmp_path)
    catalog_fetch._save_state(
        {"checked_at": 0.0, "remote": {"size": 200_016, "updated_at": "2026-09-09T00:00:00Z"}}
    )
    blob = b"SQLite format 3\x00" + b"\x00" * 400_000
    monkeypatch.setattr(
        catalog_fetch, "release_asset_info",
        lambda timeout=15.0: {"size": len(blob), "updated_at": "2026-09-10T00:00:00Z"},
    )
    monkeypatch.setattr(catalog_fetch, "_get_bytes", lambda url, timeout: blob)

    assert catalog_fetch.ensure_catalog().read_bytes() == blob


def test_stale_cache_fallback(tmp_path):
    # Pure helper: no network involved. An invalid cache is never offered as
    # a fallback; a valid one is.
    _seed_cache(tmp_path, valid=False)
    assert (
        catalog_fetch.stale_cache_fallback(catalog_fetch.CatalogFetchError("x"))
        is None
    )

    _seed_cache(tmp_path, valid=True)
    assert (
        catalog_fetch.stale_cache_fallback(catalog_fetch.CatalogFetchError("x"))
        == catalog_fetch.CACHE_DB_PATH
    )


def test_corrupt_cache_is_redownloaded(tmp_path, monkeypatch):
    _seed_cache(tmp_path, valid=False)
    blob = b"SQLite format 3\x00" + b"\x00" * 250_000
    monkeypatch.setattr(
        catalog_fetch, "release_asset_info",
        lambda timeout=15.0: {"size": len(blob), "updated_at": "2026-09-09T13:00:00Z"},
    )
    monkeypatch.setattr(catalog_fetch, "_get_bytes", lambda url, timeout: blob)

    path = catalog_fetch.ensure_catalog()
    assert path.read_bytes() == blob


def test_catalog_counts_against_real_db():
    """Integration: the counter SQL runs against the real local catalog.

    Skipped automatically on Streamlit Cloud (no local DB there) — the
    hosted mode counts come from the downloaded asset with identical schema.
    """
    from pathlib import Path as _P

    repo_db = _P(catalog_fetch.APP_DIR) / "rbx_scout.db"
    if not repo_db.exists():
        pytest.skip("no local catalog in this checkout")
    counts = catalog_fetch.catalog_counts(str(repo_db))
    assert counts["games"] and counts["games"] > 0
    assert counts["target"] is not None and counts["target"] > 0
    assert counts["found_today"] is not None  # legitimately 0 late in the UTC day
    assert counts["last_sync"]
