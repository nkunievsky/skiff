# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Skiff** — a macOS dual-pane SSH/SFTP file manager built on PyQt5 + paramiko. Single-user, runs from a thin `.app` bundle that just `exec`s the Python interpreter against `run.py`.

## Commands

```bash
# Run from source (no bundle)
/opt/anaconda3/bin/python3 run.py

# Lint
/opt/anaconda3/bin/python3 -m pyflakes skiff/ run.py

# Headless smoke-test (Qt minimal platform — verifies imports + window construction)
QT_QPA_PLATFORM=minimal /opt/anaconda3/bin/python3 -c "
import sys
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication
from skiff import app_state, state as state_mod
from skiff.app_state import StateBus
from skiff.style import STYLESHEET
from skiff.window import MainWindow
app = QApplication(sys.argv); app.setStyleSheet(STYLESHEET)
state = state_mod.load_state(); state.setdefault('windows', [])
record = app_state.add_window_record(state)
MainWindow(state, StateBus(), record).show()
QTimer.singleShot(150, app.quit); sys.exit(app.exec_())
"

# Rebuild the macOS bundle (regenerates icon if missing)
./build_app.sh           # writes dist/Skiff.app
open dist/Skiff.app      # launch it

# Regenerate the app icon only
/opt/anaconda3/bin/python3 tools/make_icon.py

# When debugging SSH problems
tail -f /tmp/skiff_paramiko.log
```

There are no unit tests. Verification is done by running the app or the headless smoke-test above.

## Persistent state

