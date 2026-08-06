"""Filename set / page pattern helpers for Duped compare navigation.

Ordered gallery names look like ``{synth}_{rest}`` where ``synth`` is a
zero-padded gallery index. The *rest* often encodes a set id + page token:

- ``1546__490P_1.jpg``     → family ``490P``, page ``1``
- ``0462_DM_20250918114808_001.jpg`` → family ``DM_20250918114808``, page ``001``
- ``23_132837196_22.jpg``  → family ``132837196``, page ``22``
- ``01_126324413_p0.jpg``  → family ``126324413``, page ``0`` (Pixiv)
- ``0142_1_3.jpg``         → page-batch ``1`` / ``3`` (mix dump; weak alone)

Compare-session Prev/Next should stay inside one family on each side when
both anchors have a detectable pattern; otherwise walk the full sequences.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

# Leading gallery order prefix: 01_ / 001_ / 1546_ / …
_SYNTH_RE = re.compile(r"^(\d+)_(.+)$", re.DOTALL)
# Pixiv multipage: illustId_pN
_PIXIV_PAGE_RE = re.compile(r"^(.*)_p(\d+)$", re.IGNORECASE)
# Trailing page / frame id after the set token.
_PAGE_RE = re.compile(r"^(.*)_(\d+)$", re.DOTALL)
# Mix dump: {synth}_{page}_{batch} with a short batch token.
_PAGE_BATCH_RE = re.compile(r"^(\d+)_(\d+)$")


def strip_synth_prefix(filename: str) -> str:
    """Remove leading ``NNN_`` synth index (any digit width)."""
    name = Path(filename or "").name
    m = _SYNTH_RE.match(name)
    if not m:
        return name
    return m.group(2) or name


def _split_synth(stem: str) -> tuple[str | None, str]:
    m = _SYNTH_RE.match(stem)
    if not m:
        return None, stem
    return m.group(1), (m.group(2) or "").lstrip("_")


def parse_name_pattern(filename: str) -> dict | None:
    """Extract ``{family, page, stem, synth, kind}`` when a set+page pattern exists.

    Returns None when the name is too plain for a useful set key (no page
    token after the synth prefix, or empty family).
    """
    raw = Path(filename or "").name
    if not raw:
        return None
    stem = Path(raw).stem
    synth, rest = _split_synth(stem)
    if not rest:
        return None

    # Pixiv illustId_pN (before generic trailing _digits).
    pm = _PIXIV_PAGE_RE.match(rest)
    if pm:
        family = (pm.group(1) or "").strip("_")
        page = pm.group(2)
        if family and not (family.isdigit() and len(family) < 5):
            return {
                "synth": synth,
                "family": family,
                "page": page,
                "stem": stem,
                "raw": raw,
                "kind": "pixiv",
            }

    # Mix-style page_batch: ``1_3`` after synth (weak family; still useful).
    bm = _PAGE_BATCH_RE.match(rest)
    if bm:
        page, batch = bm.group(1), bm.group(2)
        # Batch is a short tag (usually 1–2 digits); page may be larger.
        if len(batch) <= 2:
            return {
                "synth": synth,
                "family": f"batch:{batch}",
                "page": page,
                "batch": batch,
                "stem": stem,
                "raw": raw,
                "kind": "page_batch",
            }

    pm = _PAGE_RE.match(rest)
    if not pm:
        # No trailing _digits page — treat whole rest as family only if it
        # still looks like a multi-token set id (contains letters or long id).
        family = rest.strip("_")
        if not family or family.isdigit():
            return None
        return {
            "synth": synth,
            "family": family,
            "page": None,
            "stem": stem,
            "raw": raw,
            "kind": "family_only",
        }
    family = (pm.group(1) or "").strip("_")
    page = pm.group(2)
    if not family:
        return None
    # Lone numeric family with a page is weak (e.g. ``01_02``); require some
    # non-digit or a reasonably long id so gallery-order leftovers don't bind.
    if family.isdigit() and len(family) < 5:
        return None
    return {
        "synth": synth,
        "family": family,
        "page": page,
        "stem": stem,
        "raw": raw,
        "kind": "set_page",
    }


def family_key(filename: str) -> str | None:
    """Case-folded set id for grouping, or None if no pattern."""
    p = parse_name_pattern(filename)
    if not p or not p.get("family"):
        return None
    # page_batch is too weak for compare-session slicing alone.
    if p.get("kind") == "page_batch":
        return None
    return str(p["family"]).casefold()


def filter_seq_by_family(seq: list[dict], family: str | None) -> list[dict]:
    """Keep slots whose name shares ``family`` (case-insensitive)."""
    if not family:
        return list(seq)
    want = family.casefold()
    out: list[dict] = []
    for slot in seq:
        name = slot.get("name") or ""
        fk = family_key(name)
        if fk and fk == want:
            out.append(slot)
    return out


def gallery_set_score(names: list[str]) -> dict:
    """Score how "dedicated set"-like a name list is.

    Higher ``score`` → prefer as home. Pixiv / strong single-family runs beat
    mix dumps (many families or page_batch names).
    """
    families: Counter[str] = Counter()
    pixiv_families: Counter[str] = Counter()
    page_batch_n = 0
    patterned = 0
    for name in names:
        p = parse_name_pattern(name)
        if not p:
            continue
        patterned += 1
        kind = p.get("kind")
        fam = str(p.get("family") or "")
        if kind == "page_batch":
            page_batch_n += 1
            continue
        if not fam:
            continue
        families[fam.casefold()] += 1
        if kind == "pixiv":
            pixiv_families[fam.casefold()] += 1

    top_fam, top_n = ("", 0)
    if families:
        top_fam, top_n = families.most_common(1)[0]
    pixiv_top_n = pixiv_families.most_common(1)[0][1] if pixiv_families else 0
    n = max(len(names), 1)
    # Dedicated: one strong family covers most files; mix: page_batch / many fams.
    coverage = top_n / n
    diversity_penalty = max(0, len(families) - 1) * 0.15
    batch_penalty = (page_batch_n / n) * 0.8
    score = (
        pixiv_top_n * 2.0
        + top_n * 1.0
        + coverage * 3.0
        - diversity_penalty
        - batch_penalty
    )
    return {
        "score": score,
        "top_family": top_fam or None,
        "top_count": top_n,
        "family_n": len(families),
        "pixiv_top_count": pixiv_top_n,
        "page_batch_n": page_batch_n,
        "patterned": patterned,
        "total": len(names),
    }


def prefer_dedicated_side(
    key_a: str,
    names_a: list[str],
    key_b: str,
    names_b: list[str],
) -> tuple[str, str, dict, dict]:
    """Return ``(set_key, mix_key, score_a, score_b)`` preferring dedicated set."""
    sa = gallery_set_score(names_a)
    sb = gallery_set_score(names_b)
    if sa["score"] >= sb["score"]:
        return key_a, key_b, sa, sb
    return key_b, key_a, sb, sa
