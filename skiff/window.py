"""Main window: sidebar + two file panes, transfer queue, persistent state."""
from __future__ import annotations

import os

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QKeySequence
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QColorDialog,
    QDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QStatusBar,
)

from . import state as state_mod
from .app_state import StateBus
from .connection import ConnectDialog, ConnectionRequest
from .fs import SFTPBackend
from .pane import FilePane
from .sidebar import Sidebar
from .transfer import TransferJob, TransferQueue


class MainWindow(QMainWindow):
    def __init__(self, state: dict, bus: StateBus, record: dict):
        super().__init__()
        self.state = state
        self.bus = bus
        self.record = record
        self.window_id = record["id"]

        self.setWindowTitle(record.get("title") or "Skiff")
        # Default geometry; restored below if record has it.
        self.resize(1380, 760)

        self.sidebar = Sidebar()
        self.left = FilePane()
        self.right = FilePane()
        self._panes = (self.left, self.right)
        self._active_pane: FilePane = self.left

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.sidebar)
        splitter.addWidget(self.left)
        splitter.addWidget(self.right)
        splitter.setSizes([220, 580, 580])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 1)
        splitter.setHandleWidth(1)
        self.setCentralWidget(splitter)

        # ----- status bar with progress
        self.status_label = QLabel("Ready")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(220)
        self.progress.setMaximumHeight(14)
        self.progress.hide()
        sb = QStatusBar()
        sb.addWidget(self.status_label, stretch=1)
        sb.addPermanentWidget(self.progress)
        self.setStatusBar(sb)

        # ----- transfer queue
        self.queue = TransferQueue(self)
        self.queue.job_started.connect(self._on_job_started)
        self.queue.file_started.connect(self._on_file_started)
        self.queue.progress.connect(self._on_progress)
        self.queue.file_done.connect(self._on_file_done)
        self.queue.job_finished.connect(self._on_job_finished)
        self.queue.queue_idle.connect(self._on_queue_idle)
        self._cur_total_bytes = 0

        # ----- pane wiring
        for pane in self._panes:
            pane.transfer_requested.connect(self._on_transfer_requested)
            pane.status.connect(self.status_label.setText)
            pane.location_changed.connect(self._on_location_changed)
            pane.focused.connect(self._on_pane_focused)
            pane.connect_requested.connect(
                lambda target_path, p=pane: self._open_connect_dialog(p, target_path)
            )
            pane.connection_established.connect(self._on_connection_established)
            pane.favorite_toggle_requested.connect(self._on_favorite_toggle)
            pane.column_visibility_changed.connect(self._on_columns_changed)
            pane.column_widths_changed.connect(self._on_column_widths_changed)
            pane.reconnect_requested.connect(
                lambda target_path, p=pane: self._reconnect_pane(p, target_path)
            )

        # ----- sidebar wiring
        self.sidebar.location_activated.connect(self._on_sidebar_location)
        self.sidebar.connection_activated.connect(self._on_sidebar_connection)
        self.sidebar.favorite_remove_requested.connect(self._on_favorite_remove)
        self.sidebar.connection_remove_requested.connect(self._on_connection_remove)
        self.sidebar.recent_clear_requested.connect(self._on_recent_clear)

        # ----- menu bar
        self._build_menus()

        # ----- restore per-pane local paths from window record
        panes_state = self.record.get("panes") or []
        for i, pane in enumerate(self._panes):
            if i < len(panes_state):
                p = panes_state[i].get("path") or ""
                if p and os.path.isdir(p):
                    pane.cwd = p
                    pane.refresh()

        # ----- restore column visibility (shared across windows)
        cols = self.state.get("columns") or {}
        if cols:
            for pane in self._panes:
                pane.apply_column_visibility(cols)

        widths = self.state.get("column_widths") or {}
        if widths:
            for pane in self._panes:
                pane.apply_column_widths(widths)

        # ----- apply window-level customizations from record
        if record.get("bg_color"):
            self._apply_bg_color(record["bg_color"])
        geom = record.get("geometry") or []
        if len(geom) == 4:
            self.setGeometry(*geom)

        self.sidebar.populate(self.state)
        self._refresh_favorite_icons()

        # ----- listen for cross-window state mutations
        self.bus.state_changed.connect(self._on_external_state_changed)

    # ---------------- menus ----------------------------------------------

    def _build_menus(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("File")
        act_new = QAction("New Window", self)
        act_new.setShortcut(QKeySequence("Ctrl+N"))
        act_new.triggered.connect(self.bus.new_window_requested)
        file_menu.addAction(act_new)
        file_menu.addSeparator()
        act_close = QAction("Close Window", self)
        act_close.setShortcut(QKeySequence("Ctrl+W"))
        act_close.triggered.connect(self.close)
        file_menu.addAction(act_close)

        self.win_menu = bar.addMenu("Window")
        act_title = QAction("Set Window Title…", self)
        act_title.setShortcut(QKeySequence("Ctrl+Shift+T"))
        act_title.triggered.connect(self._prompt_title)
        self.win_menu.addAction(act_title)
        act_color = QAction("Set Background Color…", self)
        act_color.triggered.connect(self._prompt_color)
        self.win_menu.addAction(act_color)
        act_reset = QAction("Reset Color", self)
        act_reset.triggered.connect(self._reset_color)
        self.win_menu.addAction(act_reset)
        self.win_menu.addSeparator()
        act_bring_all = QAction("Bring All Windows to Front", self)
        # Cmd+Shift+F — easy to hit one-handed; doesn't collide.
        act_bring_all.setShortcut(QKeySequence("Ctrl+Shift+F"))
        act_bring_all.triggered.connect(self.bus.bring_all_to_front_requested)
        self.win_menu.addAction(act_bring_all)
        # Dynamic list of all open windows, refreshed each time the menu opens.
        self._window_list_separator = self.win_menu.addSeparator()
        self._window_list_actions: list[QAction] = []
        self.win_menu.aboutToShow.connect(self._refresh_window_list)

    def _refresh_window_list(self) -> None:
        for act in self._window_list_actions:
            self.win_menu.removeAction(act)
        self._window_list_actions.clear()
        # Include hidden/minimized windows too so the user can surface
        # any of them from the menu.
        windows = [
            w for w in QApplication.topLevelWidgets()
            if isinstance(w, MainWindow)
        ]
        windows.sort(key=lambda w: (w.windowTitle() or "").lower())
        for w in windows:
            title = w.windowTitle() or "(untitled)"
            label = f"{'✓ ' if w is self else '   '}{title}"
            act = QAction(label, self)
            act.triggered.connect(
                lambda _checked=False, win=w: self._raise_window(win)
            )
            self.win_menu.addAction(act)
            self._window_list_actions.append(act)

    def _raise_window(self, win: "MainWindow") -> None:
        if win.isMinimized():
            win.setWindowState(win.windowState() & ~Qt.WindowMinimized)
        if not win.isVisible():
            win.show()
        win.raise_()
        win.activateWindow()

    def _prompt_title(self) -> None:
        text, ok = QInputDialog.getText(
            self, "Window Title", "Title:",
            text=self.record.get("title") or "",
        )
        if not ok:
            return
        self.record["title"] = text
        self.setWindowTitle(text or "Skiff")
        self._save()

    def _prompt_color(self) -> None:
        cur_hex = self.record.get("bg_color") or "#f7f7f8"
        col = QColorDialog.getColor(QColor(cur_hex), self,
                                     "Window Background Color")
        if not col.isValid():
            return
        self._apply_bg_color(col.name())
        self.record["bg_color"] = col.name()
        self._save()

    def _reset_color(self) -> None:
        self.record["bg_color"] = ""
        self.setStyleSheet("")
        if self.centralWidget():
            self.centralWidget().setStyleSheet("")
        self._save()

    def _apply_bg_color(self, c: str) -> None:
        """Tint all the window's chrome surfaces. File lists stay white
        (per-widget overrides in the global stylesheet) so file content
        remains readable on any tint."""
        self.setStyleSheet(f"""
            QMainWindow {{ background: {c}; }}
            QMainWindow > QWidget {{ background: {c}; }}
            QSplitter {{ background: {c}; }}
            QSplitter::handle {{ background: {c}; }}
            QToolBar {{ background: {c}; }}
            QMenuBar {{ background: {c}; }}
            QStatusBar {{ background: {c}; }}
            QFrame#previewFrame {{ background: {c}; }}
            QTreeWidget#sidebar {{ background: {c}; }}
            QLineEdit#pathEdit {{ background: {c}; }}
        """)

    # ---------------- right-click on window chrome ----------------------

    def contextMenuEvent(self, event) -> None:
        """Right-clicking on the window's chrome (margins / toolbars /
        status bar) opens the same actions as the Window menu. Right-clicks
        inside a file list, sidebar, path bar, or column header are
        consumed by those widgets and don't reach here."""
        menu = QMenu(self)
        act_title = menu.addAction("Set Window Title…")
        act_title.triggered.connect(self._prompt_title)
        act_color = menu.addAction("Set Background Color…")
        act_color.triggered.connect(self._prompt_color)
        act_reset = menu.addAction("Reset Color")
        act_reset.triggered.connect(self._reset_color)
        menu.addSeparator()
        act_new = menu.addAction("New Window")
        act_new.triggered.connect(self.bus.new_window_requested.emit)
        menu.exec_(event.globalPos())

    # ---------------- pane focus / active --------------------------------

    def _on_pane_focused(self, pane: FilePane) -> None:
        self._active_pane = pane

    # ---------------- sidebar handlers -----------------------------------

    def _on_sidebar_location(self, loc: dict) -> None:
        target = self._active_pane
        if loc.get("kind") == "local":
            target.go_local(loc.get("path") or os.path.expanduser("~"))
            return
        cur = target.backend
        same = (
            not cur.is_local
            and getattr(cur, "host", None) == loc.get("host")
            and int(getattr(cur, "port", 22)) == int(loc.get("port", 22))
            and getattr(cur, "username", None) == loc.get("username")
        )
        if same and loc.get("path"):
            target.navigate_to(loc["path"])
            return
        # SFTP destination not currently connected — open dialog with the
        # full saved profile (so password auto-fills if we have it).
        prefill = self._lookup_connection(loc) or {
            "host": loc.get("host", ""),
            "port": loc.get("port", 22),
            "username": loc.get("username", ""),
            "key_path": loc.get("key_path", ""),
        }
        self._open_connect_dialog(target, target_path=loc.get("path"),
                                  prefill=prefill)

    def _lookup_connection(self, loc: dict) -> dict | None:
        host = loc.get("host", "")
        port = int(loc.get("port", 22))
        user = loc.get("username", "")
        for c in self.state.get("connections", []):
            if (c.get("host") == host
                    and int(c.get("port", 22)) == port
                    and c.get("username") == user):
                return dict(c)
        return None

    def _on_sidebar_connection(self, conn: dict) -> None:
        self._open_connect_dialog(self._active_pane, prefill=conn)

    def _on_favorite_remove(self, loc: dict) -> None:
        state_mod.remove_favorite(self.state, loc)
        self._save()

    def _on_connection_remove(self, conn: dict) -> None:
        state_mod.remove_connection(
            self.state,
            host=conn.get("host", ""),
            port=int(conn.get("port", 22)),
            username=conn.get("username", ""),
        )
        self._save()

    def _on_recent_clear(self) -> None:
        self.state["recents"] = []
        self._save()

    # ---------------- pane events -> state -------------------------------

    def _on_location_changed(self, loc: dict) -> None:
        sender = self.sender()
        if loc.get("kind") == "local" and sender in self._panes:
            panes = self.record.setdefault("panes", [{"path": ""}, {"path": ""}])
            while len(panes) < len(self._panes):
                panes.append({"path": ""})
            panes[self._panes.index(sender)]["path"] = loc.get("path", "")

        state_mod.add_recent(self.state, loc)
        self._save()
        self._refresh_favorite_icons()

    def _on_connection_established(self, conn: dict) -> None:
        state_mod.save_connection(
            self.state,
            host=conn["host"], port=conn["port"],
            username=conn["username"],
            key_path=conn.get("key_path", ""),
            password=conn.get("password", ""),
        )
        self._save()

    def _on_columns_changed(self, vis: dict) -> None:
        self.state["columns"] = dict(vis)
        for pane in self._panes:
            if pane is not self.sender():
                pane.apply_column_visibility(vis)
        self._save()

    def _on_column_widths_changed(self, widths: dict) -> None:
        # Merge so hidden columns keep their previously-saved widths.
        cw = self.state.get("column_widths") or {}
        cw.update(widths)
        self.state["column_widths"] = cw
        for pane in self._panes:
            if pane is not self.sender():
                pane.apply_column_widths(widths)
        self._save()

    def _on_favorite_toggle(self, loc: dict) -> None:
        if state_mod.is_favorite(self.state, loc):
            state_mod.remove_favorite(self.state, loc)
        else:
            state_mod.add_favorite(self.state, loc)
        self._save()
        self._refresh_favorite_icons()

    def _refresh_favorite_icons(self) -> None:
        for pane in self._panes:
            pane.set_favorite_state(
                state_mod.is_favorite(self.state, pane.current_location())
            )

    # ---------------- connect dialog router ------------------------------

    def _reconnect_pane(self, pane: FilePane, target_path: str) -> None:
        """Auto-rebuild a dropped SFTP session using the saved password."""
        if not isinstance(pane.backend, SFTPBackend):
            return
        host = pane.backend.host
        port = pane.backend.port
        user = pane.backend.username
        saved = self._lookup_connection({"host": host, "port": port, "username": user})
        if not saved or not saved.get("password"):
            QMessageBox.warning(
                self, "Reconnect",
                f"Connection to {pane.backend.display_name()} dropped, "
                "but no saved password is available. Please reconnect manually.",
            )
            pane.disconnect_remote()
            return
        req = ConnectionRequest(
            host=host, port=int(port), username=user,
            password=saved["password"],
            key_path=saved.get("key_path") or None,
        )
        pane.connect_with(req, target_path=target_path)

    def _open_connect_dialog(
        self,
        pane: FilePane,
        target_path: str | None = None,
        prefill: dict | None = None,
    ) -> None:
        dlg = ConnectDialog(
            self,
            profiles=self.state.get("connections", []),
            prefill=prefill,
        )
        if dlg.exec_() != QDialog.Accepted:
            return
        req = dlg.request()
        if req is None:
            QMessageBox.warning(self, "Connect", "Host and username are required.")
            return
        pane.connect_with(req, target_path=target_path)

    # ---------------- transfer slots -------------------------------------

    def _on_transfer_requested(self, src_backend, paths, dst_backend, dst_dir):
        self.queue.submit(TransferJob(
            source_backend=src_backend, paths=list(paths),
            dest_backend=dst_backend, dest_dir=dst_dir,
        ))

    def _on_job_started(self, job: TransferJob):
        self.status_label.setText(
            f"Transferring → {job.dest_backend.display_name()}:{job.dest_dir}"
        )
        self.progress.setRange(0, 0)
        self.progress.show()

    def _on_file_started(self, name: str, total: int):
        self._cur_total_bytes = max(total, 1)
        self.progress.setRange(0, self._cur_total_bytes)
        self.progress.setValue(0)
        self.status_label.setText(f"Copying {name}")

    def _on_progress(self, done: int):
        if self.progress.maximum() != self._cur_total_bytes:
            self.progress.setRange(0, self._cur_total_bytes)
        self.progress.setValue(min(done, self._cur_total_bytes))

    def _on_file_done(self):
        pass

    def _on_job_finished(self, ok: int, fail: int, errors: list):
        self.left.refresh()
        self.right.refresh()
        msg = f"Transferred {ok} file(s)"
        if fail:
            msg += f", {fail} failed"
        self.status_label.setText(msg)
        if errors:
            QMessageBox.warning(self, "Transfer errors", "\n".join(errors[:20]))

    def _on_queue_idle(self):
        self.progress.hide()
        self.progress.setRange(0, 0)

    # ---------------- save / cross-window sync ---------------------------

    def _save(self) -> None:
        state_mod.save_state(self.state)
        # Notify all windows (including this one) to repopulate sidebar.
        self.bus.state_changed.emit()

    def _on_external_state_changed(self) -> None:
        self.sidebar.populate(self.state)
        self._refresh_favorite_icons()

    # ---------------- shutdown -------------------------------------------

    def closeEvent(self, event):
        # Persist geometry before tearing down.
        g = self.geometry()
        self.record["geometry"] = [g.x(), g.y(), g.width(), g.height()]
        try:
            state_mod.save_state(self.state)
        except Exception:
            pass
        for pane in self._panes:
            try:
                if not pane.backend.is_local:
                    pane.backend.close()
            except Exception:
                pass
        self.bus.window_closed.emit(self.window_id)
        super().closeEvent(event)
