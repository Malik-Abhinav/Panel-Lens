import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from persistent_cache import PersistentCache, ocr_cache_key, translation_cache_key


OCR = {"model": "ocr-v1", "preprocess": "rgb", "resize": "none", "filter": "v1"}
REGIONS = [{"original": " 안녕  세계 ", "type": "dialogue", "bbox": [1, 2, 3, 4]}]


def test_persistent_ocr_hit_and_restart(tmp_path):
    path = tmp_path / "cache.db"
    key = ocr_cache_key(b"same", OCR)
    PersistentCache(path).put("ocr", key, {"regions": REGIONS})
    value, source = PersistentCache(path).get("ocr", key)
    assert value["regions"] == REGIONS
    assert source == "disk_ocr"


def test_persistent_translation_hit_and_memory_source(tmp_path):
    cache = PersistentCache(tmp_path / "cache.db")
    key = translation_cache_key(REGIONS, {"model": "m1", "prompt": "p1", "context": []})
    cache.put("translation", key, {"regions": [{"translation": "Hello"}]})
    assert cache.get("translation", key)[1] == "memory_translation"


def test_key_invalidation_and_content_identity():
    assert ocr_cache_key(b"x", OCR) == ocr_cache_key(b"x", OCR)
    assert ocr_cache_key(b"x", OCR) != ocr_cache_key(b"y", OCR)
    assert ocr_cache_key(b"x", OCR) != ocr_cache_key(b"x", {**OCR, "model": "v2"})
    base = {"model": "m1", "prompt": "p1", "context_policy": "free", "context": []}
    key = translation_cache_key(REGIONS, base)
    assert key != translation_cache_key(REGIONS, {**base, "model": "m2"})
    assert key != translation_cache_key(REGIONS, {**base, "prompt": "p2"})
    assert key != translation_cache_key(REGIONS, {**base, "context": [{"korean": "a", "english": "b"}]})
    assert key != translation_cache_key(REGIONS, {**base, "context_policy": "rolling"})


def test_url_and_dom_identity_are_not_cache_identity():
    # URL/element IDs deliberately are not accepted by the key API.
    assert ocr_cache_key(b"bytes", OCR) == ocr_cache_key(b"bytes", OCR)


def test_eviction_and_bounded_payload(tmp_path):
    cache = PersistentCache(tmp_path / "cache.db", max_bytes=16_384, memory_entries=1)
    for index in range(30):
        cache.put("ocr", str(index), {"data": "x" * 1500, "index": index})
    stats = cache.stats()
    assert stats["payload_bytes"] + 16_384 <= stats["max_bytes"] + 16_384
    assert stats["entries"] < 30


def test_malformed_entry_recovers(tmp_path):
    path = tmp_path / "cache.db"
    cache = PersistentCache(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO cache_entries VALUES('ocr','bad','{',1,1,1)")
    assert cache.get("ocr", "bad") == (None, "miss")


def test_corrupt_database_recovers(tmp_path):
    path = tmp_path / "cache.db"
    path.write_bytes(b"not sqlite")
    cache = PersistentCache(path)
    cache.put("ocr", "ok", {"regions": []})
    assert cache.get("ocr", "ok")[0] == {"regions": []}


def test_concurrent_identical_access(tmp_path):
    cache = PersistentCache(tmp_path / "cache.db")
    def operation(_):
        cache.put("ocr", "same", {"regions": []})
        return cache.get("ocr", "same")[0]
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(value == {"regions": []} for value in pool.map(operation, range(32)))
