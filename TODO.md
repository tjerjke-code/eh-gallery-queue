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
    per sighting (soft; canonical `sample_path` stays first copy)
- When implementing: read aliases + bare names, score against user rules,
  soft-rename or link into target set folders without losing alias history.
- Related: `db.py` (`record_name_alias`, `list_name_aliases`, `register_sha1`),
  download skip path in `app.py`, `tools/backfill_pics.py`

### Perceptual dHash for post-resize matching
- Status: **deferred**
- Column `image_fingerprints.dhash` exists (nullable BIGINT). Fill when the
  resize pipeline lands; match equal / small Hamming distance.

### Queue filter (manual / auto / all)
- Status: **open**
- Parse queue already color-codes auto rows (blue) and keeps them at the end.
- Optional: filter control if the mixed list gets noisy.

## Done (recent)
- Local Import tab: scan pics folders, EH title search (quoted `f_search` +
  fallbacks), register into `galleries` + fingerprints, or enqueue to verify
- EH title search helper (`eh_title_search`) — full folder names fail unquoted;
  phrase quotes hit
- EH `f_shash` slow check queue (`eh_sha_checks` / `eh_sha_match_galleries`)
  + background worker + auto-enqueue (`source=auto`, end of queue)
- Ordered prefixes (`01_` / `001_` / …) from gallery total
- SHA-1 fingerprint skip across galleries
- Backfill rename + fingerprints under `a:\trt\.Pics`
