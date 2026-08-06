# EH Gallery Queue — TODO

## Open

### Filename → set matching rules
- Status: **deferred** (you will define rules later)
- Problem: thumb / source filenames often carry useful identity (Pixiv id,
  `DM_…` stamps, page tokens, etc.). The same bytes can appear under different
  names across galleries/sets.
- Direction: keep **meta names** (aliases) per SHA-1 without forcing a single
  on-disk name; later apply rules to soft-rename / appoint files into the
  proper set.
- Already in place for this work:
  - `dbo.image_fingerprints` — exact content id (`sha1 BINARY(20)`)
  - `dbo.image_name_aliases` — ordered name + bare thumb name + gallery_key
    per sighting (soft; canonical `sample_path` stays first copy / Duped home)
  - `dbo.sha1_match_groups` — user-confirmed multi-SHA identity (compare Done);
    members behave like one content id for Duped Exact + download skip
  - **Duped** tab — manual appoint home + move; optional peer symlinks
  - Auto-symlink on SHA skip so empty/partial folders stay visible
- When implementing automatic rules: read aliases + bare names, score against
  user rules, soft-rename or link into target set folders without losing
  alias history.
- Related: `db.py` (`record_name_alias`, `list_name_aliases`, `register_sha1`,
  `list_dupe_galleries`, `merge_sha1_match`), `fs_links.py`, download skip path
  in `downloader.py`, `tools/backfill_pics.py`

### Perceptual dHash for post-resize matching
- Status: **partial** (Duped Near mode)
- Column `image_fingerprints.dhash` filled by background worker + on register
- Cached `dhash_near_pairs` (BK-tree rebuild / incremental upsert)
- `dhash_false_positives` for dismissed pairs
- Duped: Exact SHA / Near dHash toggle; **compare session** (double-click row):
  Same / False positive / Prev–Next parallel walk / More picker / Done →
  `sha1_match_groups`; UI module: `duped_tab.py`
- No move/home for near matches yet (review-only)

### Queue filter (manual / auto / all)
- Status: **partial**
- Parse queue color-codes auto rows (blue) and keeps them at the end.
- Filter bar: name/url substring, `images:N` / `images:>=N`, right-click Copy URL.
- Optional later: explicit source filter (manual / auto / all) if mixed list stays noisy.

## Done (recent)
- Duped: remove match board; compare session with SHA match groups (Done)
- Queue: reorder selected rows (↑ ↓ Top Bottom); persists `position`; rebuilds
  waiting jobs if Start is already running
- Duped tab: shared SHA galleries, mark home, move ± peer symlinks; DB aliases
  remain identity even without links; EH `f_shash` once per SHA-1 (pairs skip);
  Undecided-only drops fully decided homes; a *new* peer gallery alias clears
  `home_decided` so those SHAs reappear for review; Strip peers removes peer
  symlinks that point at the home file (nofollow path guard)
- Auto-symlink into gallery folder when download skips on exact SHA match
- Duped: Exact/Near toggle; false positives; large compare session (double-click);
  UI lives in `duped_tab.py` (`DupedTab`)
- Local Import tab: scan pics folders, EH title search (quoted `f_search` +
  fallbacks), register into `galleries` + fingerprints, or enqueue to verify;
  UI in `import_tab.py`
- EH download engine in `downloader.py` (`EHDownloader`)
- EH title search helper (`eh_title_search`) — full folder names fail unquoted;
  phrase quotes hit
- EH `f_shash` slow check queue (`eh_sha_checks` / `eh_sha_match_galleries`)
  + background worker + auto-enqueue (`source=auto`, end of queue)
- Ordered prefixes (`01_` / `001_` / …) from gallery total
- SHA-1 fingerprint skip across galleries
- Backfill rename + fingerprints under `a:\trt\.Pics`