User state lives at **`~/Library/Application Support/Skiff/state.json`** (not `~/.config/` — that path is root-owned on this user's Mac). Directory mode is `0700`, file mode `0600`. The file holds favorites, recents, saved connections (**including plaintext passwords**, an explicit user opt-in), per-window records (id/title/bg_color/geometry/panes), and column visibility/widths. `skiff/state.py` automatically migrates from the legacy `Forklift/` directory on first launch so existing users keep their data. paramiko debug log goes to `/tmp/skiff_paramiko.log`.

## Architecture

### Module layout

- **`fs.py`** — `Backend` ABC with `LocalBackend` and `SFTPBackend`. `FSEntry` carries name/path/is_dir/size/mtime + optional mode/uid/gid/owner/ctime.
- **`connection.py`** — SSH connect flow: `ConnectDialog`, `ConnectWorker` (QThread), `ConnectCoordinator` (main-thread prompt presenter).
- **`pane.py`** — `FilePane`: location bar, `FileTree` (subclassed `QTreeWidget` with custom drag/drop), context menus, image preview, in-app `APP_FILE_CLIPBOARD`. `PANE_REGISTRY` (uuid → pane) lets drop-side resolve drag source.
- **`transfer.py`** — `TransferQueue` + `TransferWorker` (one job at a time, chunked stream-copy with progress).
- **`preview.py`** — Image preview with caching and worker thread per remote fetch.
- **`window.py`** — `MainWindow`. Sidebar + two `FilePane`s in a `QSplitter`. Owns the menu bar and per-window title/bg color.
- **`sidebar.py`** — Favorites / Recent / Saved Connections, grouped by host.
- **`state.py`** — JSON load/save + helpers (favorites, recents, connections, last paths).
- **`app_state.py`** — `StateBus` (Qt signals across windows) and window-record helpers. Single shared state dict passed by reference.
- **`style.py`** — Single QSS string applied at app level.
- **`run.py`** — Multi-window manager. Builds initial windows from `state["windows"]`, handles `bus.new_window_requested` / `bus.window_closed`.

### Cross-cutting flows

**SSH auth (multi-factor, Duo-aware)** — `connection.py` ConnectWorker runs auth off-thread. Each kbd-interactive challenge is bounced to the main thread via `pyqtSignal` + `queue.Queue` (worker blocks on `q.get()`, main thread shows `KbdInteractiveDialog` and puts responses on the queue). Saved password is auto-fed into prompts whose label contains "password" so the user only sees the Duo challenge.

**Auto-reconnect** — On a pane operation failure, `FilePane._maybe_auto_reconnect()` checks `transport.is_active()` plus the exception text; if dropped, emits `reconnect_requested(cwd)`. Window looks up the saved profile (incl. password) and calls `pane.connect_with(req, target_path=cwd)`. Duo prompt reappears; pane lands at the same path.

**Multi-window state sync** — All windows share one state dict (passed by reference) and one `StateBus`. Any mutation calls `state_mod.save_state(state)` then `bus.state_changed.emit()`. Each window's `_on_external_state_changed` repopulates its sidebar.

**Drag-drop / move vs copy** — `FileTree.dropEvent` resolves a `target_dir` (folder under cursor → that folder, else cwd) and emits `drop_received(src_id, paths, target_dir)`. `FilePane._on_drop_received` calls `_same_logical_backend(src)` (same instance, both local, or same SFTP host/user/port). Same-logical-backend → `_move_within_backend` uses `backend.rename` (server-side move, no transfer). Otherwise → `transfer_requested` signal → `TransferQueue`.

**SFTP concurrency** — `SFTPBackend` holds a shared `paramiko.SFTPClient` (used from main thread for list/stat/mkdir, serialized by `RLock`). All streaming ops (`open_read`, `open_write`, `read_bytes`) open a **dedicated channel** via `transport.open_sftp_client()` so transfers and previews don't corrupt each other or the metadata client.

## Non-obvious constraints (read before changing the affected code)

These all came from real bugs encountered against UChicago RCC's midway3 (OpenSSH 8.0 + Duo PAM). Each is a load-bearing comment near the code:

- **paramiko pinned to `>=3.5,<4`** in `requirements.txt`. paramiko 4.0 has an SFTP-subsystem regression on OpenSSH 8.x where `SFTPClient.from_transport` raises `EOFError` post-auth.
- **Pre-connect TCP via `socket.create_connection`** in `ConnectWorker.run`, then pass the live socket to `paramiko.Transport(sock)`. paramiko's own hostname iteration re-resolves the name on each retry, so on round-robin DNS where one address refuses it never actually rotates.
- **Auth-method ordering: publickey → keyboard-interactive → password.** The `password` SSH method always fails on Duo-MFA servers (the password is meant to be delivered via kbd-interactive). Trying it standalone burns a failed-auth count and some servers close the connection even after a later successful auth. See `_authenticate` in `connection.py`.
- **Don't auto-fill `~/.ssh/id_*` in the connect dialog.** A publickey attempt with an unauthorized key counts as a failed-auth on the connection and can cause the same close-after-success as above.
- **`transport.set_keepalive(30)` after auth** in `ConnectWorker.run` — prevents idle servers from culling sessions during laptop sleep.
- **400 ms sleep + one retry around `SFTPClient.from_transport`** (`_open_sftp_with_retry`) — Duo PAM finalizes the session asynchronously after `USERAUTH_SUCCESS`; opening a channel too quickly races that finalization.
- **`_relative(backend, root, full)` returns `""` when `full == root`** (`transfer.py`) — prevents `posixpath.join("/dest/foo", "/abs/source/foo")` from discarding `dest_root` when an absolute path is on the right.
- **Long-running `QThread`s must be retained in a list** (e.g. `_open_workers`, `_workers` in `preview.py`). If the Python wrapper is GC'd while the C++ thread is still running, Qt aborts (SIGABRT). Crash reports go to `~/Library/Logs/DiagnosticReports/python3.12-*.ips`.
- **No auto-raise-all on `applicationStateChanged.ApplicationActive`.** Raising windows that live on other macOS Spaces drags Mission Control across Spaces — annoying when the user splits Skiff windows by project. Explicit "Bring All Windows to Front" (Cmd+Shift+F) and per-window menu items remain.
- **Background tint via per-window stylesheet must override several selectors** (QMainWindow, QToolBar, QMenuBar, QStatusBar, QFrame#previewFrame, QTreeWidget#sidebar, QLineEdit#pathEdit, QSplitter+handle). The file-list `QTreeWidget` is intentionally left white for readability.
- **Column-width persistence merges, doesn't replace** (`window._on_column_widths_changed`). Hidden columns report `sectionSize == 0`; `current_column_widths` skips them so toggling a column off and on preserves its prior width.

## .app bundle gotchas

`build_app.sh` produces a thin bundle — `Contents/MacOS/Skiff` is a shell script that hardcodes `PYTHON_BIN` and `REPO_DIR`. **Move or rename the project folder and you must re-run `./build_app.sh`.** Icon is generated by `tools/make_icon.py` (Pillow) into `assets/icon_1024.png`, then `sips` + `iconutil` build `assets/AppIcon.icns`. Edit the Pillow code in `make_icon.py` to redesign the logo.
