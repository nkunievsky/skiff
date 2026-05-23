"""Entry point: launches the dual-pane SSH/SFTP file manager."""
from __future__ import annotations

import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from skiff import app_state, state as state_mod
from skiff.app_state import StateBus
from skiff.style import STYLESHEET
from skiff.window import MainWindow


def raise_all(windows: list[MainWindow]) -> None:
    """Surface every Skiff window — un-minimize, un-hide, raise, activate.

    The last one in the list wins focus. Calling this handles dock-click,
    Cmd-Tab back, and the explicit Window > Bring All Windows to Front
    menu item the same way."""
    for w in windows:
        if w.isMinimized():
            w.setWindowState(w.windowState() & ~Qt.WindowMinimized)
        if not w.isVisible():
            w.show()
        w.raise_()
        w.activateWindow()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Skiff")
    app.setApplicationDisplayName("Skiff")
    app.setStyleSheet(STYLESHEET)

    state = state_mod.load_state()
    state.setdefault("windows", [])
    app_state.migrate_legacy_local_paths(state)

    bus = StateBus()
    windows: list[MainWindow] = []

    def create_window(record: dict | None = None) -> MainWindow:
        if record is None:
            record = app_state.add_window_record(state)
            state_mod.save_state(state)
        win = MainWindow(state, bus, record)
        windows.append(win)
        win.show()
        return win

    def on_new_window():
        create_window()

    def on_window_closed(window_id: str):
        nonlocal windows
        windows = [w for w in windows if w.window_id != window_id]
        app_state.remove_window(state, window_id)
        state_mod.save_state(state)
        if not windows:
            app.quit()

    bus.new_window_requested.connect(on_new_window)
    bus.window_closed.connect(on_window_closed)

    # Don't auto-raise all windows on activation — that pulls windows across
    # macOS Spaces (annoying when the user has split Skiff windows by project
    # onto different desktops). macOS handles ordinary activation natively.
    # The explicit Window > Bring All Windows to Front (Cmd+Shift+F) and
    # per-window menu items remain for when the user *does* want to gather
    # all windows together.
    bus.bring_all_to_front_requested.connect(lambda: raise_all(list(windows)))

    saved = list(state.get("windows") or [])
    if saved:
        for record in saved:
            create_window(record)
    else:
        create_window()

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
