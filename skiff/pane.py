"""Single file pane: location bar, file tree, preview, drag-drop."""
from __future__ import annotations

import json
import os
import posixpath
import stat as stat_mod
import uuid
from datetime import datetime

from PyQt5.QtCore import QEvent, QMimeData, QThread, QTimer, QUrl, Qt, pyqtSignal
from PyQt5.QtGui import QDesktopServices, QKeySequence
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QAction,
    QApplication,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QShortcut,
    QStyle,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .connection import ConnectCoordinator, ConnectionRequest
from .fs import Backend, FSEntry, LocalBackend, SFTPBackend
from .preview import PreviewRequest, PreviewWidget, is_image

REMOTE_OPEN_CACHE = os.path.expanduser("~/.cache/skiff/remote_open")

# (key, header label, default-visible). Name is index 0 and always visible.
# Order here is the column order in the tree.
COLUMNS: list[tuple[str, str, bool]] = [
    ("name",        "Name",        True),
    ("size",        "Size",        True),
    ("modified",    "Modified",    True),
    ("type",        "Type",        False),
    ("permissions", "Permissions", False),
    ("owner",       "Owner",       False),
    ("created",     "Created",     False),
]
COL_KEYS = [c[0] for c in COLUMNS]
COL_LABELS = [c[1] for c in COLUMNS]
COL_DEFAULTS = {c[0]: c[2] for c in COLUMNS}
COL_DEFAULT_WIDTHS = {
    "name": 320, "size": 80, "modified": 140,
    "type": 80, "permissions": 110, "owner": 100, "created": 140,
}


class _RemoteOpenWorker(QThread):
    """Download a remote file to local cache, then signal its path."""

    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, backend: Backend, src: str, dst: str):
        super().__init__()
        self.backend = backend
        self.src = src
        self.dst = dst

    def run(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.dst), exist_ok=True)
            with self.backend.open_read(self.src) as r, open(self.dst, "wb") as w:
                while True:
                    buf = r.read(256 * 1024)
                    if not buf:
                        break
                    w.write(buf)
            self.finished_ok.emit(self.dst)
        except Exception as e:
            self.failed.emit(str(e))

MIME_TYPE = "application/x-skiff-paths"

# Pane registry, keyed by per-pane uuid. Lets the drop side find the source.
PANE_REGISTRY: dict[str, "FilePane"] = {}

# In-app clipboard for Copy/Cut/Paste between panes and windows.
# Shape: {"backend": Backend, "paths": [str], "op": "copy" | "cut"}
APP_FILE_CLIPBOARD: dict = {"backend": None, "paths": [], "op": "copy"}


class _EntryItem(QTreeWidgetItem):
    """Sorts directories above files regardless of order, then by column."""

    def __lt__(self, other: "QTreeWidgetItem") -> bool:
        tree = self.treeWidget()
        if tree is None:
            return super().__lt__(other)
        a: FSEntry | None = self.data(0, Qt.UserRole)
        b: FSEntry | None = other.data(0, Qt.UserRole)
        if a is None or b is None:
            return super().__lt__(other)

        # Directories always win the top, in either ascending or descending
        # mode. Qt inverts the result for descending, so we flip our return
        # value to keep dirs pinned.
        if a.is_dir != b.is_dir:
            ascending = (
                tree.header().sortIndicatorOrder() == Qt.AscendingOrder
            )
            return a.is_dir if ascending else not a.is_dir

        col = tree.sortColumn()
        key = COL_KEYS[col] if 0 <= col < len(COL_KEYS) else "name"
        if key == "size":
            return a.size < b.size
        if key == "modified":
            return a.mtime < b.mtime
        if key == "created":
            return a.ctime < b.ctime
        if key == "type":
            return _ext_of(a).casefold() < _ext_of(b).casefold()
        if key == "permissions":
            return (a.mode or 0) < (b.mode or 0)
        if key == "owner":
            return (a.owner or "").casefold() < (b.owner or "").casefold()
        return a.name.casefold() < b.name.casefold()


