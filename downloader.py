"""EH gallery page parse + image download (hot-reloadable)."""

from __future__ import annotations

import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import ceil
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from db import (
    QueueStore,
    gallery_key_from_url,
    index_pad_width,
    normalize_image_links,
    strip_order_prefix,
)
from fs_links import ensure_symlink, resolve_real_file, same_path
from image_dhash import compute_dhash

HEADERS = {'User-Agent': 'Mozilla/5.0'}
REQUEST_INTERVAL = 0.35
MAX_RETRIES = 5
DEFAULT_WORKERS = 4
WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class DownloadStopped(Exception):
    pass


class RateLimiter:
    """Serialize request *starts* so N workers don't stampede the site."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            time.sleep(delay)


def sanitize_name(name: str) -> str:
    name = WIN_BAD.sub('_', name).strip(' .')
    return name[:180] or 'gallery'


def looks_like_ban(text: str) -> bool:
    t = text.lower()
    return any(s in t for s in (
        'temporarily banned',
        'ban expires',
        'exceeded your image',
        '509 bandwidth',
        'owner has enabled',
    ))


def looks_like_image(data: bytes) -> bool:
    if not data or len(data) < 16:
        return False
    if data[:1] == b'<' or data[:15].lstrip().lower().startswith(b'<!doctype') or data[:5].lower() == b'<html':
        return False
    return (
        data[:3] == b'\xff\xd8\xff'
        or data[:8] == b'\x89PNG\r\n\x1a\n'
        or data[:6] in (b'GIF87a', b'GIF89a')
        or data[:4] == b'RIFF'
    )


def _sha1_bytes(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()


def _sha1_file(path: Path, chunk: int = 1024 * 1024) -> bytes:
    h = hashlib.sha1()
    with path.open('rb') as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.digest()


def find_existing_image(
    target_dir: Path, ordered_name: str, *, total: int
) -> Path | None:
    """Find an on-disk file for this slot (ordered name or legacy bare thumb name)."""
    target_dir = Path(target_dir)
    direct = target_dir / ordered_name
    if direct.is_file() and direct.stat().st_size > 0:
        return direct
    w = index_pad_width(total)
    bare = strip_order_prefix(ordered_name, w)
    if bare and bare != ordered_name:
        legacy = target_dir / bare
        if legacy.is_file() and legacy.stat().st_size > 0:
            return legacy
    return None


def adopt_to_ordered_name(existing: Path, ordered_name: str) -> Path:
    """Rename legacy bare file to ordered name when needed."""
    dest = existing.parent / ordered_name
    if existing.resolve() == dest.resolve():
        return dest
    if dest.exists():
        return dest
    existing.rename(dest)
    return dest


class EHDownloader:
    def __init__(
        self,
        out_dir: str,
        log,
        should_stop,
        workers=DEFAULT_WORKERS,
        interval=REQUEST_INTERVAL,
        store: QueueStore | None = None,
        gallery_url: str | None = None,
        on_meta=None,
    ):
        self.out_dir = out_dir
        self.log = log
        self.should_stop = should_stop
        self.workers = max(1, int(workers))
        self.limiter = RateLimiter(interval)
        self._local = threading.local()
        self._stats_lock = threading.Lock()
        self.store = store
        self.gallery_url = gallery_url
        self.on_meta = on_meta
        self._name_total = 1
        self._dhash_worker = None  # App may attach DhashFillWorker for wake()

    def _session(self):
        s = getattr(self._local, 'session', None)
        if s is None:
            s = requests.Session()
            s.headers.update(HEADERS)
            self._local.session = s
        return s

    def check_stop(self):
        if self.should_stop():
            raise DownloadStopped()

    def get(self, url, binary=False, retries=MAX_RETRIES):
        last_err = None
        for attempt in range(1, retries + 1):
            self.check_stop()
            try:
                self.limiter.wait()
                self.check_stop()
                r = self._session().get(url, timeout=30)
                r.raise_for_status()
                body = r.content if binary else r.text
                if not binary and isinstance(body, str) and looks_like_ban(body):
                    raise RuntimeError('rate-limit / ban page')
                if binary and not looks_like_image(body):
                    text = body[:500].decode('utf-8', 'ignore')
                    if looks_like_ban(text) or '<html' in text.lower():
                        raise RuntimeError('non-image / ban response')
                return body
            except DownloadStopped:
                raise
            except Exception as e:
                last_err = e
                self.log(f'  request fail ({attempt}/{retries}): {e}')
                time.sleep(REQUEST_INTERVAL * attempt)
        raise RuntimeError(f'Failed after {retries} tries: {last_err}')

    def skip_warning(self, url):
        source = self.get(url)
        soup = BeautifulSoup(source, 'lxml')
        h1 = soup.find('h1')
        if h1 and 'Content Warning' in h1.text:
            url = soup.find('p', style='text-align:center').find('a')['href']
            source = self.get(url)
            soup = BeautifulSoup(source, 'lxml')
        return url, soup

    def page_count(self, soup):
        ptt = soup.find('table', class_='ptt')
        if ptt:
            nums = [int(td.get_text(strip=True)) for td in ptt.find_all('td')
                    if td.get_text(strip=True).isdigit()]
            if nums:
                return max(nums)
        gpc = soup.find('p', class_='gpc')
        if gpc:
            parts = gpc.get_text(strip=True).replace(',', '').split()
            try:
                total = int(parts[parts.index('of') + 1])
                per_page = int(parts[parts.index('-') + 1])
                return max(1, ceil(total / per_page))
            except (ValueError, IndexError):
                pass
        return 1

    def image_total(self, soup):
        gpc = soup.find('p', class_='gpc')
        if gpc:
            parts = gpc.get_text(strip=True).replace(',', '').split()
            try:
                return int(parts[parts.index('of') + 1])
            except (ValueError, IndexError):
                pass
        return 0

    def thumb_links(self, soup):
        box = soup.find('div', id='gdt') or soup.find('div', class_='gt200')
        if not box:
            return []
        links = []
        for a in box.find_all('a', href=True):
            title_el = a.find(attrs={'title': True})
            if not title_el:
                continue
            name = title_el['title'].split(': ')[-1]
            links.append((a['href'], name))
        return links

    def resolve_img(self, pp_url):
        source = self.get(pp_url)
        soup = BeautifulSoup(source, 'lxml')
        img = soup.find('img', id='img')
        if img is not None:
            return img['src']

        loadfail = soup.find(id='loadfail')
        if loadfail and loadfail.get('onclick'):
            m = re.search(r"nl\('([^']+)'\)", loadfail['onclick'])
            if m:
                sep = '&' if '?' in pp_url else '?'
                source = self.get(f'{pp_url}{sep}nl={m.group(1)}')
                soup = BeautifulSoup(source, 'lxml')
                img = soup.find('img', id='img')
                if img is not None:
                    return img['src']
        raise RuntimeError('no #img (rate-limit / ban / bad page)')

    def _fp_register(
        self,
        digest: bytes,
        byte_len: int,
        *,
        path: str | Path | None = None,
        ordered_name: str | None = None,
    ) -> None:
        if not self.store:
            return
        name = ordered_name or (Path(path).name if path else None)
        bare = None
        if name:
            bare = strip_order_prefix(name, index_pad_width(self._name_total))
        dhash = None
        if path:
            try:
                dhash = compute_dhash(path)
            except Exception:
                dhash = None
        try:
            self.store.register_sha1(
                digest,
                byte_len,
                sample_path=str(path) if path else None,
                gallery_key=gallery_key_from_url(self.gallery_url or ''),
                dhash=dhash,
                name=name,
                bare_name=bare,
            )
        except Exception as e:
            self.log(f'  fp register failed: {e}')
        # App owns the fill worker; EHDownloader must not assume the attribute.
        worker = getattr(self, '_dhash_worker', None)
        if dhash is not None and worker is not None:
            try:
                worker.wake()
            except Exception:
                pass

    def _bump(self, stats, key, pic_name=None, error=None):
        with self._stats_lock:
            stats[key] += 1
            done = stats['saved'] + stats['skipped'] + stats['failed']
            total = stats['total']
        if self.store and self.gallery_url and pic_name:
            try:
                self.store.update_image(
                    self.gallery_url,
                    pic_name,
                    key,
                    error=str(error) if error else None,
                    bump=key,
                )
            except Exception as e:
                self.log(f'  db image update failed: {e}')
        if key == 'saved' and pic_name:
            self.log(f'  saved {pic_name}  ({done}/{total})')
        elif key == 'failed' and pic_name:
            self.log(f'  SKIPPED {pic_name}')

    def download_image(self, pp_url, pic_name, target_dir, stats):
        path = Path(target_dir) / pic_name
        existing = find_existing_image(
            Path(target_dir), pic_name, total=self._name_total
        )
        if existing is not None:
            try:
                path = adopt_to_ordered_name(existing, pic_name)
            except OSError as e:
                self.log(f'  rename {existing.name} → {pic_name} failed: {e}')
                path = existing
            if self.store:
                try:
                    digest = _sha1_file(path)
                    self._fp_register(
                        digest,
                        path.stat().st_size,
                        path=path,
                        ordered_name=pic_name,
                    )
                except Exception as e:
                    self.log(f'  fp backfill failed: {e}')
            self._bump(stats, 'skipped', pic_name)
            return

        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            self.check_stop()
            try:
                img_url = self.resolve_img(pp_url)
                data = self.get(img_url, binary=True)
                if not looks_like_image(data):
                    raise RuntimeError('invalid image data')
                digest = _sha1_bytes(data)
                if self.store:
                    try:
                        hit = self.store.lookup_sha1(digest)
                    except Exception as e:
                        hit = None
                        self.log(f'  fp lookup failed: {e}')
                    if hit:
                        prior = hit.get('sample_path') or 'known file'
                        aliases = []
                        try:
                            aliases = self.store.list_name_aliases(digest)
                        except Exception:
                            pass
                        alias_hint = ''
                        if aliases:
                            names = sorted({
                                a.get('bare_name') or a.get('name')
                                for a in aliases
                                if a.get('bare_name') or a.get('name')
                            })
                            if names:
                                alias_hint = f'; known as {names[:3]}'
                        self.log(
                            f'  skip {pic_name} — exact match in DB '
                            f'({prior}){alias_hint}'
                        )
                        self._fp_register(
                            digest,
                            len(data),
                            path=hit.get('sample_path'),
                            ordered_name=pic_name,
                        )
                        # Visibility: symlink into this gallery (DB is still
                        # source of truth; pair SHA never re-enqueues EH check).
                        real = resolve_real_file(hit.get('sample_path'))
                        if real is not None and not same_path(real, path):
                            link_status = ensure_symlink(path, real)
                            if link_status == 'ok':
                                self.log(f'  link {pic_name} → {real.name}')
                                if self.store:
                                    try:
                                        bare = strip_order_prefix(
                                            pic_name,
                                            index_pad_width(self._name_total),
                                        )
                                        self.store.record_name_alias(
                                            digest,
                                            name=pic_name,
                                            bare_name=bare,
                                            gallery_key=gallery_key_from_url(
                                                self.gallery_url or ''
                                            ),
                                            sample_path=str(path),
                                        )
                                    except Exception:
                                        pass
                            elif link_status == 'failed':
                                self.log(
                                    f'  symlink failed for {pic_name} '
                                    f'(enable Windows Developer Mode?)'
                                )
                        self._bump(stats, 'skipped', pic_name)
                        return
                part = path.with_suffix(path.suffix + '.part')
                part.write_bytes(data)
                part.replace(path)
                self._fp_register(
                    digest,
                    len(data),
                    path=path,
                    ordered_name=pic_name,
                )
                self._bump(stats, 'saved', pic_name)
                return
            except DownloadStopped:
                raise
            except Exception as e:
                last_err = e
                self.log(f'  {pic_name}: {e} — retry {attempt}/{MAX_RETRIES}')
                time.sleep(REQUEST_INTERVAL * attempt)
        self._bump(stats, 'failed', pic_name, error=last_err)
        self.log(f'  reason: {last_err}')

    def _fetch_page_links(self, gallery_url, page_index, pages):
        self.check_stop()
        source = self.get(f'{gallery_url}?p={page_index}')
        links = self.thumb_links(BeautifulSoup(source, 'lxml'))
        if not links:
            time.sleep(REQUEST_INTERVAL * 2)
            source = self.get(f'{gallery_url}?p={page_index}')
            links = self.thumb_links(BeautifulSoup(source, 'lxml'))
        if links:
            self.log(f'  page {page_index + 1}/{pages}: {len(links)} thumbs')
        else:
            self.log(f'  page {page_index + 1}/{pages}: empty — skip')
        return links

    def collect_links(self, gallery_url, pages):
        """Fetch gallery index pages in parallel, keep page order."""
        by_page = {}
        page_workers = min(self.workers, pages)
        with ThreadPoolExecutor(max_workers=page_workers) as pool:
            futs = {
                pool.submit(self._fetch_page_links, gallery_url, n, pages): n
                for n in range(pages)
            }
            try:
                for fut in as_completed(futs):
                    self.check_stop()
                    n = futs[fut]
                    try:
                        by_page[n] = fut.result()
                    except DownloadStopped:
                        for f in futs:
                            f.cancel()
                        raise
                    except Exception as e:
                        self.log(f'  page {n + 1}: {e}')
                        by_page[n] = []
            except DownloadStopped:
                pool.shutdown(wait=False, cancel_futures=True)
                raise

        links = []
        for n in range(pages):
            links.extend(by_page.get(n) or [])
        return links

    def parse_gallery(self, url):
        self.check_stop()
        self.gallery_url = url.strip()
        url, soup = self.skip_warning(self.gallery_url)
        title_raw = soup.find('h1', id='gn').text
        title = sanitize_name(title_raw)
        save_root = Path(self.out_dir)
        target_dir = save_root / title
        # Import enqueue stores the existing gallery folder on the queue row.
        # mark_running() may temporarily set out_dir to Save-to root — ignore that.
        if self.store:
            try:
                key = gallery_key_from_url(self.gallery_url)
                prior = None
                if key:
                    q = self.store.find_queue_by_key(key)
                    prior = (q or {}).get('out_dir')
                if prior:
                    p = Path(prior)
                    try:
                        is_save_root = p.resolve() == save_root.resolve()
                    except OSError:
                        is_save_root = (
                            str(p).rstrip('\\/').casefold()
                            == str(save_root).rstrip('\\/').casefold()
                        )
                    if p.is_dir() and not is_save_root:
                        target_dir = p
            except Exception:
                pass
        target_dir.mkdir(parents=True, exist_ok=True)

        pages = self.page_count(soup)
        total = self.image_total(soup)
        stats = {'saved': 0, 'skipped': 0, 'failed': 0, 'total': total or 0}
        self.log(f'— {title_raw}')
        self.log(f'  {pages} pages, ~{total} images, workers={self.workers} → {target_dir}')

        if self.store:
            try:
                self.store.set_gallery_meta(
                    self.gallery_url,
                    title=title_raw[:256],
                    out_dir=str(target_dir),
                    image_total=total or None,
                )
            except Exception as e:
                self.log(f'  db meta update failed: {e}')
        if self.on_meta:
            try:
                self.on_meta(title=title_raw, image_total=total or None)
            except Exception:
                pass

        all_links = normalize_image_links(
            self.collect_links(url, pages),
            total=total or None,
        )
        self._name_total = max(len(all_links), int(total or 0), 1)
        self.log(f'  collected {len(all_links)} image pages')
        stats['total'] = len(all_links) or stats['total']

        if self.store:
            try:
                self.store.replace_images(self.gallery_url, all_links)
            except Exception as e:
                self.log(f'  db image list failed: {e}')

        # Fast skip / adopt legacy names (no network)
        todo = []
        for pp_url, pic_name in all_links:
            existing = find_existing_image(
                target_dir, pic_name, total=self._name_total
            )
            if existing is not None:
                try:
                    adopted = adopt_to_ordered_name(existing, pic_name)
                except OSError as e:
                    self.log(f'  rename {existing.name} → {pic_name} failed: {e}')
                    adopted = existing
                if self.store:
                    try:
                        digest = _sha1_file(adopted)
                        bare = strip_order_prefix(
                            pic_name, index_pad_width(self._name_total)
                        )
                        self.store.register_sha1(
                            digest,
                            adopted.stat().st_size,
                            sample_path=str(adopted),
                            gallery_key=gallery_key_from_url(self.gallery_url),
                            name=pic_name,
                            bare_name=bare,
                        )
                    except Exception as e:
                        self.log(f'  fp backfill failed: {e}')
                self._bump(stats, 'skipped', pic_name)
            else:
                todo.append((pp_url, pic_name))

        if stats['skipped']:
            self.log(f'  already have {stats["skipped"]}, downloading {len(todo)}')

        if todo:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futs = [
                    pool.submit(self.download_image, pp, name, target_dir, stats)
                    for pp, name in todo
                ]
                try:
                    for fut in as_completed(futs):
                        self.check_stop()
                        try:
                            fut.result()
                        except DownloadStopped:
                            for f in futs:
                                f.cancel()
                            pool.shutdown(wait=False, cancel_futures=True)
                            raise
                        except Exception as e:
                            self.log(f'  worker error: {e}')
                except DownloadStopped:
                    raise

        self.log(
            f"  done: saved {stats['saved']}, skipped {stats['skipped']}, "
            f"failed {stats['failed']}"
        )
        stats['title'] = title_raw
        stats['target_dir'] = str(target_dir)
        return stats


