"""Filesystem backends: local and SFTP, behind a common interface."""
from __future__ import annotations

import os
import posixpath
import shutil
import stat as stat_mod
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import BinaryIO, Iterable

import paramiko


@dataclass
class FSEntry:
    name: str
    path: str
    is_dir: bool
    size: int
    mtime: float
    mode: int = 0
    uid: int = -1
    gid: int = -1
    owner: str = ""
    ctime: float = 0.0


class Backend(ABC):
    is_local: bool = False
    id: str = ""

    @abstractmethod
    def display_name(self) -> str: ...
    @abstractmethod
    def home(self) -> str: ...
    @abstractmethod
    def list(self, path: str) -> list[FSEntry]: ...
    @abstractmethod
    def join(self, parent: str, child: str) -> str: ...
    @abstractmethod
    def parent(self, path: str) -> str: ...
    @abstractmethod
    def mkdir(self, path: str) -> None: ...
    @abstractmethod
    def delete(self, path: str, is_dir: bool) -> None: ...
    @abstractmethod
    def rename(self, src: str, dst: str) -> None: ...
    @abstractmethod
    def open_read(self, path: str) -> BinaryIO: ...
    @abstractmethod
    def open_write(self, path: str) -> BinaryIO: ...
    @abstractmethod
    def read_bytes(self, path: str, max_bytes: int | None = None) -> bytes: ...
    @abstractmethod
    def stat_is_dir(self, path: str) -> bool: ...
    @abstractmethod
    def stat_size(self, path: str) -> int: ...

    def close(self) -> None:
        pass


