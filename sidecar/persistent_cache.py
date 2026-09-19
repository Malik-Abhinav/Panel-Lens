"""Versioned, bounded L2/L3 caches for OCR and translation results."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
OCR_KEY_VERSION = "ocr-key-v1"
TRANSLATION_KEY_VERSION = "translation-key-v1"
DEFAULT_MAX_BYTES = 256 * 1024 * 1024


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_hash(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


def ocr_cache_key(image_bytes: bytes, config: dict[str, Any]) -> str:
    identity = {"version": OCR_KEY_VERSION, "content_sha256": content_hash(image_bytes), "config": config}
    return hashlib.sha256(_canonical(identity)).hexdigest()


def translation_cache_key(regions: list[dict[str, Any]], config: dict[str, Any]) -> str:
    ordered = [
        {
            "text": " ".join(str(region.get("original", "")).split()),
            "type": str(region.get("type", "dialogue")),
            "order": index,
        }
        for index, region in enumerate(regions)
    ]
    identity = {"version": TRANSLATION_KEY_VERSION, "regions": ordered, "config": config}
    return hashlib.sha256(_canonical(identity)).hexdigest()


class PersistentCache:
    """Thread-safe SQLite cache with JSON validation and deterministic LRU eviction."""

    def __init__(self, path: str | Path, max_bytes: int = DEFAULT_MAX_BYTES, memory_entries: int = 64) -> None:
        self.path = Path(path)
        self.max_bytes = max(4096, int(max_bytes))
        self.memory_entries = max(1, int(memory_entries))
        self._memory: dict[str, OrderedDict[str, dict[str, Any]]] = {
            "ocr": OrderedDict(), "translation": OrderedDict()
        }
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, SCHEMA_VERSION):
                    raise RuntimeError(f"Unsupported cache schema version {version}")
                db.execute("BEGIN IMMEDIATE")
                db.execute("""CREATE TABLE IF NOT EXISTS cache_entries (
                    namespace TEXT NOT NULL CHECK(namespace IN ('ocr','translation')),
                    cache_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    payload_bytes INTEGER NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    last_access_ms INTEGER NOT NULL,
                    PRIMARY KEY(namespace, cache_key)
                )""")
                db.execute("CREATE INDEX IF NOT EXISTS cache_lru ON cache_entries(last_access_ms, namespace, cache_key)")
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                db.commit()
        except sqlite3.DatabaseError:
            self._recover_corrupt_database()

    def _recover_corrupt_database(self) -> None:
        stamp = int(time.time() * 1000)
        if self.path.exists():
            self.path.replace(self.path.with_name(f"{self.path.name}.corrupt-{stamp}"))
        for suffix in ("-wal", "-shm"):
            auxiliary = Path(str(self.path) + suffix)
            if auxiliary.exists():
                auxiliary.unlink()
        with self._connect() as db:
            db.execute("""CREATE TABLE cache_entries (
                namespace TEXT NOT NULL CHECK(namespace IN ('ocr','translation')),
                cache_key TEXT NOT NULL, payload TEXT NOT NULL, payload_bytes INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL, last_access_ms INTEGER NOT NULL,
                PRIMARY KEY(namespace, cache_key))""")
            db.execute("CREATE INDEX cache_lru ON cache_entries(last_access_ms, namespace, cache_key)")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def get(self, namespace: str, key: str) -> tuple[dict[str, Any] | None, str]:
        self._validate_namespace(namespace)
        with self._lock:
            memory = self._memory[namespace]
            if key in memory:
                memory.move_to_end(key)
                return copy.deepcopy(memory[key]), f"memory_{namespace}"
            try:
                with self._connect() as db:
                    row = db.execute(
                        "SELECT payload FROM cache_entries WHERE namespace=? AND cache_key=?", (namespace, key)
                    ).fetchone()
                    if row is None:
                        return None, "miss"
                    try:
                        value = json.loads(row[0])
                        if not isinstance(value, dict):
                            raise ValueError("cache payload must be an object")
                    except (json.JSONDecodeError, TypeError, ValueError):
                        db.execute("DELETE FROM cache_entries WHERE namespace=? AND cache_key=?", (namespace, key))
                        return None, "miss"
                    db.execute(
                        "UPDATE cache_entries SET last_access_ms=? WHERE namespace=? AND cache_key=?",
                        (int(time.time() * 1000), namespace, key),
                    )
            except sqlite3.DatabaseError:
                return None, "miss"
            self._remember(namespace, key, value)
            return copy.deepcopy(value), f"disk_{namespace}"

    def put(self, namespace: str, key: str, value: dict[str, Any]) -> None:
        self._validate_namespace(namespace)
        encoded = _canonical(value).decode("utf-8")
        now = int(time.time() * 1000)
        with self._lock:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    """INSERT INTO cache_entries(namespace,cache_key,payload,payload_bytes,created_at_ms,last_access_ms)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(namespace,cache_key) DO UPDATE SET
                    payload=excluded.payload,payload_bytes=excluded.payload_bytes,last_access_ms=excluded.last_access_ms""",
                    (namespace, key, encoded, len(encoded.encode("utf-8")), now, now),
                )
                self._evict(db)
                db.commit()
            self._remember(namespace, key, value)

    def _evict(self, db: sqlite3.Connection) -> None:
        while True:
            payload_total = db.execute("SELECT COALESCE(SUM(payload_bytes),0) FROM cache_entries").fetchone()[0]
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            minimum_overhead = page_size * 4
            if payload_total + minimum_overhead <= self.max_bytes:
                return
            victim = db.execute(
                "SELECT namespace,cache_key FROM cache_entries ORDER BY last_access_ms,namespace,cache_key LIMIT 1"
            ).fetchone()
            if victim is None:
                return
            db.execute("DELETE FROM cache_entries WHERE namespace=? AND cache_key=?", victim)
            self._memory[victim[0]].pop(victim[1], None)

    def clear(self, persistent: bool = True) -> None:
        with self._lock:
            for memory in self._memory.values():
                memory.clear()
            if persistent:
                with self._connect() as db:
                    db.execute("DELETE FROM cache_entries")

    def stats(self) -> dict[str, int]:
        with self._connect() as db:
            count, payload = db.execute("SELECT COUNT(*),COALESCE(SUM(payload_bytes),0) FROM cache_entries").fetchone()
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            pages = db.execute("PRAGMA page_count").fetchone()[0]
        return {"entries": count, "payload_bytes": payload, "database_bytes": page_size * pages, "max_bytes": self.max_bytes}

    def _remember(self, namespace: str, key: str, value: dict[str, Any]) -> None:
        memory = self._memory[namespace]
        memory[key] = copy.deepcopy(value)
        memory.move_to_end(key)
        while len(memory) > self.memory_entries:
            memory.popitem(last=False)

    @staticmethod
    def _validate_namespace(namespace: str) -> None:
        if namespace not in {"ocr", "translation"}:
            raise ValueError(f"Unknown cache namespace {namespace!r}")


def default_cache() -> PersistentCache:
    path = os.environ.get("PANELLENS_CACHE_DB", str(Path.home() / ".cache" / "panellens" / "cache.sqlite3"))
    maximum = int(os.environ.get("PANELLENS_CACHE_MAX_BYTES", str(DEFAULT_MAX_BYTES)))
    return PersistentCache(path, maximum)
