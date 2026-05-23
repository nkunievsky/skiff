"""SSH connection: dialog, worker thread, kbd-interactive 2FA handling."""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import queue
import socket
import time
from dataclasses import dataclass

import paramiko

PARAMIKO_LOG = "/tmp/skiff_paramiko.log"


def _setup_paramiko_logging() -> None:
    """One-time debug logging to a known file so we can diagnose failures."""
    log = logging.getLogger("paramiko")
    if any(getattr(h, "_skiff", False) for h in log.handlers):
        return
    log.setLevel(logging.DEBUG)
    handler = logging.FileHandler(PARAMIKO_LOG, mode="w")
    handler._skiff = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    ))
    log.addHandler(handler)


_setup_paramiko_logging()
from paramiko.ssh_exception import SSHException
from PyQt5.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .fs import SFTPBackend


# ---------------------------------------------------------------- request/dialog


@dataclass
class ConnectionRequest:
    host: str
    port: int = 22
    username: str = ""
    password: str | None = None
    key_path: str | None = None
    key_passphrase: str | None = None


class ConnectDialog(QDialog):
    """Collects SSH connection parameters."""

    def __init__(
        self,
        parent: QWidget | None = None,
        profiles: list[dict] | None = None,
        prefill: dict | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Connect to SSH server")
        self.setMinimumWidth(440)

        self.profile = QComboBox()
        self.profile.addItem("(new connection)", None)
        for p in profiles or []:
            label = f"{p.get('username','')}@{p.get('host','')}"
            if p.get("port", 22) != 22:
                label += f":{p['port']}"
            self.profile.addItem(label, p)
        self.profile.currentIndexChanged.connect(self._on_profile_changed)

        self.host = QLineEdit()
        self.host.setPlaceholderText("example.com")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        self.username = QLineEdit()
        self.username.setPlaceholderText(os.environ.get("USER", ""))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("(optional — prompted if needed)")

        self.key_path = QLineEdit()
        self.key_path.setPlaceholderText("(optional)")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_key)
        key_row = QHBoxLayout()
        key_row.addWidget(self.key_path)
        key_row.addWidget(browse)

        self.key_pass = QLineEdit()
        self.key_pass.setEchoMode(QLineEdit.Password)
        self.key_pass.setPlaceholderText("(if key is encrypted)")

        form = QFormLayout()
        if profiles:
            form.addRow("Saved", self.profile)
        form.addRow("Host", self.host)
        form.addRow("Port", self.port)
        form.addRow("Username", self.username)
        form.addRow("Password", self.password)
        form.addRow("Private key", key_row)
        form.addRow("Key passphrase", self.key_pass)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        if prefill:
            self._apply_prefill(prefill)
        # Note: we deliberately do NOT auto-fill ~/.ssh/id_*. On strict-PAM
        # servers a publickey attempt with an unauthorized key burns a failed-
        # auth count and can cause the server to close the connection even
        # after a later successful keyboard-interactive auth.

    def _on_profile_changed(self, idx: int) -> None:
        data = self.profile.itemData(idx)
        if data is None:
            return
        self._apply_prefill(data)
        self.password.setFocus()

    def _apply_prefill(self, data: dict) -> None:
        if "host" in data:
            self.host.setText(str(data.get("host") or ""))
        if "port" in data:
            self.port.setValue(int(data.get("port") or 22))
        if "username" in data:
            self.username.setText(str(data.get("username") or ""))
        if "key_path" in data:
            self.key_path.setText(str(data.get("key_path") or ""))
        if data.get("password"):
            self.password.setText(str(data["password"]))

    def _browse_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select private key",
            os.path.expanduser("~/.ssh"),
        )
        if path:
            self.key_path.setText(path)

    def request(self) -> ConnectionRequest | None:
        if not self.host.text().strip() or not self.username.text().strip():
            return None
        return ConnectionRequest(
            host=self.host.text().strip(),
            port=self.port.value(),
            username=self.username.text().strip(),
            password=self.password.text() or None,
            key_path=self.key_path.text().strip() or None,
            key_passphrase=self.key_pass.text() or None,
        )


