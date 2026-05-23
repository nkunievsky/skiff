"""Persistent state: favorites, recents, saved connections, last paths.

Saved-connection profiles may carry a plaintext password (user-opted-in
auto-fill). Directory and file permissions are forced to 0700 / 0600.

A "location" dict has the shape:

    {"kind": "local",  "path": "..."}
    {"kind": "sftp",   "path": "...",
     "host": "...", "port": 22, "username": "...", "key_path": ""}
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from pathlib import Path


def _state_dir() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Skiff"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return (Path(appdata) if appdata else home) / "Skiff"
    return home / ".config" / "skiff"


def _legacy_state_files() -> list[Path]:
    """Pre-rename locations from when the app was called Forklift.
    Read these once on first launch if the new Skiff dir is empty."""
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "Forklift" / "state.json"]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else home
        return [base / "Forklift" / "state.json"]
    return [home / ".config" / "forklift" / "state.json"]


STATE_DIR = _state_dir()
STATE_FILE = STATE_DIR / "state.json"
MAX_RECENTS = 10

_lock = threading.Lock()


def _default_state() -> dict:
    return {
        "favorites": [],
        "recents": [],
        "connections": [],
        "last_paths": {},
    }


def _migrate_from_legacy() -> None:
    """If no Skiff state file exists yet but an old Forklift one does,
    copy it over so users keep their saved connections, favorites, etc.
    Best-effort — failures are silent (we'll just start fresh)."""
    if STATE_FILE.exists():
        return
    for old in _legacy_state_files():
        if not old.exists():
            continue
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, STATE_FILE)
            try:
                os.chmod(STATE_DIR, 0o700)
                os.chmod(STATE_FILE, 0o600)
            except OSError:
                pass
            return
        except OSError:
            continue


def load_state() -> dict:
    _migrate_from_legacy()
    if not STATE_FILE.exists():
        return _default_state()
    try:
        with STATE_FILE.open("r") as f:
            d = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _default_state()
    base = _default_state()
    if isinstance(d, dict):
        base.update({k: v for k, v in d.items() if k in base})
    return base


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # State holds plaintext passwords (user-opted-in). Lock down file modes.
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with _lock:
        with tmp.open("w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        tmp.replace(STATE_FILE)
        try:
            os.chmod(STATE_FILE, 0o600)
        except OSError:
            pass


def location_key(loc: dict) -> tuple:
    """Stable identity for dedup."""
    if loc.get("kind") == "local":
        return ("local", loc.get("path", ""))
    return (
        "sftp",
        loc.get("host", ""),
        int(loc.get("port", 22)),
        loc.get("username", ""),
        loc.get("path", ""),
    )


def add_recent(state: dict, loc: dict) -> None:
    key = location_key(loc)
    new = [loc] + [r for r in state["recents"] if location_key(r) != key]
    state["recents"] = new[:MAX_RECENTS]


def add_favorite(state: dict, loc: dict) -> None:
    key = location_key(loc)
    if any(location_key(f) == key for f in state["favorites"]):
        return
    state["favorites"].append(loc)


def remove_favorite(state: dict, loc: dict) -> None:
    key = location_key(loc)
    state["favorites"] = [
        f for f in state["favorites"] if location_key(f) != key
    ]


def is_favorite(state: dict, loc: dict) -> bool:
    key = location_key(loc)
    return any(location_key(f) == key for f in state["favorites"])


def save_connection(
    state: dict,
    *,
    host: str,
    port: int,
    username: str,
    key_path: str = "",
    password: str = "",
) -> None:
    """Insert/overwrite a connection profile.

    Empty `key_path` / `password` arguments preserve any existing values
    on a profile rather than wiping them — so callers that don't know
    the password (e.g. sidebar context) can still update the entry.
    """
    key = (host, int(port), username)
    existing = next(
        (c for c in state["connections"]
         if (c.get("host"), int(c.get("port", 22)), c.get("username")) == key),
        None,
    )
    entry = {
        "host": host,
        "port": int(port),
        "username": username,
        "key_path": key_path or (existing.get("key_path", "") if existing else ""),
        "password": password or (existing.get("password", "") if existing else ""),
    }
    new = [entry]
    new.extend(
        c for c in state["connections"]
        if (c.get("host"), int(c.get("port", 22)), c.get("username")) != key
    )
    state["connections"] = new


def remove_connection(state: dict, host: str, port: int, username: str) -> None:
    key = (host, int(port), username)
    state["connections"] = [
        c for c in state["connections"]
        if (c.get("host"), int(c.get("port", 22)), c.get("username")) != key
    ]


def remember_path(state: dict, backend_id: str, path: str) -> None:
    state["last_paths"][backend_id] = path


def last_path_for(state: dict, backend_id: str) -> str | None:
    return state["last_paths"].get(backend_id)