class LocalBackend(Backend):
    is_local = True
    id = "local"

    def display_name(self) -> str:
        return "Local"

    def home(self) -> str:
        return os.path.expanduser("~")

    def list(self, path: str) -> list[FSEntry]:
        out: list[FSEntry] = []
        with os.scandir(path) as it:
            for e in it:
                try:
                    st = e.stat()
                    out.append(FSEntry(
                        name=e.name,
                        path=os.path.join(path, e.name),
                        is_dir=e.is_dir(),
                        size=st.st_size,
                        mtime=st.st_mtime,
                        mode=st.st_mode,
                        uid=st.st_uid,
                        gid=st.st_gid,
                        owner=_local_owner_name(st.st_uid),
                        ctime=st.st_ctime,
                    ))
                except OSError:
                    continue
        out.sort(key=lambda x: (not x.is_dir, x.name.lower()))
        return out

    def join(self, parent: str, child: str) -> str:
        return os.path.join(parent, child)

    def parent(self, path: str) -> str:
        p = os.path.dirname(path.rstrip(os.sep)) or path
        return p

    def mkdir(self, path: str) -> None:
        os.mkdir(path)

    def delete(self, path: str, is_dir: bool) -> None:
        if is_dir:
            shutil.rmtree(path)
        else:
            os.remove(path)

    def rename(self, src: str, dst: str) -> None:
        os.replace(src, dst)

    def open_read(self, path: str) -> BinaryIO:
        return open(path, "rb")

    def open_write(self, path: str) -> BinaryIO:
        return open(path, "wb")

    def read_bytes(self, path: str, max_bytes: int | None = None) -> bytes:
        with open(path, "rb") as f:
            return f.read() if max_bytes is None else f.read(max_bytes)

    def stat_is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def stat_size(self, path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0


class _SFTPHandle:
    """File-like wrapper that closes its dedicated SFTP channel on close()."""

    def __init__(self, sftp: paramiko.SFTPClient, f):
        self._sftp = sftp
        self._f = f

    def read(self, n: int = -1):
        return self._f.read() if n == -1 else self._f.read(n)

    def write(self, data) -> int:
        return self._f.write(data)

    def close(self) -> None:
        try:
            self._f.close()
        finally:
            try:
                self._sftp.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class SFTPBackend(Backend):
    is_local = False

    def __init__(
        self,
        transport: paramiko.Transport,
        sftp: paramiko.SFTPClient,
        username: str,
        host: str,
        port: int,
    ):
        self.transport = transport
        self.sftp = sftp
        self.username = username
        self.host = host
        self.port = port
        self.id = f"sftp://{username}@{host}:{port}"
        # Serialize access to the shared self.sftp; streaming ops use their
        # own dedicated channel and don't take this lock.
        self._lock = threading.RLock()

    def display_name(self) -> str:
        suffix = "" if self.port == 22 else f":{self.port}"
        return f"{self.username}@{self.host}{suffix}"

    def _new_sftp(self) -> paramiko.SFTPClient:
        c = self.transport.open_sftp_client()
        if c is None:
            raise IOError("Failed to open SFTP channel")
        return c

    def home(self) -> str:
        # SFTPClient.normalize('.') resolves to the user's home on most servers.
        with self._lock:
            try:
                return self.sftp.normalize(".")
            except Exception:
                return "/"

    def list(self, path: str) -> list[FSEntry]:
        with self._lock:
            attrs = list(self.sftp.listdir_attr(path))
        out: list[FSEntry] = []
        for attr in attrs:
            mode = attr.st_mode or 0
            is_dir = stat_mod.S_ISDIR(mode)
            uid = attr.st_uid if attr.st_uid is not None else -1
            gid = attr.st_gid if attr.st_gid is not None else -1
            out.append(FSEntry(
                name=attr.filename,
                path=posixpath.join(path, attr.filename),
                is_dir=is_dir,
                size=attr.st_size or 0,
                mtime=float(attr.st_mtime or 0),
                mode=mode,
                uid=uid,
                gid=gid,
                # SFTP exposes uid only — no name resolution without an
                # extra round-trip; show numeric.
                owner=str(uid) if uid >= 0 else "",
                ctime=0.0,
            ))
        out.sort(key=lambda x: (not x.is_dir, x.name.lower()))
        return out

    def join(self, parent: str, child: str) -> str:
        return posixpath.join(parent, child)

    def parent(self, path: str) -> str:
        p = posixpath.dirname(path.rstrip("/")) or "/"
        return p

    def mkdir(self, path: str) -> None:
        with self._lock:
            self.sftp.mkdir(path)

    def delete(self, path: str, is_dir: bool) -> None:
        if is_dir:
            self._rmtree(path)
        else:
            with self._lock:
                self.sftp.remove(path)

    def _rmtree(self, path: str) -> None:
        for entry in self.list(path):
            self.delete(entry.path, entry.is_dir)
        with self._lock:
            self.sftp.rmdir(path)

    def rename(self, src: str, dst: str) -> None:
        with self._lock:
            self.sftp.posix_rename(src, dst)

    def open_read(self, path: str) -> BinaryIO:
        sftp = self._new_sftp()
        try:
            f = sftp.open(path, "rb")
            f.prefetch()
        except Exception:
            sftp.close()
            raise
        return _SFTPHandle(sftp, f)  # type: ignore[return-value]

    def open_write(self, path: str) -> BinaryIO:
        sftp = self._new_sftp()
        try:
            f = sftp.open(path, "wb")
            f.set_pipelined(True)
        except Exception:
            sftp.close()
            raise
        return _SFTPHandle(sftp, f)  # type: ignore[return-value]

    def read_bytes(self, path: str, max_bytes: int | None = None) -> bytes:
        sftp = self._new_sftp()
        try:
            with sftp.open(path, "rb") as f:
                f.prefetch()
                return f.read() if max_bytes is None else f.read(max_bytes)
        finally:
            sftp.close()

    def stat_is_dir(self, path: str) -> bool:
        with self._lock:
            try:
                attr = self.sftp.stat(path)
                return stat_mod.S_ISDIR(attr.st_mode or 0)
            except OSError:
                return False

    def stat_size(self, path: str) -> int:
        with self._lock:
            try:
                attr = self.sftp.stat(path)
                return attr.st_size or 0
            except OSError:
                return 0

    def close(self) -> None:
        with self._lock:
            try:
                self.sftp.close()
            finally:
                self.transport.close()


def _local_owner_name(uid: int) -> str:
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError):
        return str(uid)


def iter_walk(backend: Backend, path: str) -> Iterable[tuple[str, bool, int]]:
    """Yield (path, is_dir, size) for path and everything under it (dirs first)."""
    if not backend.stat_is_dir(path):
        yield (path, False, backend.stat_size(path))
        return
    yield (path, True, 0)
    for entry in backend.list(path):
        if entry.is_dir:
            yield from iter_walk(backend, entry.path)
        else:
            yield (entry.path, False, entry.size)