# ---------------------------------------------------------- kbd-interactive prompt


class KbdInteractiveDialog(QDialog):
    """Renders a multi-prompt kbd-interactive challenge (e.g. password + OTP)."""

    def __init__(
        self,
        title: str,
        instructions: str,
        prompts: list[tuple[str, bool]],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title or "Authentication required")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        if instructions:
            lbl = QLabel(instructions)
            lbl.setWordWrap(True)
            layout.addWidget(lbl)

        self._inputs: list[QLineEdit] = []
        form = QFormLayout()
        for prompt_text, echo in prompts:
            edit = QLineEdit()
            if not echo:
                edit.setEchoMode(QLineEdit.Password)
            form.addRow(prompt_text or "Response", edit)
            self._inputs.append(edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if self._inputs:
            self._inputs[0].setFocus()

    def responses(self) -> list[str]:
        return [e.text() for e in self._inputs]


# ------------------------------------------------------------------ worker thread


class ConnectWorker(QThread):
    """Runs paramiko handshake + auth off the UI thread.

    Emits ``need_kbd_interactive`` and ``need_host_key_confirm`` so the main
    thread can show modal dialogs while we block on a Queue for the answer.
    """

    succeeded = pyqtSignal(object)  # SFTPBackend
    failed = pyqtSignal(str)
    # title, instructions, [(prompt, echo)], response_queue
    need_kbd_interactive = pyqtSignal(str, str, list, object)
    # hostname, key_type, fingerprint, response_queue
    need_host_key_confirm = pyqtSignal(str, str, str, object)

    def __init__(self, req: ConnectionRequest):
        super().__init__()
        self.req = req

    # paramiko calls this from this thread; we must not touch QWidgets here.
    def _kbd_handler(self, title, instructions, prompt_list):
        # Auto-fill any password-style prompt with the stored password (if we
        # have one). The user only sees prompts we don't already know — usually
        # just the Duo "Passcode or option (1-3):" question.
        prompts = list(prompt_list)
        prefilled: list[str | None] = [None] * len(prompts)
        if self.req.password:
            for i, (text, echo) in enumerate(prompts):
                if (not echo) and "password" in (text or "").lower():
                    prefilled[i] = self.req.password

        ask_indices = [i for i, p in enumerate(prefilled) if p is None]
        if not ask_indices:
            return [prefilled[i] or "" for i in range(len(prompts))]

        ask_prompts = [prompts[i] for i in ask_indices]
        q: queue.Queue = queue.Queue(maxsize=1)
        self.need_kbd_interactive.emit(
            title or "", instructions or "", ask_prompts, q,
        )
        result = q.get()
        if result is None:
            raise SSHException("Authentication cancelled by user")

        merged: list[str] = []
        result_iter = iter(result)
        for i in range(len(prompts)):
            if prefilled[i] is not None:
                merged.append(prefilled[i] or "")
            else:
                merged.append(next(result_iter, ""))
        return merged

    def _confirm_host_key(self, hostname, key) -> None:
        hosts_path = os.path.expanduser("~/.ssh/known_hosts")
        hk = paramiko.HostKeys()
        if os.path.exists(hosts_path):
            try:
                hk.load(hosts_path)
            except Exception:
                pass

        existing = hk.lookup(hostname)
        if existing is not None:
            stored = existing.get(key.get_name())
            if stored is not None:
                if stored.asbytes() == key.asbytes():
                    return
                raise SSHException(
                    f"Host key for {hostname} does not match the one in known_hosts"
                )

        digest = hashlib.sha256(key.asbytes()).digest()
        fp = "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
        q: queue.Queue = queue.Queue(maxsize=1)
        self.need_host_key_confirm.emit(hostname, key.get_name(), fp, q)
        accept = q.get()
        if not accept:
            raise SSHException("Host key not accepted")

        hk.add(hostname, key.get_name(), key)
        try:
            os.makedirs(os.path.dirname(hosts_path), exist_ok=True)
            hk.save(hosts_path)
        except OSError:
            pass

    def _load_key(self, path: str, passphrase: str | None):
        last_err: Exception | None = None
        for cls in (
            paramiko.Ed25519Key,
            paramiko.ECDSAKey,
            paramiko.RSAKey,
            paramiko.DSSKey,
        ):
            try:
                return cls.from_private_key_file(path, password=passphrase or None)
            except paramiko.PasswordRequiredException as e:
                raise e
            except Exception as e:
                last_err = e
                continue
        raise ValueError(
            f"Could not load private key {path}: {last_err}"
        )

    def _authenticate(self, transport: paramiko.Transport) -> None:
        req = self.req
        username = req.username

        pkey = None
        key_load_error: str | None = None
        if req.key_path:
            try:
                pkey = self._load_key(req.key_path, req.key_passphrase)
            except paramiko.PasswordRequiredException:
                key_load_error = (
                    f"Private key {req.key_path} is encrypted; "
                    "no passphrase given — skipping publickey auth"
                )
            except Exception as e:
                key_load_error = (
                    f"Could not load key {req.key_path} ({e}) — "
                    "skipping publickey auth"
                )

        # Probe allowed methods.
        try:
            allowed = transport.auth_none(username)
        except paramiko.BadAuthenticationType as e:
            allowed = list(e.allowed_types)
        except paramiko.AuthenticationException:
            allowed = ["password", "publickey", "keyboard-interactive"]

        tried: set[str] = set()
        password = req.password
        last_failure: str | None = key_load_error

        while not transport.is_authenticated():
            if not allowed:
                break
            # Prefer kbd-interactive over plain "password": on PAM-MFA servers
            # (Duo, etc.) the bare "password" method always fails because the
            # password is meant to be delivered via kbd-interactive. Trying it
            # standalone burns a failed-auth count on the connection and some
            # servers close the connection if too many failures accumulate.
            method = next(
                (m for m in ("publickey", "keyboard-interactive", "password")
                 if m in allowed and m not in tried),
                None,
            )
            if method is None:
                method = next((m for m in allowed if m not in tried), None)
            if method is None:
                break
            tried.add(method)

            try:
                if method == "publickey":
                    if pkey is None:
                        continue
                    next_allowed = transport.auth_publickey(username, pkey)
                elif method == "password":
                    if password is None:
                        resp = self._kbd_handler(
                            "Password", "",
                            [(f"Password for {username}@{self.req.host}: ", False)],
                        )
                        password = resp[0] if resp else ""
                    next_allowed = transport.auth_password(username, password)
                elif method == "keyboard-interactive":
                    next_allowed = transport.auth_interactive(
                        username, self._kbd_handler,
                    )
                else:
                    continue
                allowed = list(next_allowed) if next_allowed else []
                # If a method partially succeeded, reset 'tried' for remaining
                # methods so we will try them again as additional factors.
                tried = tried & set(allowed)
            except paramiko.BadAuthenticationType as e:
                allowed = list(e.allowed_types)
            except paramiko.AuthenticationException as e:
                last_failure = f"{method} rejected: {e}"
                continue

        if not transport.is_authenticated():
            msg = "Authentication failed"
            if last_failure:
                msg += f" — {last_failure}"
            raise paramiko.AuthenticationException(msg)

    def _open_sftp_with_retry(self, transport: paramiko.Transport):
        """Open SFTP, retrying once if the first channel is killed mid-handshake."""
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                sftp = paramiko.SFTPClient.from_transport(transport)
                if sftp is None:
                    raise SSHException("from_transport returned None")
                return sftp
            except EOFError as e:
                last_err = e
                # Server killed the channel before sending SSH_FXP_VERSION.
                # Wait a beat and try one more time on a fresh channel.
                time.sleep(0.6)
                continue
            except Exception as e:
                last_err = e
                break

        # All retries exhausted — surface a clear message and the log path.
        raise SSHException(
            "Auth succeeded, but the SFTP subsystem closed the channel "
            "before sending the version handshake (after retry).\n\n"
            f"Underlying: {type(last_err).__name__}: {last_err}\n\n"
            f"Detailed paramiko trace was written to:\n  {PARAMIKO_LOG}\n"
            "Open that file and look at the last 60 lines — it will show "
            "the exact protocol packet where the server closed the channel."
        ) from last_err

    def run(self) -> None:
        transport = None
        sock = None
        try:
            host_label = (
                self.req.host if self.req.port == 22
                else f"[{self.req.host}]:{self.req.port}"
            )
            # Bypass paramiko's hostname-iteration quirk: it re-resolves the
            # name on each attempt, so on round-robin DNS where one IP refuses
            # it never actually rotates. socket.create_connection iterates the
            # resolved addresses directly and lands on a live one.
            sock = socket.create_connection(
                (self.req.host, self.req.port), timeout=20,
            )
            transport = paramiko.Transport(sock)
            transport.banner_timeout = 30
            transport.start_client(timeout=20)

            host_key = transport.get_remote_server_key()
            self._confirm_host_key(host_label, host_key)

            self._authenticate(transport)

            # Some PAM stacks (e.g. Duo on midway3) finalize the session
            # asynchronously after USERAUTH_SUCCESS. Opening a channel
            # immediately can race that finalization and the server then
            # closes our channel mid-handshake. A short pause sidesteps it.
            time.sleep(0.4)

            # Send keepalive packets so idle servers don't reap our session
            # when the laptop sleeps for a while.
            try:
                transport.set_keepalive(30)
            except Exception:
                pass

            sftp = self._open_sftp_with_retry(transport)
            backend = SFTPBackend(
                transport, sftp,
                self.req.username, self.req.host, self.req.port,
            )
            self.succeeded.emit(backend)
        except Exception as e:
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
            elif sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            self.failed.emit(f"{type(e).__name__}: {e}")


# --------------------------------------------------------- main-thread coordinator


class ConnectCoordinator(QObject):
    """Owns a ConnectWorker and presents prompts on the main thread."""

    # SFTPBackend, target_path (or None to use backend.home())
    succeeded = pyqtSignal(object, object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        parent_widget: QWidget,
        req: ConnectionRequest,
        target_path: str | None = None,
    ):
        super().__init__(parent_widget)
        self.parent_widget = parent_widget
        self.target_path = target_path
        self.worker = ConnectWorker(req)
        self.worker.need_kbd_interactive.connect(
            self._on_kbd_interactive, Qt.QueuedConnection,
        )
        self.worker.need_host_key_confirm.connect(
            self._on_host_key, Qt.QueuedConnection,
        )
        self.worker.succeeded.connect(self._on_worker_ok)
        self.worker.failed.connect(self.failed)

    def start(self) -> None:
        self.worker.start()

    def _on_worker_ok(self, backend) -> None:
        self.succeeded.emit(backend, self.target_path)

    def _on_kbd_interactive(self, title, instructions, prompts, q):
        dlg = KbdInteractiveDialog(title, instructions, prompts, self.parent_widget)
        if dlg.exec_() == QDialog.Accepted:
            q.put(dlg.responses())
        else:
            q.put(None)

    def _on_host_key(self, hostname, key_type, fingerprint, q):
        msg = (
            f"The authenticity of host '{hostname}' can't be established.\n\n"
            f"{key_type} key fingerprint: {fingerprint}\n\n"
            "Continue connecting and add this key to known_hosts?"
        )
        ret = QMessageBox.question(
            self.parent_widget, "Verify host key", msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        q.put(ret == QMessageBox.Yes)