def _ext_of(e: FSEntry) -> str:
    if e.is_dir:
        return ""
    return os.path.splitext(e.name)[1].lstrip(".").lower()


def _fmt_mode(m: int) -> str:
    if not m:
        return ""
    if stat_mod.S_ISDIR(m):
        t = "d"
    elif stat_mod.S_ISLNK(m):
        t = "l"
    else:
        t = "-"
    perms = ""
    for shift in (6, 3, 0):
        perms += "r" if m & (4 << shift) else "-"
        perms += "w" if m & (2 << shift) else "-"
        perms += "x" if m & (1 << shift) else "-"
    return t + perms


class FileTree(QTreeWidget):
    """QTreeWidget with custom drag/drop wired to our mime format."""

    drop_received = pyqtSignal(str, list, str)  # source_pane_id, paths, target_dir

    def __init__(self, pane: "FilePane"):
        super().__init__()
        self.pane = pane
        self.setColumnCount(len(COLUMNS))
        self.setHeaderLabels(COL_LABELS)
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setSortingEnabled(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAlternatingRowColors(True)

        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)

        header = self.header()
        # Interactive on every column → user can drag any border to resize.
        for i in range(len(COLUMNS)):
            header.setSectionResizeMode(i, QHeaderView.Interactive)
        header.setStretchLastSection(False)
        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.customContextMenuRequested.connect(self._on_header_menu)
        for i, key in enumerate(COL_KEYS):
            self.setColumnHidden(i, not COL_DEFAULTS[key])
            header.resizeSection(i, COL_DEFAULT_WIDTHS.get(key, 100))
        header.sectionResized.connect(self._on_section_resized)

    def _on_header_menu(self, pos) -> None:
        from PyQt5.QtWidgets import QMenu
        menu = QMenu(self)
        for i, (key, label, _) in enumerate(COLUMNS):
            if key == "name":
                continue
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(not self.isColumnHidden(i))
            act.triggered.connect(
                lambda checked, idx=i: self._toggle_column(idx, checked)
            )
        menu.exec_(self.header().mapToGlobal(pos))

    def _toggle_column(self, idx: int, visible: bool) -> None:
        self.setColumnHidden(idx, not visible)
        self.pane.column_visibility_changed.emit(self.pane.visible_columns())

    def _on_section_resized(self, _idx: int, _old: int, _new: int) -> None:
        # Debounce — header fires sectionResized for every pixel during a
        # drag. Save 300 ms after the user stops.
        self.pane.schedule_save_column_widths()

    # --- drag side -------------------------------------------------------
    def mimeTypes(self):
        return [MIME_TYPE, "text/uri-list"]

    def mimeData(self, items):
        paths = []
        for it in items:
            entry = it.data(0, Qt.UserRole)
            if entry is not None:
                paths.append(entry.path)
        payload = json.dumps({"pane_id": self.pane.uid, "paths": paths}).encode()
        m = QMimeData()
        m.setData(MIME_TYPE, payload)
        return m

    # --- drop side -------------------------------------------------------
    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasFormat(MIME_TYPE) or md.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        md = event.mimeData()
        if md.hasFormat(MIME_TYPE) or md.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def _drop_target_dir(self, pos) -> str:
        """If the cursor is over a folder row, drop INTO that folder.
        Otherwise drop into the pane's current directory."""
        item = self.itemAt(pos)
        if item is not None:
            entry = item.data(0, Qt.UserRole)
            if entry is not None and entry.is_dir:
                return entry.path
        return self.pane.cwd

    def dropEvent(self, event):
        md = event.mimeData()
        target_dir = self._drop_target_dir(event.pos())
        if md.hasFormat(MIME_TYPE):
            try:
                data = json.loads(bytes(md.data(MIME_TYPE)).decode())
            except Exception:
                event.ignore()
                return
            src_id = data.get("pane_id")
            paths = data.get("paths") or []
            if not paths:
                event.ignore()
                return
            # Same-pane drops: only allow when dropping onto a folder that's
            # different from the file's current parent. Otherwise it's a no-op.
            if src_id == self.pane.uid and target_dir == self.pane.cwd:
                event.ignore()
                return
            self.drop_received.emit(src_id, paths, target_dir)
            event.acceptProposedAction()
            return
        if md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            if paths:
                self.drop_received.emit("__external__", paths, target_dir)
                event.acceptProposedAction()
                return
        event.ignore()


