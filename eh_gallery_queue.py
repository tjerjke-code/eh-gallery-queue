from bs4 import BeautifulSoup
import requests
import re
import threading
import queue
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import ceil
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

DEFAULT_DIR = r'a:\trt\.Pics'
HEADERS = {'User-Agent': 'Mozilla/5.0'}
# Global minimum gap between HTTP starts (all workers share this)
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


class EHDownloader:
    def __init__(self, out_dir: str, log, should_stop, workers=DEFAULT_WORKERS, interval=REQUEST_INTERVAL):
        self.out_dir = out_dir
        self.log = log
        self.should_stop = should_stop
        self.workers = max(1, int(workers))
        self.limiter = RateLimiter(interval)
        self._local = threading.local()
        self._stats_lock = threading.Lock()

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

    def _bump(self, stats, key, pic_name=None):
        with self._stats_lock:
            stats[key] += 1
            done = stats['saved'] + stats['skipped'] + stats['failed']
            total = stats['total']
        if key == 'saved' and pic_name:
            self.log(f'  saved {pic_name}  ({done}/{total})')
        elif key == 'failed' and pic_name:
            self.log(f'  SKIPPED {pic_name}')

    def download_image(self, pp_url, pic_name, target_dir, stats):
        path = Path(target_dir) / pic_name
        if path.is_file() and path.stat().st_size > 0:
            self._bump(stats, 'skipped')
            return

        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            self.check_stop()
            try:
                img_url = self.resolve_img(pp_url)
                data = self.get(img_url, binary=True)
                if not looks_like_image(data):
                    raise RuntimeError('invalid image data')
                part = path.with_suffix(path.suffix + '.part')
                part.write_bytes(data)
                part.replace(path)
                self._bump(stats, 'saved', pic_name)
                return
            except DownloadStopped:
                raise
            except Exception as e:
                last_err = e
                self.log(f'  {pic_name}: {e} — retry {attempt}/{MAX_RETRIES}')
                time.sleep(REQUEST_INTERVAL * attempt)
        self._bump(stats, 'failed', pic_name)
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
        url, soup = self.skip_warning(url.strip())
        title_raw = soup.find('h1', id='gn').text
        title = sanitize_name(title_raw)
        target_dir = Path(self.out_dir) / title
        target_dir.mkdir(parents=True, exist_ok=True)

        pages = self.page_count(soup)
        total = self.image_total(soup)
        stats = {'saved': 0, 'skipped': 0, 'failed': 0, 'total': total or '?'}
        self.log(f'— {title_raw}')
        self.log(f'  {pages} pages, ~{total} images, workers={self.workers} → {target_dir}')

        all_links = self.collect_links(url, pages)
        self.log(f'  collected {len(all_links)} image pages')

        # Fast skip pass (no network)
        todo = []
        for pp_url, pic_name in all_links:
            path = target_dir / pic_name
            if path.is_file() and path.stat().st_size > 0:
                self._bump(stats, 'skipped')
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
        return stats


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('EH Gallery Queue')
        self.geometry('720x540')
        self.minsize(560, 420)

        self.job_queue = queue.Queue()
        self._stop = threading.Event()
        self._worker = None
        self._queue_urls = []

        top = ttk.Frame(self, padding=8)
        top.pack(fill='x')

        ttk.Label(top, text='Save to:').pack(side='left')
        self.dir_var = tk.StringVar(value=DEFAULT_DIR)
        ttk.Entry(top, textvariable=self.dir_var).pack(side='left', fill='x', expand=True, padx=4)
        ttk.Button(top, text='Browse…', command=self.browse_dir).pack(side='left')

        opts = ttk.Frame(self, padding=(8, 0))
        opts.pack(fill='x')
        ttk.Label(opts, text='Workers:').pack(side='left')
        self.workers_var = tk.IntVar(value=DEFAULT_WORKERS)
        ttk.Spinbox(opts, from_=1, to=8, width=4, textvariable=self.workers_var).pack(side='left', padx=4)
        ttk.Label(opts, text='(3–4 safe · higher = faster, more ban risk)').pack(side='left')

        add_row = ttk.Frame(self, padding=(8, 4))
        add_row.pack(fill='x')
        ttk.Label(add_row, text='URL:').pack(side='left')
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(add_row, textvariable=self.url_var)
        self.url_entry.pack(side='left', fill='x', expand=True, padx=4)
        self.url_entry.bind('<Return>', lambda e: self.add_url())
        ttk.Button(add_row, text='Add to queue', command=self.add_url).pack(side='left')

        mid = ttk.Frame(self, padding=8)
        mid.pack(fill='both', expand=True)

        left = ttk.Frame(mid)
        left.pack(side='left', fill='both', expand=True)
        ttk.Label(left, text='Parse queue').pack(anchor='w')
        self.listbox = tk.Listbox(left, height=10, selectmode='extended')
        self.listbox.pack(fill='both', expand=True)
        btns = ttk.Frame(left)
        btns.pack(fill='x', pady=4)
        ttk.Button(btns, text='Remove', command=self.remove_selected).pack(side='left')
        ttk.Button(btns, text='Clear', command=self.clear_queue).pack(side='left', padx=4)

        right = ttk.Frame(mid)
        right.pack(side='left', fill='y', padx=(8, 0))
        self.start_btn = ttk.Button(right, text='Start', command=self.start)
        self.start_btn.pack(fill='x', pady=2)
        self.stop_btn = ttk.Button(right, text='Stop', command=self.stop, state='disabled')
        self.stop_btn.pack(fill='x', pady=2)

        ttk.Label(self, text='Log', padding=(8, 0)).pack(anchor='w')
        self.log_box = scrolledtext.ScrolledText(self, height=14, state='disabled', wrap='word')
        self.log_box.pack(fill='both', expand=True, padx=8, pady=(0, 8))

        self.status = tk.StringVar(value='Idle')
        ttk.Label(self, textvariable=self.status, padding=8).pack(fill='x')

        self.protocol('WM_DELETE_WINDOW', self.on_close)

    def browse_dir(self):
        path = filedialog.askdirectory(initialdir=self.dir_var.get() or None)
        if path:
            self.dir_var.set(path)

    def add_url(self):
        url = self.url_var.get().strip()
        if not url:
            return
        if '/g/' not in url:
            messagebox.showwarning('Invalid URL', 'Paste an e-hentai gallery URL (/g/...).')
            return
        if url in self._queue_urls:
            messagebox.showinfo('Queue', 'URL already in queue.')
            return
        self._queue_urls.append(url)
        self.listbox.insert('end', url)
        if self._worker and self._worker.is_alive():
            self.job_queue.put(url)
            self.set_status(f'Running — {self.job_queue.qsize()} waiting (+ current)')
        self.url_var.set('')
        self.url_entry.focus_set()

    def remove_selected(self):
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)
            del self._queue_urls[i]

    def clear_queue(self):
        if self._worker and self._worker.is_alive():
            messagebox.showinfo('Busy', 'Stop the worker before clearing.')
            return
        self.listbox.delete(0, 'end')
        self._queue_urls.clear()

    def ui_log(self, msg):
        def _append():
            self.log_box.configure(state='normal')
            self.log_box.insert('end', msg + '\n')
            self.log_box.see('end')
            self.log_box.configure(state='disabled')
        self.after(0, _append)

    def set_status(self, text):
        self.after(0, lambda: self.status.set(text))

    def start(self):
        if self._worker and self._worker.is_alive():
            return
        if not self._queue_urls:
            messagebox.showinfo('Queue', 'Add at least one gallery URL.')
            return
        out = self.dir_var.get().strip()
        if not out:
            messagebox.showwarning('Save to', 'Choose an output folder.')
            return
        Path(out).mkdir(parents=True, exist_ok=True)

        try:
            workers = int(self.workers_var.get())
        except (tk.TclError, ValueError):
            workers = DEFAULT_WORKERS
        workers = max(1, min(8, workers))

        self._stop.clear()
        while not self.job_queue.empty():
            try:
                self.job_queue.get_nowait()
            except queue.Empty:
                break
        for u in self._queue_urls:
            self.job_queue.put(u)

        self.start_btn.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        self.set_status(f'Running — {self.job_queue.qsize()} in queue, {workers} workers')
        self._worker = threading.Thread(
            target=self.worker, args=(out, workers), daemon=True
        )
        self._worker.start()

    def stop(self):
        self._stop.set()
        self.set_status('Stopping…')
        self.ui_log('Stop requested — cancelling workers…')

    def worker(self, out_dir, workers):
        dl = EHDownloader(
            out_dir, self.ui_log, self._stop.is_set, workers=workers
        )
        done = 0
        try:
            while not self._stop.is_set():
                try:
                    url = self.job_queue.get(timeout=0.5)
                except queue.Empty:
                    break
                self.after(0, self._pop_queue_item, url)
                self.set_status(
                    f'Working… ({self.job_queue.qsize()} left, {workers} workers)'
                )
                self.ui_log(f'\nQueue item: {url}')
                try:
                    dl.parse_gallery(url)
                    done += 1
                except DownloadStopped:
                    self.ui_log('Stopped by user.')
                    break
                except Exception as e:
                    self.ui_log(f'Gallery error: {e}')
        finally:
            self.after(0, self._worker_done, done)

    def _pop_queue_item(self, url):
        try:
            i = self._queue_urls.index(url)
            del self._queue_urls[i]
            self.listbox.delete(i)
        except ValueError:
            pass

    def _worker_done(self, done):
        self.start_btn.configure(state='normal')
        self.stop_btn.configure(state='disabled')
        if self._stop.is_set():
            self.set_status(
                f'Stopped — finished {done} gallery(ies), {len(self._queue_urls)} left'
            )
        else:
            self.set_status(f'Idle — finished {done} gallery(ies)')
        self.ui_log(f'\n=== batch finished ({done}) ===')

    def on_close(self):
        if self._worker and self._worker.is_alive():
            if not messagebox.askokcancel('Quit', 'Downloader is running. Stop and quit?'):
                return
            self._stop.set()
        self.destroy()


if __name__ == '__main__':
    App().mainloop()
