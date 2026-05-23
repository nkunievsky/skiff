"""Image preview pane: small thumbnail of the currently-selected image file."""
from __future__ import annotations

import os
from dataclasses import dataclass

from PyQt5.QtCore import QThread, Qt, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .fs import Backend

IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
    ".tif", ".tiff", ".ico", ".heic",
}
PREVIEW_BYTE_LIMIT = 8 * 1024 * 1024  # don't pull >8MB across SFTP for a thumbnail


def is_image(name: str) -> bool:
    _, ext = os.path.splitext(name)
    return ext.lower() in IMAGE_EXTS


@dataclass
class PreviewRequest:
    backend_id: str
    path: str
    name: str
    size: int


class _FetchWorker(QThread):
    """Fetch raw bytes for a remote image, off the UI thread."""

    fetched = pyqtSignal(str, bytes)   # path, bytes
    failed = pyqtSignal(str, str)      # path, message

    def __init__(self, backend: Backend, path: str):
        super().__init__()
        self.backend = backend
        self.path = path

    def run(self) -> None:
        try:
            data = self.backend.read_bytes(self.path, PREVIEW_BYTE_LIMIT)
            self.fetched.emit(self.path, data)
        except Exception as e:
            self.failed.emit(self.path, str(e))


class PreviewWidget(QFrame):
    """Compact preview frame: thumbnail + filename + size."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("previewFrame")
        self.setFrameShape(QFrame.NoFrame)
        self.setFixedHeight(140)

        self.image = QLabel()
        self.image.setFixedSize(180, 120)
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setObjectName("previewImage")

        self.title = QLabel("")
        self.title.setObjectName("previewTitle")
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("previewSubtitle")
        self.subtitle.setWordWrap(True)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(12, 0, 0, 0)
        text_col.addStretch(1)
        text_col.addWidget(self.title)
        text_col.addWidget(self.subtitle)
        text_col.addStretch(1)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.image)
        layout.addLayout(text_col, stretch=1)

        self._current_path: str | None = None
        self._workers: list[_FetchWorker] = []
        self._latest_seq: int = 0
        self._cache: dict[tuple[str, str], QPixmap] = {}
        self.clear()

    def clear(self) -> None:
        self._current_path = None
        self.image.clear()
        self.image.setText("no preview")
        self.title.setText("")
        self.subtitle.setText("")

    def show_for(self, backend: Backend, request: PreviewRequest) -> None:
        if not is_image(request.name):
            self.clear()
            return

        self._current_path = request.path
        self.title.setText(request.name)
        self.subtitle.setText(_fmt_size(request.size))

        cache_key = (request.backend_id, request.path)
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._set_pixmap(cached)
            return

        if backend.is_local:
            try:
                data = backend.read_bytes(request.path, PREVIEW_BYTE_LIMIT)
            except Exception as e:
                self.image.setText(f"can't read\n{e}")
                return
            self._handle_bytes(cache_key, request.path, data)
            return

        # SFTP: fetch in a worker. Keep a reference in _workers until the
        # thread emits `finished` — otherwise Python may GC the QThread
        # wrapper while the C++ thread is still running, which Qt aborts on.
        self.image.setText("loading…")
        self._latest_seq += 1
        seq = self._latest_seq
        target_path = request.path
        target_key = cache_key

        worker = _FetchWorker(backend, request.path)
        self._workers.append(worker)

        def on_fetched(path: str, data: bytes) -> None:
            if seq != self._latest_seq:
                return  # superseded by a newer selection
            if self._current_path != target_path:
                return
            self._handle_bytes(target_key, path, data)

        def on_failed(path: str, msg: str) -> None:
            if seq != self._latest_seq:
                return
            if self._current_path != target_path:
                return
            self.image.setText(f"failed\n{msg[:60]}")

        def on_finished() -> None:
            if worker in self._workers:
                self._workers.remove(worker)

        worker.fetched.connect(on_fetched)
        worker.failed.connect(on_failed)
        worker.finished.connect(on_finished)
        worker.start()

    def _handle_bytes(self, cache_key, path, data) -> None:
        pix = QPixmap()
        if not pix.loadFromData(data):
            self.image.setText("not an image")
            return
        self._cache[cache_key] = pix
        if self._current_path == path:
            self._set_pixmap(pix)

    def _set_pixmap(self, pix: QPixmap) -> None:
        scaled = pix.scaled(
            self.image.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.image.setPixmap(scaled)


def _fmt_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:,.0f} {u}" if u == "B" else f"{f:,.1f} {u}"
        f /= 1024
    return f"{n} B"
