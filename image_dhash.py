"""64-bit difference hash (dHash) for resize/re-encode tolerant matching.

Exact SHA-1 remains canonical identity. dHash finds near-duplicates when
bytes differ (compression, downscale) but the image is the same picture.

Storage: SQL ``BIGINT`` is signed — use :func:`to_sql_bigint` /
:func:`from_sql_bigint` at the DB boundary.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from PIL import Image

# Hamming ≤ this is treated as a near match by default (tune in UI).
DEFAULT_MAX_HAMMING = 5

# Typical dHash: resize to 9×8 grayscale, compare adjacent pixels → 64 bits.
_DHASH_W = 9
_DHASH_H = 8


def to_sql_bigint(dhash: int) -> int:
    """Map unsigned 64-bit dHash into signed SQL BIGINT range."""
    v = int(dhash) & ((1 << 64) - 1)
    if v >= (1 << 63):
        return v - (1 << 64)
    return v


def from_sql_bigint(value: int | None) -> int | None:
    """Map signed SQL BIGINT back to unsigned 64-bit dHash."""
    if value is None:
        return None
    v = int(value)
    if v < 0:
        return v + (1 << 64)
    return v


def hamming64(a: int, b: int) -> int:
    return ((int(a) ^ int(b)) & ((1 << 64) - 1)).bit_count()


def compute_dhash(path: Path | str) -> int | None:
    """Return 64-bit dHash for an image file, or None if unreadable."""
    try:
        with Image.open(path) as im:
            im.load()
            g = im.convert("L").resize(
                (_DHASH_W, _DHASH_H), Image.Resampling.LANCZOS
            )
            pixels = list(g.getdata())
    except OSError:
        return None
    except Exception:
        return None

    bits = 0
    for row in range(_DHASH_H):
        base = row * _DHASH_W
        for col in range(_DHASH_W - 1):
            bits = (bits << 1) | (1 if pixels[base + col] > pixels[base + col + 1] else 0)
    return bits


def order_sha_pair(a: bytes, b: bytes) -> tuple[bytes, bytes]:
    """Canonical order for undirected pair keys (a < b)."""
    if a <= b:
        return a, b
    return b, a


class BKTree:
    """Metric tree for Hamming search over (key, dhash) items.

    Fine for tens/hundreds of thousands of 64-bit hashes in memory.
    """

    __slots__ = ("_dist", "_root")

    def __init__(self):
        self._dist = hamming64
        self._root: _BKNode | None = None

    def clear(self) -> None:
        self._root = None

    def add(self, key: bytes, dhash: int) -> None:
        node = _BKNode(key, int(dhash))
        if self._root is None:
            self._root = node
            return
        cur = self._root
        while True:
            d = self._dist(cur.dhash, node.dhash)
            child = cur.children.get(d)
            if child is None:
                cur.children[d] = node
                return
            cur = child

    def find(
        self, dhash: int, *, radius: int, exclude: bytes | None = None
    ) -> list[tuple[bytes, int]]:
        """Return ``(key, hamming)`` within ``radius`` of ``dhash``."""
        out: list[tuple[bytes, int]] = []
        if self._root is None or radius < 0:
            return out
        stack = [self._root]
        target = int(dhash)
        while stack:
            node = stack.pop()
            d = self._dist(node.dhash, target)
            if d <= radius and (exclude is None or node.key != exclude):
                out.append((node.key, d))
            lo, hi = d - radius, d + radius
            for edge, child in node.children.items():
                if lo <= edge <= hi:
                    stack.append(child)
        return out

    def __len__(self) -> int:
        if self._root is None:
            return 0
        n = 0
        stack = [self._root]
        while stack:
            node = stack.pop()
            n += 1
            stack.extend(node.children.values())
        return n


class _BKNode:
    __slots__ = ("key", "dhash", "children")

    def __init__(self, key: bytes, dhash: int):
        self.key = key
        self.dhash = dhash
        self.children: dict[int, _BKNode] = {}


def find_near_pairs(
    items: Iterable[tuple[bytes, int]],
    *,
    max_hamming: int = DEFAULT_MAX_HAMMING,
    gallery_keys: dict[bytes, set[str]] | None = None,
) -> Iterator[tuple[bytes, bytes, int]]:
    """Yield ordered ``(sha_a, sha_b, hamming)`` cross-gallery near pairs.

    ``gallery_keys`` maps sha1 → set of gallery_key. Pairs that only co-occur
    inside a single shared gallery (no other-gallery peer) are skipped when
    provided. Exact same sha is never yielded.
    """
    rows = [(sha, int(dh)) for sha, dh in items if sha and dh is not None]
    if not rows or max_hamming < 0:
        return

    tree = BKTree()
    seen_pairs: set[tuple[bytes, bytes]] = set()

    for i, (sha, dh) in enumerate(rows):
        for other, dist in tree.find(dh, radius=max_hamming, exclude=sha):
            a, b = order_sha_pair(sha, other)
            if a == b:
                continue
            key = (a, b)
            if key in seen_pairs:
                continue
            if gallery_keys is not None:
                ga = gallery_keys.get(a) or set()
                gb = gallery_keys.get(b) or set()
                # Cross-gallery: some g_a ≠ g_b (skip sole co-residence).
                if not ga or not gb or (ga == gb and len(ga) == 1):
                    continue
            seen_pairs.add(key)
            yield a, b, dist
        tree.add(sha, dh)
