"""Shared state coordinator across multiple windows.

A single dict (loaded from disk) is shared by reference across all
MainWindow instances. The StateBus broadcasts notifications so each
window's sidebar can repopulate when another window mutates state.
"""
from __future__ import annotations

import uuid

from PyQt5.QtCore import QObject, pyqtSignal


class StateBus(QObject):
    state_changed = pyqtSignal()         # any persisted state mutated
    new_window_requested = pyqtSignal()  # File > New Window
    window_closed = pyqtSignal(str)      # window_id
    bring_all_to_front_requested = pyqtSignal()  # Window > Bring All to Front


def new_window_id() -> str:
    return uuid.uuid4().hex


def add_window_record(state: dict, *, title: str = "", bg_color: str = "") -> dict:
    state.setdefault("windows", [])
    record = {
        "id": new_window_id(),
        "title": title,
        "bg_color": bg_color,
        "geometry": [],
        "panes": [{"path": ""}, {"path": ""}],
    }
    state["windows"].append(record)
    return record


def find_window(state: dict, window_id: str) -> dict | None:
    for w in state.get("windows", []):
        if w.get("id") == window_id:
            return w
    return None


def remove_window(state: dict, window_id: str) -> None:
    state["windows"] = [
        w for w in state.get("windows", []) if w.get("id") != window_id
    ]


def migrate_legacy_local_paths(state: dict) -> None:
    """Pull old state['last_paths']['local_panes'] into a window record so
    existing users keep their per-pane local cwds on first multi-window run."""
    if state.get("windows"):
        return
    old = state.get("last_paths", {}).pop("local_panes", None) if isinstance(
        state.get("last_paths"), dict
    ) else None
    if not old:
        return
    record = add_window_record(state)
    record["panes"] = [{"path": p or ""} for p in old]
