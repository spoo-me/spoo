"""A tiny in-memory stand-in for a pymongo AsyncCollection.

Enough of the query language for the repositories under test: equality on
top-level and dotted keys, ``$ne``, ``$in``, ``$nin``, ``$gt``, ``$gte``,
``$lt``, ``$lte``, ``$exists`` and ``$or``; updates with ``$set``, ``$setOnInsert``
and ``$unset``; one unique key per collection so DuplicateKeyError behaves.
"""

from __future__ import annotations

import copy
from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError


def _get(doc: dict, path: str) -> Any:
    cur: Any = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _match_value(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict) and any(k.startswith("$") for k in expected):
        for op, arg in expected.items():
            if op == "$ne" and actual == arg:
                return False
            if op == "$in" and actual not in arg:
                return False
            if op == "$nin" and actual in arg:
                return False
            if op == "$exists" and (actual is not None) != bool(arg):
                return False
            if op in ("$gt", "$gte", "$lt", "$lte"):
                if actual is None:
                    return False
                if op == "$gt" and not actual > arg:
                    return False
                if op == "$gte" and not actual >= arg:
                    return False
                if op == "$lt" and not actual < arg:
                    return False
                if op == "$lte" and not actual <= arg:
                    return False
        return True
    if hasattr(expected, "value") and not isinstance(expected, dict):
        expected = expected.value
    return actual == expected


def _matches(doc: dict, query: dict) -> bool:
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(doc, q) for q in expected):
                return False
            continue
        if not _match_value(_get(doc, key), expected):
            return False
    return True


class _Result:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeCursor:
    def __init__(self, docs: list[dict]):
        self._docs = docs

    def sort(self, key, direction=1):
        if isinstance(key, list):
            key, direction = key[0]
        self._docs = sorted(
            self._docs,
            key=lambda d: (_get(d, key) is None, _get(d, key) or 0),
            reverse=direction == -1,
        )
        return self

    def skip(self, n):
        self._docs = self._docs[n:]
        return self

    def limit(self, n):
        if n:
            self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return [copy.deepcopy(d) for d in self._docs]


class FakeCollection:
    def __init__(self, name: str, *, unique: tuple[str, ...] = ()):
        self.name = name
        self.docs: list[dict] = []
        self._unique = unique

    def _check_unique(self, doc: dict, exclude_id=None):
        for key in self._unique:
            value = _get(doc, key)
            for other in self.docs:
                if other.get("_id") != exclude_id and _get(other, key) == value:
                    raise DuplicateKeyError(f"duplicate {key}")

    async def find_one(self, query: dict, projection=None):
        for d in self.docs:
            if _matches(d, query):
                return copy.deepcopy(d)
        return None

    def find(self, query: dict | None = None, projection=None):
        return FakeCursor([d for d in self.docs if _matches(d, query or {})])

    async def insert_one(self, doc: dict):
        doc = copy.deepcopy(doc)
        doc.setdefault("_id", ObjectId())
        self._check_unique(doc)
        self.docs.append(doc)
        return _Result(inserted_id=doc["_id"])

    async def count_documents(self, query: dict):
        return sum(1 for d in self.docs if _matches(d, query))

    async def distinct(self, key: str, query: dict | None = None):
        values = [_get(d, key) for d in self.docs if _matches(d, query or {})]
        return list(dict.fromkeys(values))

    def _apply(self, doc: dict, ops: dict, *, inserting: bool) -> bool:
        changed = False
        for k, v in ops.get("$set", {}).items():
            if doc.get(k) != v:
                doc[k] = copy.deepcopy(v)
                changed = True
        if inserting:
            for k, v in ops.get("$setOnInsert", {}).items():
                doc[k] = copy.deepcopy(v)
        for k in ops.get("$unset", {}):
            parent, _, leaf = k.rpartition(".")
            target = _get(doc, parent) if parent else doc
            if isinstance(target, dict) and leaf in target:
                del target[leaf]
                changed = True
        for k, v in ops.get("$inc", {}).items():
            doc[k] = doc.get(k, 0) + v
            changed = True
        return changed

    async def update_one(self, query: dict, ops: dict, upsert: bool = False):
        for d in self.docs:
            if _matches(d, query):
                modified = self._apply(d, ops, inserting=False)
                return _Result(
                    matched_count=1, modified_count=int(modified), upserted_id=None
                )
        if upsert:
            doc = {k: v for k, v in query.items() if not k.startswith("$")}
            self._apply(doc, ops, inserting=True)
            doc.setdefault("_id", ObjectId())
            self._check_unique(doc)
            self.docs.append(doc)
            return _Result(matched_count=0, modified_count=0, upserted_id=doc["_id"])
        return _Result(matched_count=0, modified_count=0, upserted_id=None)

    async def update_many(self, query: dict, ops: dict):
        n = 0
        for d in self.docs:
            if _matches(d, query) and self._apply(d, ops, inserting=False):
                n += 1
        return _Result(matched_count=n, modified_count=n)

    async def delete_one(self, query: dict):
        for i, d in enumerate(self.docs):
            if _matches(d, query):
                del self.docs[i]
                return _Result(deleted_count=1)
        return _Result(deleted_count=0)

    async def delete_many(self, query: dict):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _matches(d, query)]
        return _Result(deleted_count=before - len(self.docs))


class FakeRedis:
    """get / setex / set(nx, ex) / delete / exists on a dict."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete(self, *keys):
        n = 0
        for k in keys:
            if self.store.pop(k, None) is not None:
                n += 1
        return n

    async def exists(self, key):
        return int(key in self.store)
