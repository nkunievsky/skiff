"""Cross-backend file transfers with progress."""
from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from .fs import Backend, iter_walk

CHUNK = 256 * 1024


@dataclass
class TransferJob:
    source_backend: Backend
    paths: list[str]
    dest_backend: Backend
    dest_dir: str


def _basename(backend: Backend, path: str) -> str:
    if backend.is_local:
        return os.path.basename(path.rstrip(os.sep))
    return posixpath.basename(path.rstrip("/"))


def _src_join(backend: Backend, parent: str, child: str) -> str:
    return backend.join(parent, child)


def _relative(backend: Backend, root: str, full: str) -> str:
    """Path of `full` relative to `root`, normalized to forward slashes.

    Returns "" when full equals root — that's the leaf-is-the-root case
    (a single-file transfer where the target is dest_root itself, not
    a child of it). Without this, posix/local join would see an
    absolute path on the right and discard dest_root entirely.
    """
    if full == root:
        return ""
    if backend.is_local:
        rel = os.path.relpath(full, root)
        return rel.replace(os.sep, "/")
    if not root.endswith("/"):
        root = root + "/"
    return full[len(root):] if full.startswith(root) else full


class TransferWorker(QThread):
    """Runs a single TransferJob; processes one file at a time."""

    started_file = pyqtSignal(str, int)        # rel_path, total_bytes
    progress = pyqtSignal(int)                 # bytes_done_for_current_file
    file_done = pyqtSignal()
    job_done = pyqtSignal(int, int, list)      # files_ok, files_failed, errors

    def __init__(self, job: TransferJob):
        super().__init__()
        self.job = job
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        ok = 0
        failed = 0
        errors: list[str] = []
        src = self.job.source_backend
        dst = self.job.dest_backend
        dest_dir = self.job.dest_dir

        for top in self.job.paths:
            if self._cancel:
                break
            top_name = _basename(src, top)
            dest_root = dst.join(dest_dir, top_name)
            try:
                walk = list(iter_walk(src, top))
            except Exception as e:
                errors.append(f"walk {top}: {e}")
                failed += 1
                continue

            for path, is_dir, size in walk:
                if self._cancel:
                    break
                rel = _relative(src, top, path)
                target = dest_root if rel in ("", ".") else dst.join(dest_root, rel)

                if is_dir:
                    try:
                        if not dst.stat_is_dir(target):
                            dst.mkdir(target)
                    except Exception as e:
                        errors.append(f"mkdir {target}: {e}")
                        failed += 1
                    continue

                # File
                try:
                    self._copy_file(src, path, dst, target, size)
                    ok += 1
                except Exception as e:
                    errors.append(f"{path} -> {target}: {e}")
                    failed += 1

        self.job_done.emit(ok, failed, errors)

    def _copy_file(self, src: Backend, src_path: str,
                   dst: Backend, dst_path: str, size: int) -> None:
        # Make sure parent exists on the destination.
        parent = dst.parent(dst_path)
        if parent and parent != dst_path and not dst.stat_is_dir(parent):
            try:
                dst.mkdir(parent)
            except Exception:
                pass  # may race with another mkdir; or parent already exists

        rel_label = _basename(src, src_path)
        self.started_file.emit(rel_label, size)

        rf = src.open_read(src_path)
        try:
            wf = dst.open_write(dst_path)
            try:
                done = 0
                while True:
                    if self._cancel:
                        break
                    buf = rf.read(CHUNK)
                    if not buf:
                        break
                    wf.write(buf)
                    done += len(buf)
                    self.progress.emit(done)
            finally:
                wf.close()
        finally:
            rf.close()
        self.file_done.emit()


class TransferQueue(QObject):
    """Serializes TransferJobs onto a single worker, surfaces signals."""

    job_started = pyqtSignal(object)        # TransferJob
    file_started = pyqtSignal(str, int)     # name, total_bytes
    progress = pyqtSignal(int)              # bytes_done
    file_done = pyqtSignal()
    job_finished = pyqtSignal(int, int, list)
    queue_idle = pyqtSignal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._pending: list[TransferJob] = []
        self._worker: TransferWorker | None = None

    def submit(self, job: TransferJob) -> None:
        self._pending.append(job)
        self._maybe_start()

    def _maybe_start(self) -> None:
        if self._worker is not None or not self._pending:
            return
        job = self._pending.pop(0)
        worker = TransferWorker(job)
        worker.started_file.connect(self.file_started)
        worker.progress.connect(self.progress)
        worker.file_done.connect(self.file_done)
        worker.job_done.connect(lambda ok, fail, errs: self._on_job_done(ok, fail, errs))
        self._worker = worker
        self.job_started.emit(job)
        worker.start()

    def _on_job_done(self, ok: int, fail: int, errs: list) -> None:
        self.job_finished.emit(ok, fail, errs)
        self._worker = None
        if self._pending:
            self._maybe_start()
        else:
            self.queue_idle.emit()
