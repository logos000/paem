"""Exact and near-duplicate detection (sha256 + 64-bit simhash with banding)."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import re
from typing import Callable, Iterable

from evidence.store import match_key


def shingles(text: str, size: int = 4) -> list[str]:
    key = match_key(text)
    if len(key) <= size:
        return [key] if key else []
    return [key[i:i + size] for i in range(len(key) - size + 1)]


def simhash(text: str, bits: int = 64) -> int:
    import numpy as np

    grams = shingles(text)
    if not grams:
        return 0
    counts: dict[str, int] = defaultdict(int)
    for gram in grams:
        counts[gram] += 1
    digests = b"".join(hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest() for gram in counts)
    # Big-endian bytes unpacked MSB-first; reverse so column i is bit i.
    matrix = np.unpackbits(np.frombuffer(digests, dtype=np.uint8).reshape(len(counts), 8), axis=1)[:, ::-1]
    weights = np.asarray(list(counts.values()), dtype=np.int64) @ (matrix.astype(np.int64) * 2 - 1)
    return sum(1 << bit for bit in range(bits) if weights[bit] > 0)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def duplicate_groups(
    docs: Iterable[dict],
    *,
    text_of: Callable[[dict], str],
    signature_of: Callable[[dict], str] | None = None,
    max_distance: int = 3,
    min_chars: int = 80,
) -> list[dict]:
    """Group documents with identical or near-identical normalized text.

    Returns ``[{"members": [doc_uid...], "kind": "exact"|"near", "max_distance": n}]``.
    Very short texts are only grouped on exact equality: two 20-character
    notices differing by a date are different facts, not duplicates.  For the
    same reason a near-duplicate pair must also share ``signature_of`` (by
    default every number in the text): the 2021 and 2022 editions of an annual
    notice are versions, not duplicates.
    """
    signature_of = signature_of or (lambda doc: " ".join(re.findall(r"\d+", text_of(doc))))
    docs = list(docs)
    uf = UnionFind()
    exact: dict[str, list[str]] = defaultdict(list)
    digest_of: dict[str, str] = {}
    hashes: dict[str, int] = {}
    lengths: dict[str, int] = {}
    signatures: dict[str, str] = {}
    for doc in docs:
        uid = doc["doc_uid"]
        text = text_of(doc)
        key = match_key(text)
        lengths[uid] = len(key)
        if not key:
            continue
        digest_of[uid] = hashlib.sha256(key.encode("utf-8")).hexdigest()
        exact[digest_of[uid]].append(uid)
        if len(key) >= min_chars:
            hashes[uid] = simhash(text)
            signatures[uid] = signature_of(doc)
    for members in exact.values():
        for other in members[1:]:
            uf.union(members[0], other)
    # 4 bands of 16 bits: any pair within distance 3 shares at least one band.
    buckets: dict[tuple[int, int], list[str]] = defaultdict(list)
    for uid, value in hashes.items():
        for band in range(4):
            buckets[(band, value >> (16 * band) & 0xFFFF)].append(uid)
    unions: list[tuple[str, int]] = []
    for members in buckets.values():
        if len(members) < 2 or len(members) > 200:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                ratio = min(lengths[a], lengths[b]) / max(1, max(lengths[a], lengths[b]))
                if ratio < 0.8 or signatures[a] != signatures[b]:
                    continue
                distance = hamming(hashes[a], hashes[b])
                if distance <= max_distance and uf.find(a) != uf.find(b):
                    uf.union(a, b)
                    unions.append((a, distance))
    groups: dict[str, list[str]] = defaultdict(list)
    for doc in docs:
        if lengths.get(doc["doc_uid"]):
            groups[uf.find(doc["doc_uid"])].append(doc["doc_uid"])
    # Roots move as groups merge, so attribute distances after the last union.
    near_distance: dict[str, int] = defaultdict(int)
    for member, distance in unions:
        root = uf.find(member)
        near_distance[root] = max(near_distance[root], distance)
    result = []
    for root, members in groups.items():
        if len(members) < 2:
            continue
        # A simhash distance of 0 does not mean identical text.
        kind = "exact" if len({digest_of[m] for m in members}) == 1 else "near"
        result.append({"members": members, "kind": kind, "max_distance": near_distance.get(root, 0)})
    return result
