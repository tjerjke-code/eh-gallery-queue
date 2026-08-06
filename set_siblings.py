"""Suggest hole-fill set siblings from SHA-matched Duped clusters.

When a dedicated set gallery shares many SHA peers with a mix dump, the mix
often still holds pages the set never had (no SHA twin). Detect those via
synth/page holes in the matched name cluster and suggest pulling them home
with a set-style rename — user confirms in the Duped file list.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from name_pattern import (
    parse_name_pattern,
    prefer_dedicated_side,
)

# Need enough SHA pairs to trust a cluster signature.
_MIN_CLUSTER = 3


def _alias_name(item: dict, gallery_key: str) -> str | None:
    for a in item.get("aliases") or []:
        if (a.get("gallery_key") or "") == gallery_key:
            n = a.get("name") or a.get("bare_name")
            if n:
                return str(n)
    return None


def _peer_keys(item: dict, focus_key: str) -> set[str]:
    out: set[str] = set()
    for a in item.get("aliases") or []:
        gk = (a.get("gallery_key") or "").strip()
        if gk and gk != focus_key:
            out.add(gk)
    return out


def _ext_of(name: str) -> str:
    return Path(name or "").suffix or ".jpg"


def _synth_width(synths: list[int], names: list[str]) -> int:
    widths = []
    for n in names:
        p = parse_name_pattern(n)
        if p and p.get("synth"):
            widths.append(len(str(p["synth"])))
    if widths:
        return max(widths)
    if synths:
        return max(len(str(s)) for s in synths)
    return 2


def _format_synth(n: int, width: int) -> str:
    return f"{n:0{width}d}"


def suggest_set_siblings(
    shared_files: list[dict],
    focus_key: str,
    *,
    list_aliases_for_gallery,
    gallery_has_sha,
    resolve_path,
) -> list[dict]:
    """Return orphan rows that likely belong to a dedicated set home.

    ``list_aliases_for_gallery(gallery_key) -> list[dict]`` with name/sha1/…
    ``gallery_has_sha(gallery_key, digest) -> bool``
    ``resolve_path(sample_path, gallery_key, name) -> Path | None``
    """
    focus_key = (focus_key or "").strip()
    if not focus_key or not shared_files:
        return []

    by_peer: dict[str, list[dict]] = defaultdict(list)
    for item in shared_files:
        for pk in _peer_keys(item, focus_key):
            by_peer[pk].append(item)

    out: list[dict] = []
    seen_sha: set[bytes] = set()

    for peer_key, items in by_peer.items():
        if len(items) < _MIN_CLUSTER:
            continue
        focus_names = []
        peer_names = []
        for it in items:
            fn = _alias_name(it, focus_key)
            pn = _alias_name(it, peer_key)
            if fn:
                focus_names.append(fn)
            if pn:
                peer_names.append(pn)
        if len(focus_names) < _MIN_CLUSTER or len(peer_names) < _MIN_CLUSTER:
            continue

        set_key, mix_key, set_score, _mix_score = prefer_dedicated_side(
            focus_key, focus_names, peer_key, peer_names
        )
        # Only act when one side looks clearly more set-like.
        if set_score.get("score", 0) < 2.0:
            continue
        family = set_score.get("top_family")
        family_display = family
        if not family:
            fam_c: Counter[str] = Counter()
            fam_disp: dict[str, str] = {}
            for it in items:
                n = _alias_name(it, set_key)
                p = parse_name_pattern(n or "")
                if p and p.get("kind") == "pixiv" and p.get("family"):
                    raw_f = str(p["family"])
                    key_f = raw_f.casefold()
                    fam_c[key_f] += 1
                    fam_disp.setdefault(key_f, raw_f)
            if not fam_c:
                continue
            family = fam_c.most_common(1)[0][0]
            family_display = fam_disp.get(family, family)
        else:
            # Restore display casing from a set-side name when possible.
            for it in items:
                n = _alias_name(it, set_key)
                p = parse_name_pattern(n or "")
                if (
                    p
                    and p.get("family")
                    and str(p["family"]).casefold() == family
                ):
                    family_display = str(p["family"])
                    break

        mix_parsed = []
        for it in items:
            n = _alias_name(it, mix_key)
            p = parse_name_pattern(n or "")
            if p and p.get("kind") == "page_batch" and p.get("batch") is not None:
                mix_parsed.append(p)
        if len(mix_parsed) < _MIN_CLUSTER:
            continue

        batch_c = Counter(str(p["batch"]) for p in mix_parsed)
        batch, batch_n = batch_c.most_common(1)[0]
        if batch_n < len(mix_parsed) * 0.6:
            continue

        pages: set[int] = set()
        synths: set[int] = set()
        for p in mix_parsed:
            if str(p.get("batch")) != batch:
                continue
            try:
                pages.add(int(p["page"]))
            except (TypeError, ValueError):
                pass
            if p.get("synth") is not None:
                try:
                    synths.add(int(p["synth"]))
                except (TypeError, ValueError):
                    pass
        if not pages or not synths:
            continue

        set_names = [_alias_name(it, set_key) for it in items]
        set_names = [n for n in set_names if n]
        set_synths: list[int] = []
        set_pages: set[int] = set()
        for n in set_names:
            p = parse_name_pattern(n)
            if not p:
                continue
            if p.get("synth") is not None:
                try:
                    set_synths.append(int(p["synth"]))
                except (TypeError, ValueError):
                    pass
            if p.get("page") is not None and (
                p.get("kind") == "pixiv"
                or (p.get("family") or "").casefold() == family
            ):
                try:
                    set_pages.add(int(p["page"]))
                except (TypeError, ValueError):
                    pass

        matched_shas = {it["sha1"] for it in items if it.get("sha1")}
        min_s, max_s = min(synths), max(synths)
        min_p, max_p = min(pages), max(pages)
        synth_holes = set(range(min_s, max_s + 1)) - synths
        # Pages missing from the matched mix span, plus pages the set never had.
        page_holes = set(range(min_p, max_p + 1)) - pages
        if set_pages:
            page_holes |= pages - set_pages
            span = set_pages | pages
            page_holes |= set(range(min(span), max(span) + 1)) - set_pages

        try:
            mix_aliases = list_aliases_for_gallery(mix_key) or []
        except Exception:
            mix_aliases = []

        width = _synth_width(set_synths, set_names)
        next_synth = (max(set_synths) + 1) if set_synths else (max_s + 1)
        # Assign suggested synths in page order for stable names.
        pending: list[tuple[int, dict, Path]] = []

        for row in mix_aliases:
            digest = row.get("sha1")
            if not digest or digest in matched_shas or digest in seen_sha:
                continue
            name = row.get("name") or row.get("bare_name") or ""
            p = parse_name_pattern(name)
            if not p or p.get("kind") != "page_batch":
                continue
            if str(p.get("batch")) != batch:
                continue
            try:
                page_i = int(p["page"])
            except (TypeError, ValueError):
                continue
            synth_i = None
            if p.get("synth") is not None:
                try:
                    synth_i = int(p["synth"])
                except (TypeError, ValueError):
                    synth_i = None

            hit_in_band = synth_i is not None and min_s <= synth_i <= max_s
            hit_synth = synth_i is not None and synth_i in synth_holes
            hit_page = page_i in page_holes or (
                min_p <= page_i <= max_p and page_i not in pages
            )
            # Stay inside the matched mix synth span (avoids other *_1_3 dumps).
            if not hit_in_band:
                continue
            if not hit_synth and not hit_page:
                continue
            if gallery_has_sha(set_key, digest):
                continue

            path = resolve_path(
                row.get("sample_path"), mix_key, name
            )
            if path is None:
                continue
            pending.append((page_i, row, path))

        pending.sort(key=lambda t: (t[0], t[1].get("name") or ""))
        for page_i, row, path in pending:
            digest = row["sha1"]
            if digest in seen_sha:
                continue
            seen_sha.add(digest)
            name = row.get("name") or row.get("bare_name") or path.name
            suggested = (
                f"{_format_synth(next_synth, width)}_{family_display}_p{page_i}"
                f"{_ext_of(name)}"
            )
            next_synth += 1
            reason_bits = []
            p = parse_name_pattern(name)
            if p and p.get("synth") is not None:
                try:
                    if int(p["synth"]) in synth_holes:
                        reason_bits.append("synth hole")
                except (TypeError, ValueError):
                    pass
            if page_i in page_holes or page_i not in pages:
                reason_bits.append(f"page {page_i}")
            reason = ", ".join(reason_bits) or "cluster gap"

            # Display names relative to focus gallery.
            if focus_key == set_key:
                local_name = suggested
                peer_name = name
                peer_g = mix_key
                this_path = ""  # not on set yet
                peer_path = str(path)
            else:
                local_name = name
                peer_name = suggested
                peer_g = set_key
                this_path = str(path)
                peer_path = ""

            out.append(
                {
                    "sha1": digest,
                    "sha1_hex": digest.hex()
                    if isinstance(digest, bytes)
                    else str(digest),
                    "sample_path": str(path),
                    "home_gallery_key": set_key,
                    "byte_len": None,
                    "seen_count": 0,
                    "home_decided": False,
                    "peer_sha1": None,
                    "aliases": [
                        {
                            "name": name,
                            "bare_name": Path(name).stem,
                            "gallery_key": mix_key,
                            "sample_path": str(path),
                            "sha1": digest,
                        }
                    ],
                    "kind": "set_sibling",
                    "preferred_home_key": set_key,
                    "suggested_home_name": suggested,
                    "source_gallery_key": mix_key,
                    "source_name": name,
                    "set_family": family_display,
                    "set_page": page_i,
                    "sibling_reason": reason,
                    "_local_name": local_name,
                    "_peer_key": peer_g,
                    "_peer_name": peer_name,
                    "_this_path": this_path,
                    "_peer_path": peer_path,
                }
            )
    return out
