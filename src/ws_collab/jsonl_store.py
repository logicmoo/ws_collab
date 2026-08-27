"""Durable append-only JSONL event streams.

Design goals (see task sections 5 and 6):

* Append-safe, single-writer writes with per-stream monotonic ``seq`` numbers.
* Tolerant reads: a torn final line never corrupts a read; the reader simply
  stops before the incomplete record and the cursor is not advanced past it.
* Efficient tailing: consumers seek to a byte offset carried in their cursor and
  read only new bytes -- the whole file is never repeatedly re-read.
* Rotation + retention with cursors that survive rotation (recovery by ``seq``).
* Restart recovery: sequence, offsets, and the idempotency window are rebuilt
  from a sidecar state file (or by a one-time bounded scan).
* Unknown fields on historical records are preserved, never dropped.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import ConflictError, CursorError
from .events import STREAMS, Event, utc_now_iso
from .ids import decode_cursor, encode_cursor


@dataclass
class ReadPage:
    events: list[Event]
    next_cursor: str
    has_more: bool
    server_time: str
    malformed: int = 0


@dataclass
class AppendResult:
    event: Event
    duplicate: bool
    cursor: str


class _StreamState:
    """Mutable per-stream bookkeeping guarded by a single writer lock."""

    def __init__(self, name: str, directory: Path, rotate_max_bytes: int, retention_max_files: int):
        self.name = name
        self.directory = directory
        self.filename = STREAMS.get(name) or f"{name}.jsonl"
        self.active_path = directory / self.filename
        self.rotate_max_bytes = rotate_max_bytes
        self.retention_max_files = retention_max_files
        self.lock = threading.RLock()
        self.seq = 0
        self.gen = 0
        self.active_start_seq = 1
        self.segments: list[dict[str, Any]] = []  # archived: {path, gen, start_seq, end_seq}
        self.idempotency: dict[str, dict[str, Any]] = {}
        self._recover()

    # ------------------------------------------------------------------ sidecar
    @property
    def state_path(self) -> Path:
        return self.directory / f"{self.filename}.state.json"

    def _write_state(self) -> None:
        payload = {
            "gen": self.gen,
            "seq": self.seq,
            "active_start_seq": self.active_start_seq,
            "segments": self.segments,
        }
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _recover(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.state_path.is_file():
            try:
                payload = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.gen = int(payload.get("gen", 0))
                self.seq = int(payload.get("seq", 0))
                self.active_start_seq = int(payload.get("active_start_seq", 1))
                self.segments = list(payload.get("segments", []))
            except (OSError, ValueError, json.JSONDecodeError):
                self._rebuild_from_scan()
        else:
            self._rebuild_from_scan()
        # Reconcile the persisted sequence with the file's true last record so a
        # crash between append and state write cannot lose or reuse a seq.
        self._reconcile_active_tail()
        self._rebuild_idempotency_window()

    def _rebuild_from_scan(self) -> None:
        self.gen = 0
        self.seq = 0
        self.active_start_seq = 1
        self.segments = []
        if not self.active_path.is_file():
            return
        max_seq = 0
        for _start, _end, line in _iter_complete_lines(self.active_path, 0):
            try:
                record = json.loads(line)
                seq = int(record.get("seq") or 0)
                max_seq = max(max_seq, seq)
            except (ValueError, json.JSONDecodeError):
                continue
        self.seq = max_seq
        self.active_start_seq = 1

    def _reconcile_active_tail(self) -> None:
        if not self.active_path.is_file():
            return
        last_seq = None
        for _start, _end, line in _iter_complete_lines(self.active_path, 0):
            try:
                record = json.loads(line)
                if record.get("seq") is not None:
                    last_seq = int(record["seq"])
            except (ValueError, json.JSONDecodeError):
                continue
        if last_seq is not None and last_seq > self.seq:
            self.seq = last_seq

    def _rebuild_idempotency_window(self) -> None:
        self.idempotency.clear()
        if not self.active_path.is_file():
            return
        for _start, end, line in _iter_complete_lines(self.active_path, 0):
            try:
                record = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            key = record.get("idempotency_key")
            if key:
                self.idempotency[key] = {"id": record.get("id"), "seq": record.get("seq"), "offset": end}

    # ------------------------------------------------------------------- append
    def _repair_torn_final_line(self) -> None:
        """Terminate an unterminated final line left behind by a crashed writer.

        Without this, the next append would be concatenated onto the partial
        record, silently corrupting both. Closing the line instead turns it into
        a single malformed (but bounded) record that readers report and skip.
        """

        if not self.active_path.is_file():
            return
        try:
            size = self.active_path.stat().st_size
        except OSError:
            return
        if size == 0:
            return
        with open(self.active_path, "rb") as handle:
            handle.seek(size - 1)
            last = handle.read(1)
        if last != b"\n":
            with open(self.active_path, "ab") as handle:
                handle.write(b"\n")
                handle.flush()
                os.fsync(handle.fileno())

    def append(self, event: Event, on_write: Callable[[Event], None] | None = None) -> AppendResult:
        with self.lock:
            if event.idempotency_key and event.idempotency_key in self.idempotency:
                existing = self.idempotency[event.idempotency_key]
                cursor = encode_cursor(
                    {"s": self.name, "seq": existing["seq"], "off": existing.get("offset", 0), "gen": self.gen}
                )
                dup = Event.from_dict({**event.to_dict(), "id": existing["id"], "seq": existing["seq"]})
                return AppendResult(event=dup, duplicate=True, cursor=cursor)

            self._repair_torn_final_line()
            self.seq += 1
            event.seq = self.seq
            event.ensure_identity()
            line = event.to_line() + "\n"
            with open(self.active_path, "a", encoding="utf-8", newline="") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            end_offset = self.active_path.stat().st_size
            if event.idempotency_key:
                self.idempotency[event.idempotency_key] = {
                    "id": event.id,
                    "seq": event.seq,
                    "offset": end_offset,
                }
            self._write_state()
            cursor = encode_cursor({"s": self.name, "seq": event.seq, "off": end_offset, "gen": self.gen})
            if on_write is not None:
                on_write(event)
            self._maybe_rotate()
            return AppendResult(event=event, duplicate=False, cursor=cursor)

    def _maybe_rotate(self) -> None:
        try:
            size = self.active_path.stat().st_size
        except OSError:
            return
        if size < self.rotate_max_bytes:
            return
        segment_path = self.directory / f"{self.filename}.{self.gen}"
        os.replace(self.active_path, segment_path)
        self.segments.append(
            {
                "path": str(segment_path),
                "gen": self.gen,
                "start_seq": self.active_start_seq,
                "end_seq": self.seq,
            }
        )
        self.gen += 1
        self.active_start_seq = self.seq + 1
        self.active_path.write_text("", encoding="utf-8")
        self._apply_retention()
        self._write_state()
        self.idempotency.clear()

    def _apply_retention(self) -> None:
        while len(self.segments) > self.retention_max_files:
            oldest = self.segments.pop(0)
            try:
                Path(oldest["path"]).unlink(missing_ok=True)
            except OSError:
                pass

    # --------------------------------------------------------------------- read
    def _ordered_sources(self) -> list[dict[str, Any]]:
        sources = list(self.segments)
        sources.append(
            {
                "path": str(self.active_path),
                "gen": self.gen,
                "start_seq": self.active_start_seq,
                "end_seq": self.seq,
                "active": True,
            }
        )
        return sources

    def iter_from_seq(self, after_seq: int) -> Iterator[tuple[Event | None, str, int, int, int]]:
        """Yield ``(event_or_None, kind, seq, end_offset, gen)`` for records after seq.

        ``kind`` is ``"event"`` or ``"malformed"``. ``event`` is ``None`` for a
        malformed (corrupt but newline-terminated) line so the admin can surface
        a malformed-line marker while still advancing past it. ``gen`` is the
        generation of the file the record came from, which lets the reader decide
        whether a byte-offset hot-path hint is valid for the next cursor.
        """

        for source in self._ordered_sources():
            # Archived segments can be skipped when their whole range is behind
            # the cursor, but the ACTIVE file must always be scanned: its cached
            # end position can lag reality (external writer, restored file), and
            # skipping it would silently hide the live tail.
            if not source.get("active") and source["end_seq"] is not None and source["end_seq"] <= after_seq:
                continue
            path = Path(source["path"])
            if not path.is_file():
                continue
            gen = source["gen"]
            for _start, end, line in _iter_complete_lines(path, 0):
                try:
                    record = json.loads(line)
                    seq = int(record.get("seq") or 0)
                except (ValueError, json.JSONDecodeError):
                    yield None, "malformed", after_seq, end, gen
                    continue
                if seq <= after_seq:
                    continue
                yield Event.from_dict(record), "event", seq, end, gen

    def read(
        self,
        after_cursor: str | None,
        limit: int,
        predicate: Callable[[Event], bool] | None = None,
    ) -> ReadPage:
        with self.lock:
            after_seq = 0
            use_hot = False
            hot_offset = 0
            incoming_gen = self.gen
            if after_cursor:
                payload = _decode_stream_cursor(after_cursor, self.name)
                after_seq = int(payload.get("seq", 0))
                incoming_gen = int(payload.get("gen", self.gen))
                if after_seq > self.seq:
                    # Position beyond EOF (truncation/replacement): recover to end.
                    raise CursorError(
                        "cursor is beyond the end of the stream",
                        details={"recovery": self._cursor_at_seq(self.seq)},
                    )
                # The byte-offset fast path is only valid when the cursor points
                # inside the current active file; otherwise resolve by seq.
                if incoming_gen == self.gen and after_seq >= self.active_start_seq - 1:
                    use_hot = True
                    hot_offset = int(payload.get("off", 0))

            events: list[Event] = []
            malformed = 0
            last_seq = after_seq
            last_offset = hot_offset if use_hot else 0
            last_gen = self.gen if use_hot else incoming_gen
            has_more = False

            iterator = self._hot_iter(hot_offset, after_seq) if use_hot else self.iter_from_seq(after_seq)
            for event, kind, seq, end_offset, gen in iterator:
                if kind == "malformed":
                    malformed += 1
                    continue
                assert event is not None
                if len(events) >= limit:
                    has_more = True
                    break
                last_seq = seq
                last_offset = end_offset
                last_gen = gen
                if predicate is not None and not predicate(event):
                    continue
                events.append(event)

            # A byte offset is only meaningful when the last record read lives in
            # the active file; for archived positions we drop the offset so the
            # next read takes the (correct) seq-based cold path.
            if last_gen == self.gen:
                cursor_payload = {"s": self.name, "seq": last_seq, "off": last_offset, "gen": self.gen}
            else:
                cursor_payload = {"s": self.name, "seq": last_seq, "off": 0, "gen": last_gen}
            return ReadPage(
                events=events,
                next_cursor=encode_cursor(cursor_payload),
                has_more=has_more,
                server_time=utc_now_iso(),
                malformed=malformed,
            )

    def _hot_iter(self, offset: int, after_seq: int) -> Iterator[tuple[Event | None, str, int, int, int]]:
        if not self.active_path.is_file():
            return
        size = self.active_path.stat().st_size
        if offset > size:
            # Truncated underneath us: fall back to a full seq-based resolution.
            yield from self.iter_from_seq(after_seq)
            return
        for _start, end, line in _iter_complete_lines(self.active_path, offset):
            try:
                record = json.loads(line)
                seq = int(record.get("seq") or 0)
            except (ValueError, json.JSONDecodeError):
                yield None, "malformed", after_seq, end, self.gen
                continue
            if seq <= after_seq:
                continue
            yield Event.from_dict(record), "event", seq, end, self.gen

    def tail(self, count: int, predicate: Callable[[Event], bool] | None = None) -> list[Event]:
        with self.lock:
            collected: list[Event] = []
            for source in reversed(self._ordered_sources()):
                path = Path(source["path"])
                if not path.is_file():
                    continue
                segment_events: list[Event] = []
                for _start, _end, line in _iter_complete_lines(path, 0):
                    try:
                        record = json.loads(line)
                    except (ValueError, json.JSONDecodeError):
                        continue
                    event = Event.from_dict(record)
                    if predicate is None or predicate(event):
                        segment_events.append(event)
                collected = segment_events + collected
                if len(collected) >= count:
                    break
            return collected[-count:] if count else collected

    def cursor_at_start(self) -> str:
        return encode_cursor({"s": self.name, "seq": 0, "off": 0, "gen": self.gen})

    def cursor_at_end(self) -> str:
        with self.lock:
            offset = self.active_path.stat().st_size if self.active_path.is_file() else 0
            return encode_cursor({"s": self.name, "seq": self.seq, "off": offset, "gen": self.gen})

    def _cursor_at_seq(self, seq: int) -> str:
        return encode_cursor({"s": self.name, "seq": max(0, seq), "off": 0, "gen": self.gen})

    def cursor_at_seq(self, seq: int) -> str:
        with self.lock:
            seq = max(0, min(seq, self.seq))
            return self._cursor_at_seq(seq)

    def stats(self) -> dict[str, Any]:
        with self.lock:
            size = self.active_path.stat().st_size if self.active_path.is_file() else 0
            return {
                "stream": self.name,
                "filename": self.filename,
                "seq": self.seq,
                "gen": self.gen,
                "active_bytes": size,
                "segments": len(self.segments),
                "active_start_seq": self.active_start_seq,
            }

    def describe_files(self) -> dict[str, Any]:
        """Operational view of this stream's on-disk files.

        Exposed so operators, diagnostics, and tests can locate a stream's
        storage without hard-coding the layout. Concrete filenames are an
        implementation detail that may change; always resolve them from here.
        """

        with self.lock:
            return {
                "stream": self.name,
                "active_path": str(self.active_path),
                "recovery_path": str(self.state_path),
                "segment_paths": [segment["path"] for segment in self.segments],
            }


def _iter_complete_lines(path: Path, start_offset: int) -> Iterator[tuple[int, int, bytes]]:
    """Yield ``(start_offset, end_offset, line_bytes)`` for complete lines only.

    A trailing partial line (no terminating newline) is never yielded, which is
    how partial-final-line tolerance is implemented: the reader simply stops and
    the caller's cursor stays at the last complete record.
    """

    try:
        handle = open(path, "rb")
    except OSError:
        return
    with handle:
        handle.seek(start_offset)
        buffer = b""
        offset = start_offset
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            buffer += chunk
            while True:
                newline = buffer.find(b"\n")
                if newline == -1:
                    break
                line = buffer[:newline]
                line_start = offset
                offset += newline + 1
                buffer = buffer[newline + 1 :]
                if line.strip():
                    yield line_start, offset, line


def _decode_stream_cursor(token: str, stream: str) -> dict[str, Any]:
    try:
        payload = decode_cursor(token)
    except ValueError as error:
        raise CursorError(
            f"malformed cursor: {error}",
            details={"recovery": encode_cursor({"s": stream, "seq": 0, "off": 0, "gen": 0})},
        ) from error
    if payload.get("s") != stream:
        raise CursorError(
            "cursor belongs to a different stream",
            details={"cursor_stream": payload.get("s"), "expected": stream},
        )
    return payload


class JsonlStore:
    """A directory of durable JSONL streams with a shared single-writer guard."""

    def __init__(self, directory: str | Path, *, rotate_max_bytes: int = 64 * 1024 * 1024, retention_max_files: int = 20):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.rotate_max_bytes = rotate_max_bytes
        self.retention_max_files = retention_max_files
        self._streams: dict[str, _StreamState] = {}
        self._dynamic: set[str] = set()
        self._streams_lock = threading.Lock()
        self._owner_lock_handle = None
        self._acquire_owner_lock()

    def _acquire_owner_lock(self) -> None:
        """Best-effort exclusive lock so only one writer owns the data directory."""

        lock_path = self.directory / ".ws_collab.lock"
        owner_path = self.directory / ".ws_collab.owner.json"
        try:
            handle = open(lock_path, "a+")
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - exercised on POSIX
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._owner_lock_handle = handle
            self._write_owner_stamp(owner_path)
        except OSError as error:
            details: dict[str, Any] = {"directory": str(self.directory), "reason": str(error)}
            details.update(self._describe_current_owner(owner_path))
            raise ConflictError(
                "another writer already owns this JSONL directory",
                details=details,
            ) from error

    def _write_owner_stamp(self, owner_path: Path) -> None:
        """Record who holds the lock so a conflicting starter can name the owner."""

        import sys

        try:
            owner_path.write_text(json.dumps({
                "pid": os.getpid(),
                "argv": sys.argv,
                "executable": sys.executable,
                "cwd": str(Path.cwd()),
                "started": utc_now_iso(),
            }, indent=2), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _describe_current_owner(owner_path: Path) -> dict[str, Any]:
        """Best-effort report of the current lock owner and whether it is alive."""

        try:
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"owner": None, "hint": "owner stamp missing; find the process holding .ws_collab.lock"}
        alive: bool | None = None
        pid = owner.get("pid")
        if isinstance(pid, int):
            try:
                import psutil  # type: ignore

                alive = psutil.pid_exists(pid)
            except ImportError:
                if os.name == "nt":
                    import ctypes

                    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
                    if handle:
                        ctypes.windll.kernel32.CloseHandle(handle)
                        alive = True
                    else:
                        alive = False
                else:  # pragma: no cover - exercised on POSIX
                    try:
                        os.kill(pid, 0)
                        alive = True
                    except ProcessLookupError:
                        alive = False
                    except OSError:
                        alive = None
        hint = (
            f"stop PID {pid} (or wait for it to exit) before starting another writer"
            if alive
            else "owner appears dead; if the lock persists remove stale handles or reboot"
        )
        return {"owner": owner, "owner_alive": alive, "hint": hint}

    def stream(self, name: str) -> _StreamState:
        if name not in STREAMS and name not in self._dynamic:
            from .errors import ValidationError

            raise ValidationError(f"unknown stream: {name!r}", details={"allowed": sorted(STREAMS) + sorted(self._dynamic)})
        with self._streams_lock:
            state = self._streams.get(name)
            if state is None:
                state = _StreamState(name, self.directory, self.rotate_max_bytes, self.retention_max_files)
                self._streams[name] = state
            return state

    def register_mailbox(self, name: str) -> None:
        """Permit a client-created ("dynamic") mailbox to be hosted by name.

        The backing JSONL file is created lazily on first append/read, exactly
        like the built-in streams."""
        with self._streams_lock:
            self._dynamic.add(name)

    def append(self, event: Event, on_write: Callable[[Event], None] | None = None) -> AppendResult:
        return self.stream(event.stream).append(event, on_write=on_write)

    def read(
        self,
        stream: str,
        after_cursor: str | None,
        limit: int,
        predicate: Callable[[Event], bool] | None = None,
    ) -> ReadPage:
        return self.stream(stream).read(after_cursor, limit, predicate)

    def tail(self, stream: str, count: int, predicate: Callable[[Event], bool] | None = None) -> list[Event]:
        return self.stream(stream).tail(count, predicate)

    def stats(self) -> list[dict[str, Any]]:
        names = list(STREAMS) + sorted(self._dynamic)
        return [self.stream(name).stats() for name in names]

    def close(self) -> None:
        if self._owner_lock_handle is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._owner_lock_handle.seek(0)
                    msvcrt.locking(self._owner_lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover
                    import fcntl

                    fcntl.flock(self._owner_lock_handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._owner_lock_handle.close()
            self._owner_lock_handle = None
            try:
                (self.directory / ".ws_collab.owner.json").unlink(missing_ok=True)
            except OSError:
                pass
