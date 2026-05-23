"""Left-rail sidebar: Favorites, Recent, Saved Connections."""
from __future__ import annotations

import os

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

KIND_FAVORITE = "favorite"
KIND_RECENT = "recent"
KIND_CONNECTION = "connection"

_HOME = os.path.expanduser("~")


def _shorten_local(path: str) -> str:
    if path == _HOME:
        return "~"
    if path.startswith(_HOME + os.sep):
        return "~" + path[len(_HOME):]
    return path


def _format_location(loc: dict) -> str:
    if loc.get("kind") == "local":
        return _shorten_local(loc.get("path", ""))
    user = loc.get("username", "")
    host = loc.get("host", "")
    port = loc.get("port", 22)
    path = loc.get("path", "") or "~"
    suffix = "" if port == 22 else f":{port}"
    return f"{user}@{host}{suffix}:{path}"


def _group_label(loc: dict) -> str:
    """Group key shown as the section header."""
    if loc.get("kind") == "local":
        return "Local"
    user = loc.get("username", "")
    host = loc.get("host", "")
    port = loc.get("port", 22)
    suffix = "" if port == 22 else f":{port}"
    return f"{user}@{host}{suffix}" if user else f"{host}{suffix}"


def _leaf_label(loc: dict) -> str:
    """Per-row label inside a group — just the path."""
    if loc.get("kind") == "local":
        return _shorten_local(loc.get("path", ""))
    return loc.get("path", "") or "~"


def _format_connection(conn: dict) -> str:
    user = conn.get("username", "")
    host = conn.get("host", "")
    port = conn.get("port", 22)
    suffix = "" if port == 22 else f":{port}"
    return f"{user}@{host}{suffix}"


class Sidebar(QTreeWidget):
    """Activate a location → tells main window to navigate the active pane.

    Right-click → contextual remove actions for favorites and connections.
    """

    location_activated = pyqtSignal(dict)        # location dict
    connection_activated = pyqtSignal(dict)      # connection profile
    favorite_remove_requested = pyqtSignal(dict)
    connection_remove_requested = pyqtSignal(dict)
    recent_clear_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setIndentation(14)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setFocusPolicy(Qt.NoFocus)
        self.setExpandsOnDoubleClick(False)
        self.setMinimumWidth(180)

        self.itemActivated.connect(self._on_activated)
        self.itemClicked.connect(self._on_activated)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def populate(self, state: dict) -> None:
        self.clear()
        self._add_section("Favorites", state.get("favorites", []),
                          KIND_FAVORITE)
        self._add_section("Recent", state.get("recents", []),
                          KIND_RECENT)
        self._add_connections(state.get("connections", []))

    def _add_section(self, title: str, items: list, kind: str) -> None:
        header = QTreeWidgetItem([title])
        header.setFlags(Qt.ItemIsEnabled)
        f = header.font(0)
        f.setBold(True)
        header.setFont(0, f)
        self.addTopLevelItem(header)
        header.setExpanded(True)
        if not items:
            empty = QTreeWidgetItem(["  (none)"])
            empty.setFlags(Qt.ItemIsEnabled)
            empty.setForeground(0, self.palette().mid().color())
            header.addChild(empty)
            return

        # Group entries by host (or "Local") preserving original order.
        groups: dict[str, tuple[QTreeWidgetItem, list]] = {}
        order: list[str] = []
        for loc in items:
            label = _group_label(loc)
            if label not in groups:
                grp = QTreeWidgetItem([label])
                grp.setFlags(Qt.ItemIsEnabled)
                gf = grp.font(0)
                gf.setItalic(True)
                grp.setFont(0, gf)
                grp.setForeground(0, self.palette().mid().color())
                header.addChild(grp)
                grp.setExpanded(True)
                groups[label] = (grp, [])
                order.append(label)
            grp, _list = groups[label]
            child = QTreeWidgetItem([_leaf_label(loc)])
            child.setData(0, Qt.UserRole, {"_kind": kind, "data": loc})
            child.setToolTip(0, _format_location(loc))
            grp.addChild(child)

    def _add_connections(self, conns: list) -> None:
        header = QTreeWidgetItem(["Saved connections"])
        header.setFlags(Qt.ItemIsEnabled)
        f = header.font(0)
        f.setBold(True)
        header.setFont(0, f)
        self.addTopLevelItem(header)
        header.setExpanded(True)
        if not conns:
            empty = QTreeWidgetItem(["  (none)"])
            empty.setFlags(Qt.ItemIsEnabled)
            empty.setForeground(0, self.palette().mid().color())
            header.addChild(empty)
            return
        for c in conns:
            child = QTreeWidgetItem([_format_connection(c)])
            child.setData(0, Qt.UserRole, {"_kind": KIND_CONNECTION,
                                            "data": c})
            child.setToolTip(0, _format_connection(c))
            header.addChild(child)

    # ----- interaction --------------------------------------------------------

    def _on_activated(self, item: QTreeWidgetItem, _col: int = 0) -> None:
        d = item.data(0, Qt.UserRole)
        if not d:
            return
        kind = d.get("_kind")
        payload = d.get("data") or {}
        if kind == KIND_CONNECTION:
            self.connection_activated.emit(payload)
        else:
            self.location_activated.emit(payload)

    def _on_context_menu(self, pos) -> None:
        item = self.itemAt(pos)
        if item is None:
            return
        d = item.data(0, Qt.UserRole)
        menu = QMenu(self)
        if d and d.get("_kind") == KIND_FAVORITE:
            act = menu.addAction("Remove from favorites")
            act.triggered.connect(
                lambda: self.favorite_remove_requested.emit(d["data"])
            )
        elif d and d.get("_kind") == KIND_CONNECTION:
            act = menu.addAction("Forget this connection")
            act.triggered.connect(
                lambda: self.connection_remove_requested.emit(d["data"])
            )
        else:
            # On the "Recent" header
            if item.text(0) == "Recent":
                act = menu.addAction("Clear recent")
                act.triggered.connect(self.recent_clear_requested.emit)
            else:
                return
        menu.exec_(self.viewport().mapToGlobal(pos))