class FilePane(QWidget):
    """Owns a Backend + cwd. Emits transfer_requested when drops arrive."""

    transfer_requested = pyqtSignal(object, list, object, str)
    # source_backend, paths, dest_backend, dest_dir

    status = pyqtSignal(str)
    location_changed = pyqtSignal(dict)        # current location dict
    focused = pyqtSignal(object)               # this pane
    connect_requested = pyqtSignal(object)     # target_path or None
    connection_established = pyqtSignal(dict)  # connection profile (incl. password)
    favorite_toggle_requested = pyqtSignal(dict)
    column_visibility_changed = pyqtSignal(dict)  # {key: visible}
    column_widths_changed = pyqtSignal(dict)      # {key: pixels}
    reconnect_requested = pyqtSignal(str)         # target_path to navigate to after reconnect

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.uid = uuid.uuid4().hex
        PANE_REGISTRY[self.uid] = self

        self.backend: Backend = LocalBackend()
        self.cwd: str = self.backend.home()
        self._open_workers: list[_RemoteOpenWorker] = []
        self._coord: ConnectCoordinator | None = None

        self._save_widths_timer = QTimer(self)
        self._save_widths_timer.setSingleShot(True)
        self._save_widths_timer.setInterval(300)
        self._save_widths_timer.timeout.connect(self._emit_column_widths)

        self._build_ui()
        self.refresh()

    # ---------- UI -----------------------------------------------------------

    def _build_ui(self) -> None:
        style = self.style()
        toolbar = QToolBar()
        toolbar.setMovable(False)
        toolbar.setIconSize(toolbar.iconSize() * 0.85)

        self.act_up = QAction(style.standardIcon(QStyle.SP_FileDialogToParent), "Up", self)
        self.act_up.triggered.connect(self.go_up)
        toolbar.addAction(self.act_up)

        self.act_refresh = QAction(style.standardIcon(QStyle.SP_BrowserReload), "Refresh", self)
        self.act_refresh.triggered.connect(self.refresh)
        toolbar.addAction(self.act_refresh)

        toolbar.addSeparator()

        self.act_newdir = QAction(style.standardIcon(QStyle.SP_FileDialogNewFolder), "New Folder", self)
        self.act_newdir.triggered.connect(self.new_folder)
        toolbar.addAction(self.act_newdir)

        self.act_delete = QAction(style.standardIcon(QStyle.SP_TrashIcon), "Delete", self)
        self.act_delete.triggered.connect(self.delete_selection)
        toolbar.addAction(self.act_delete)

        toolbar.addSeparator()

        self.act_favorite = QAction(style.standardIcon(QStyle.SP_DialogApplyButton), "Add to favorites", self)
        self.act_favorite.setCheckable(True)
        self.act_favorite.triggered.connect(self._on_favorite_clicked)
        toolbar.addAction(self.act_favorite)

        toolbar.addSeparator()

        self.act_connect = QAction(style.standardIcon(QStyle.SP_ComputerIcon), "Connect SSH…", self)
        self.act_connect.triggered.connect(lambda: self.connect_requested.emit(None))
        toolbar.addAction(self.act_connect)

        self.act_disconnect = QAction(style.standardIcon(QStyle.SP_DialogCloseButton), "Disconnect", self)
        self.act_disconnect.triggered.connect(self.disconnect_remote)
        self.act_disconnect.setEnabled(False)
        toolbar.addAction(self.act_disconnect)

        self.host_label = QLabel(self.backend.display_name())
        self.host_label.setObjectName("hostLabel")
        toolbar.addSeparator()
        toolbar.addWidget(self.host_label)

        self.path_edit = QLineEdit(self.cwd)
        self.path_edit.returnPressed.connect(self._path_entered)
        self.path_edit.setObjectName("pathEdit")

        self.tree = FileTree(self)
        self.tree.itemDoubleClicked.connect(self._on_double_clicked)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.tree.drop_received.connect(self._on_drop_received)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self.tree.installEventFilter(self)
        self.path_edit.installEventFilter(self)

        # Esc → focus the path bar with text selected for quick path entry.
        esc_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        esc_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        esc_shortcut.activated.connect(self._jump_to_path_edit)

        # Cmd+R / Ctrl+R refreshes the focused pane.
        refresh_shortcut = QShortcut(QKeySequence("Ctrl+R"), self)
        refresh_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        refresh_shortcut.activated.connect(self.refresh)

        self.preview = PreviewWidget()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(toolbar)
        layout.addWidget(self.path_edit)
        layout.addWidget(self.tree, stretch=1)
        layout.addWidget(self.preview)

    # ---------- focus tracking ----------------------------------------------

    def eventFilter(self, obj, event):
        if event.type() == QEvent.FocusIn:
            self.focused.emit(self)
        return super().eventFilter(obj, event)

    # ---------- backend / navigation ----------------------------------------

    def set_backend(self, backend: Backend, cwd: str | None = None) -> None:
        old = self.backend
        self.backend = backend
        self.cwd = cwd if cwd is not None else backend.home()
        self.host_label.setText(backend.display_name())
        self.act_disconnect.setEnabled(not backend.is_local)
        self.refresh()
        if old is not backend and not old.is_local:
            try:
                old.close()
            except Exception:
                pass

    def disconnect_remote(self) -> None:
        if self.backend.is_local:
            return
        self.set_backend(LocalBackend())

    def connect_with(self, req: ConnectionRequest, target_path: str | None = None) -> None:
        """Bypass the dialog and connect with given parameters."""
        self.status.emit(f"Connecting to {req.username}@{req.host}…")
        self._pending_req = req
        self._coord = ConnectCoordinator(self, req, target_path=target_path)
        self._coord.succeeded.connect(self._on_connect_ok)
        self._coord.failed.connect(self._on_connect_failed)
        self._coord.start()

    def _on_connect_ok(self, backend: SFTPBackend, target_path) -> None:
        self.status.emit(f"Connected: {backend.display_name()}")
        target = target_path or backend.home()
        self.set_backend(backend, target)
        req = getattr(self, "_pending_req", None)
        self.connection_established.emit({
            "host": backend.host,
            "port": backend.port,
            "username": backend.username,
            "key_path": (req.key_path if req else "") or "",
            "password": (req.password if req else "") or "",
        })

    def _on_connect_failed(self, msg: str) -> None:
        self.status.emit("Connect failed")
        QMessageBox.critical(self, "Connection failed", msg)

    def navigate_to(self, path: str) -> None:
        self.cwd = path
        self.path_edit.setText(path)
        self.refresh()

    def go_up(self) -> None:
        parent = self.backend.parent(self.cwd)
        if parent and parent != self.cwd:
            self.navigate_to(parent)

    def go_local(self, path: str | None = None) -> None:
        self.set_backend(LocalBackend(), path)

    def current_location(self) -> dict:
        if self.backend.is_local:
            return {"kind": "local", "path": self.cwd}
        sftp = self.backend  # type: ignore[assignment]
        return {
            "kind": "sftp",
            "path": self.cwd,
            "host": sftp.host,
            "port": sftp.port,
            "username": sftp.username,
            "key_path": "",
        }

    def set_favorite_state(self, is_fav: bool) -> None:
        self.act_favorite.setChecked(is_fav)
        self.act_favorite.setText(
            "Remove from favorites" if is_fav else "Add to favorites",
        )

    def _on_favorite_clicked(self) -> None:
        self.favorite_toggle_requested.emit(self.current_location())

    def _jump_to_path_edit(self) -> None:
        self.path_edit.setFocus()
        self.path_edit.selectAll()

    # ---------- auto-reconnect on dropped SFTP session ----------------------

    def _maybe_auto_reconnect(self, exc: Exception) -> bool:
        """If an op failed because the SFTP transport is dead, ask the
        window to rebuild the connection at the current cwd. Returns True
        when the caller should bail (we've taken over)."""
        if self.backend.is_local or not isinstance(self.backend, SFTPBackend):
            return False
        try:
            alive = self.backend.transport.is_active()
        except Exception:
            alive = False
        looks_like_drop = (
            not alive
            or isinstance(exc, EOFError)
            or "ssh session not active" in str(exc).lower()
            or "socket is closed" in str(exc).lower()
        )
        if not looks_like_drop:
            return False
        self.status.emit(
            f"Connection to {self.backend.display_name()} lost — reconnecting…"
        )
        self.reconnect_requested.emit(self.cwd)
        return True

    # ---------- column visibility -------------------------------------------

    def visible_columns(self) -> dict:
        return {
            COL_KEYS[i]: not self.tree.isColumnHidden(i)
            for i in range(len(COL_KEYS))
        }

    def apply_column_visibility(self, vis: dict) -> None:
        for i, key in enumerate(COL_KEYS):
            if key == "name":
                continue
            if key in vis:
                self.tree.setColumnHidden(i, not bool(vis[key]))

    def current_column_widths(self) -> dict:
        header = self.tree.header()
        # Hidden columns report width 0; skip them so we don't clobber a
        # previously-saved width when the user toggles a column off and on.
        return {
            COL_KEYS[i]: header.sectionSize(i)
            for i in range(len(COL_KEYS))
            if not self.tree.isColumnHidden(i)
        }

    def apply_column_widths(self, widths: dict) -> None:
        header = self.tree.header()
        # Block our own sectionResized → schedule_save loop while we apply.
        header.blockSignals(True)
        try:
            for i, key in enumerate(COL_KEYS):
                w = widths.get(key)
                if isinstance(w, int) and w > 0:
                    header.resizeSection(i, w)
        finally:
            header.blockSignals(False)

    def schedule_save_column_widths(self) -> None:
        self._save_widths_timer.start()

    def _emit_column_widths(self) -> None:
        self.column_widths_changed.emit(self.current_column_widths())

    def _path_entered(self) -> None:
        target = self.path_edit.text().strip()
        if not target:
            return
        try:
            if not self.backend.stat_is_dir(target):
                QMessageBox.warning(self, "Navigate", f"Not a directory: {target}")
                self.path_edit.setText(self.cwd)
                return
        except Exception as e:
            if self._maybe_auto_reconnect(e):
                # Reconnect will land us at the prior cwd; ignore this attempt.
                self.path_edit.setText(self.cwd)
                return
            QMessageBox.warning(self, "Navigate", str(e))
            self.path_edit.setText(self.cwd)
            return
        self.navigate_to(target)

    def refresh(self) -> None:
        self.path_edit.setText(self.cwd)
        try:
            entries = self.backend.list(self.cwd)
        except Exception as e:
            if self._maybe_auto_reconnect(e):
                return
            QMessageBox.warning(self, "List failed", f"{self.cwd}: {e}")
            return
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        style = self.style()
        dir_icon = style.standardIcon(QStyle.SP_DirIcon)
        file_icon = style.standardIcon(QStyle.SP_FileIcon)
        for e in entries:
            item = _EntryItem()
            item.setText(0, e.name)
            item.setIcon(0, dir_icon if e.is_dir else file_icon)
            item.setText(1, "" if e.is_dir else _fmt_size(e.size))
            item.setText(2, _fmt_time(e.mtime))
            item.setText(3, "folder" if e.is_dir else (_ext_of(e) or "file"))
            item.setText(4, _fmt_mode(e.mode))
            item.setText(5, e.owner or "")
            item.setText(6, _fmt_time(e.ctime) if e.ctime else "")
            item.setData(0, Qt.UserRole, e)
            self.tree.addTopLevelItem(item)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.preview.clear()
        self.location_changed.emit(self.current_location())

    # ---------- selection / open --------------------------------------------

    def _on_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        entry: FSEntry = item.data(0, Qt.UserRole)
        if entry is None:
            return
        if entry.is_dir:
            self.navigate_to(entry.path)
            return
        self._open_in_default_app(entry)

    def _open_in_default_app(self, entry: FSEntry) -> None:
        if self.backend.is_local:
            QDesktopServices.openUrl(QUrl.fromLocalFile(entry.path))
            return
        # Remote: download to cache, then open.
        local_path = os.path.join(REMOTE_OPEN_CACHE, self.backend.id.replace("/", "_"), entry.name)
        self.status.emit(f"Downloading {entry.name}…")
        worker = _RemoteOpenWorker(self.backend, entry.path, local_path)
        worker.finished_ok.connect(self._on_remote_open_done)
        worker.failed.connect(self._on_remote_open_failed)
        worker.finished.connect(lambda: self._open_workers.remove(worker)
                                if worker in self._open_workers else None)
        self._open_workers.append(worker)
        worker.start()

    def _on_remote_open_done(self, local_path: str) -> None:
        self.status.emit(f"Opened {os.path.basename(local_path)}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(local_path))

    def _on_remote_open_failed(self, msg: str) -> None:
        self.status.emit("Open failed")
        QMessageBox.warning(self, "Open file", msg)

    def _on_selection_changed(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            self.preview.clear()
            return
        entry: FSEntry = items[0].data(0, Qt.UserRole)
        if entry is None or entry.is_dir or not is_image(entry.name):
            self.preview.clear()
            return
        self.preview.show_for(
            self.backend,
            PreviewRequest(
                backend_id=self.backend.id,
                path=entry.path,
                name=entry.name,
                size=entry.size,
            ),
        )

    # ---------- mutations ---------------------------------------------------

    def new_folder(self) -> None:
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if not ok or not name.strip():
            return
        try:
            self.backend.mkdir(self.backend.join(self.cwd, name.strip()))
        except Exception as e:
            if self._maybe_auto_reconnect(e):
                return
            QMessageBox.warning(self, "New Folder", str(e))
            return
        self.refresh()

    def delete_selection(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            return
        entries = [it.data(0, Qt.UserRole) for it in items]
        names = "\n".join(f"  • {e.name}" for e in entries[:6])
        if len(entries) > 6:
            names += f"\n  …and {len(entries) - 6} more"
        ret = QMessageBox.question(
            self, "Delete",
            f"Delete {len(entries)} item(s)?\n{names}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        errors = []
        last_exc: Exception | None = None
        for e in entries:
            try:
                self.backend.delete(e.path, e.is_dir)
            except Exception as exc:
                last_exc = exc
                errors.append(f"{e.name}: {exc}")
        if last_exc is not None and self._maybe_auto_reconnect(last_exc):
            return
        if errors:
            QMessageBox.warning(self, "Delete", "\n".join(errors))
        self.refresh()

    # ---------- drop -> transfer / move ------------------------------------

    def _on_drop_received(self, source_pane_id: str, paths: list, target_dir: str) -> None:
        if source_pane_id == "__external__":
            source_backend: Backend = LocalBackend()
        else:
            source = PANE_REGISTRY.get(source_pane_id)
            if source is None:
                return
            source_backend = source.backend
        if self._same_logical_backend(source_backend):
            self._move_within_backend(source_backend, paths, target_dir)
            return
        self.transfer_requested.emit(source_backend, paths, self.backend, target_dir)

    def _same_logical_backend(self, other: Backend) -> bool:
        """True if rename across (other -> self) is a server-local op."""
        if other is self.backend:
            return True
        if other.is_local and self.backend.is_local:
            return True
        if isinstance(other, SFTPBackend) and isinstance(self.backend, SFTPBackend):
            return (
                other.host == self.backend.host
                and other.port == self.backend.port
                and other.username == self.backend.username
            )
        return False

    def _move_within_backend(self, src_backend: Backend, src_paths: list, target_dir: str) -> None:
        errors: list[str] = []
        for sp in src_paths:
            name = self._path_basename(sp)
            dst = self.backend.join(target_dir, name)
            if dst == sp:
                continue
            try:
                self.backend.rename(sp, dst)
            except Exception as e:
                if self._maybe_auto_reconnect(e):
                    return
                errors.append(f"{name}: {e}")
        if errors:
            QMessageBox.warning(self, "Move", "\n".join(errors))
        # Refresh the destination pane plus any pane whose backend points
        # at the same place (so the source pane updates after moves).
        self.refresh()
        for p in PANE_REGISTRY.values():
            if p is not self and p._same_logical_backend(src_backend):
                try:
                    p.refresh()
                except Exception:
                    pass

    def _path_basename(self, p: str) -> str:
        if self.backend.is_local:
            return os.path.basename(p.rstrip(os.sep))
        return posixpath.basename(p.rstrip("/"))

    # ---------- context menus ----------------------------------------------

    def _on_tree_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            self._show_background_menu(pos)
        else:
            self._show_item_menu(item, pos)

    def _show_item_menu(self, item: QTreeWidgetItem, pos) -> None:
        # If the right-clicked item isn't part of the selection, switch
        # selection to just it (matches Finder behavior).
        if not item.isSelected():
            self.tree.clearSelection()
            item.setSelected(True)
        entries = [it.data(0, Qt.UserRole) for it in self.tree.selectedItems()
                   if it.data(0, Qt.UserRole) is not None]
        if not entries:
            return
        single = len(entries) == 1
        only_folders = all(e.is_dir for e in entries)

        m = QMenu(self.tree)
        if single:
            primary_label = "Open" if entries[0].is_dir else "Open with default app"
            act_open = m.addAction(primary_label)
            act_open.triggered.connect(lambda: self._on_double_clicked(item, 0))
            m.addSeparator()
            act_rename = m.addAction("Rename…")
            act_rename.triggered.connect(lambda: self._rename_entry(entries[0]))

        act_copy = m.addAction("Copy")
        act_copy.triggered.connect(lambda: self._clipboard_set(entries, "copy"))
        act_cut = m.addAction("Cut")
        act_cut.triggered.connect(lambda: self._clipboard_set(entries, "cut"))

        clip = APP_FILE_CLIPBOARD
        if only_folders and single and clip.get("paths"):
            act_paste_into = m.addAction(
                f"Paste {len(clip['paths'])} item(s) into folder"
            )
            act_paste_into.triggered.connect(
                lambda: self._clipboard_paste(target_dir=entries[0].path)
            )

        m.addSeparator()
        act_copy_path = m.addAction(
            "Copy Unix Path" + ("" if single else f"  ({len(entries)} items)")
        )
        act_copy_path.triggered.connect(lambda: self._copy_paths_to_clipboard(entries))

        m.addSeparator()
        act_delete = m.addAction("Delete")
        act_delete.triggered.connect(self.delete_selection)

        m.exec_(self.tree.viewport().mapToGlobal(pos))

    def _show_background_menu(self, pos) -> None:
        m = QMenu(self.tree)
        act_copy_cwd = m.addAction("Copy Unix Path of current folder")
        act_copy_cwd.triggered.connect(self._copy_cwd_to_clipboard)
        m.addSeparator()
        act_new_folder = m.addAction("New Folder…")
        act_new_folder.triggered.connect(self.new_folder)
        act_new_text = m.addAction("New Text File…")
        act_new_text.triggered.connect(self._new_text_file)

        clip = APP_FILE_CLIPBOARD
        if clip.get("paths"):
            m.addSeparator()
            act_paste = m.addAction(
                f"Paste {len(clip['paths'])} item(s) here"
            )
            act_paste.triggered.connect(lambda: self._clipboard_paste(target_dir=self.cwd))

        m.addSeparator()
        act_refresh = m.addAction("Refresh")
        act_refresh.triggered.connect(self.refresh)
        m.exec_(self.tree.viewport().mapToGlobal(pos))

    # ---------- rename / copy / paste / unix-path / new-text-file ----------

    def _rename_entry(self, entry: FSEntry) -> None:
        new_name, ok = QInputDialog.getText(
            self, "Rename", "New name:", text=entry.name,
        )
        if not ok or not new_name.strip() or new_name == entry.name:
            return
        new_name = new_name.strip()
        new_path = self.backend.join(self.backend.parent(entry.path), new_name)
        try:
            self.backend.rename(entry.path, new_path)
        except Exception as e:
            if self._maybe_auto_reconnect(e):
                return
            QMessageBox.warning(self, "Rename", str(e))
            return
        self.refresh()

    def _clipboard_set(self, entries: list, op: str) -> None:
        APP_FILE_CLIPBOARD["backend"] = self.backend
        APP_FILE_CLIPBOARD["paths"] = [e.path for e in entries]
        APP_FILE_CLIPBOARD["op"] = op
        self.status.emit(f"{op.title()} {len(entries)} item(s)")

    def _clipboard_paste(self, target_dir: str) -> None:
        clip = APP_FILE_CLIPBOARD
        src_backend: Backend | None = clip.get("backend")
        paths = list(clip.get("paths") or [])
        op = clip.get("op", "copy")
        if not src_backend or not paths:
            return
        if self._same_logical_backend(src_backend) and op == "cut":
            self._move_within_backend(src_backend, paths, target_dir)
        else:
            self.transfer_requested.emit(src_backend, paths, self.backend, target_dir)
            if op == "cut":
                # Best-effort: clear the clipboard so a second paste copies.
                # (Server-side delete on cut after cross-backend copy would
                # require waiting for the transfer to finish — skip for now.)
                pass
        # If it was a cut, clear so the same items aren't re-pasted later.
        if op == "cut":
            APP_FILE_CLIPBOARD["paths"] = []

    def _copy_paths_to_clipboard(self, entries: list) -> None:
        text = "\n".join(e.path for e in entries)
        QApplication.clipboard().setText(text)
        n = len(entries)
        self.status.emit(f"Copied {n} path{'s' if n != 1 else ''} to clipboard")

    def _copy_cwd_to_clipboard(self) -> None:
        QApplication.clipboard().setText(self.cwd)
        self.status.emit(f"Copied path to clipboard: {self.cwd}")

    def _new_text_file(self) -> None:
        name, ok = QInputDialog.getText(
            self, "New Text File", "Filename:", text="Untitled.txt",
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        target = self.backend.join(self.cwd, name)
        # Refuse to overwrite — pick a different name if it exists.
        try:
            if self.backend.is_local and os.path.exists(target):
                QMessageBox.warning(self, "New Text File",
                                    f"File already exists: {name}")
                return
        except Exception:
            pass
        try:
            with self.backend.open_write(target) as f:
                f.write(b"")
        except Exception as e:
            if self._maybe_auto_reconnect(e):
                return
            QMessageBox.warning(self, "New Text File", str(e))
            return
        self.refresh()
        # Open in default editor. For local: opens directly. For remote:
        # downloads to ~/.cache/skiff/remote_open/<conn>/<name> and opens
        # that local copy. Edits won't auto-sync back to remote — drag the
        # edited file back when done.
        fake_entry = FSEntry(
            name=name, path=target, is_dir=False, size=0, mtime=0,
        )
        self._open_in_default_app(fake_entry)


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    f = float(n)
    for u in ("KB", "MB", "GB", "TB"):
        f /= 1024
        if f < 1024 or u == "TB":
            return f"{f:,.1f} {u}"
    return f"{n} B"


def _fmt_time(ts: float) -> str:
    if ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        return ""
