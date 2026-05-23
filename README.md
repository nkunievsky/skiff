# Skiff

<img src="assets/icon_1024.png" alt="Skiff icon" width="128" align="right">

A macOS dual-pane SSH/SFTP file manager. Two file lists side by side, drag
between them to transfer, image previews built in. Designed around the
working pattern of moving files between a laptop and remote servers (HPC
clusters, dev boxes, anywhere with SFTP).

Built on PyQt5 + paramiko. Single-user, no telemetry.

## Features

- **Dual-pane browser** — local or remote on either side, drag-drop to
  transfer between them.
- **SSH with multi-factor auth** — keyboard-interactive prompts (Duo, OTP,
  …) appear as Qt dialogs. Host-key fingerprint is verified against
  `~/.ssh/known_hosts` on first connect.
- **Saved connections + passwords** — connection profiles auto-fill on
  re-connect; you'll only need to approve Duo. Stored at `0600` in
  `~/Library/Application Support/Skiff/state.json`.
- **Auto-reconnect** — if your session dies during laptop sleep, the next
  operation transparently rebuilds the SSH session using saved credentials.
- **Sidebar** — Favorites, last-10 Recents (grouped by host), and Saved
  Connections, all shared across all open windows.
- **Multiple windows** — Cmd+N opens another window. Set a per-window
  title and background tint to identify them at a glance. Each window
  remembers its size, position, and last-visited paths.
- **Image previews** — selecting an image shows a thumbnail in the pane.
- **Drag onto a folder = move** — same-server drops use SFTP rename
  (server-side, instant). Cross-server drops fall back to the transfer
  queue.
- **Right-click context menus** — Rename, Copy, Cut, Paste, Copy Unix
  Path, Delete; New Folder and New Text File on empty areas.
- **Configurable columns** — Name, Size, Modified, Type, Permissions,
  Owner, Created. Right-click the header to toggle. Widths are persisted.
- **Keyboard** — Cmd+R refresh, Cmd+N new window, Cmd+Shift+T set title,
  Cmd+Shift+F bring all windows to front, Esc → jump to path bar.

## Requirements

- macOS (tested on Sonoma+; should work on most recent versions)
- Python 3.10+
- `PyQt5`, `paramiko>=3.5,<4`, `Pillow` (see `requirements.txt`)

> **Why paramiko `<4`?** paramiko 4.0 has an SFTP-subsystem regression on
> OpenSSH 8.x servers (the SFTP version handshake EOFs). 3.5.x is stable.

## Install & run

```bash
git clone https://github.com/nkunievsky/skiff.git
cd skiff
pip install -r requirements.txt

# Run from source
python3 run.py
```

### Build the macOS .app

```bash
./build_app.sh
# Writes dist/Skiff.app — drag it to /Applications.
```

`build_app.sh` produces a **thin** bundle: the `Skiff` executable is a
shell script that points at your `python3` and the project's `run.py`. If
you move or rename the project directory, re-run `./build_app.sh`. For a
fully self-contained bundle, you'd need py2app or PyInstaller (not wired
up here).

The icon (`assets/icon_1024.png` + `assets/AppIcon.icns`) is rendered
from `tools/make_icon.py` — edit the Pillow code there to redesign it.

## Persistent state

- **State file**: `~/Library/Application Support/Skiff/state.json`
  (`0600`, directory `0700`). Holds favorites, recents, connections
  (including plaintext passwords if you opted in), per-window records,
  column visibility, and column widths.
- **paramiko debug log**: `/tmp/skiff_paramiko.log` (useful when SSH/SFTP
  misbehaves).
- **Remote-file cache**: `~/.cache/skiff/remote_open/` (where remote
  files get downloaded when you double-click them).

## Architecture

See [`CLAUDE.md`](CLAUDE.md) for a tour of the module layout, the
cross-cutting flows (SSH auth, auto-reconnect, multi-window state sync,
drag-drop move-vs-copy, SFTP channel concurrency), and the non-obvious
constraints baked into the code — most of which came from real bugs
debugged against an HPC cluster with Duo 2FA.

## Known limits

- In-flight transfers don't resume after a session drop — they surface as
  errors in the transfer dialog and you re-drag.
- Cross-server Cut+Paste behaves as Copy (the source isn't deleted after
  the cross-backend transfer). Same-server Cut+Paste uses rename and
  moves correctly.
- Remote "New Text File" creates the file on the server, then downloads
  a local copy to your editor — edits don't auto-sync back.
- Some macOS Spaces behavior is at the OS level: a window on another
  Space stays on its Space. Use **Window > Bring All Windows to Front**
  (Cmd+Shift+F) to gather them on the current Space.

## License

No license chosen yet — all rights reserved by default. Add a `LICENSE`
file if you want to make the code reusable (MIT and Apache-2.0 are common
choices for tools like this).
