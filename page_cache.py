"""Shared page cache for the read-only data pages.

Item, NPC, spell, zone, tradeskill and compare pages show the same thing to
every signed-out visitor, so the finished HTML is kept for a few minutes and
handed straight back without touching the database. A crawler walking
thousands of item pages then costs one render per page per TTL instead of one
per hit.

Rules:
  * GET/HEAD only, on the paths in CACHED_PREFIXES / CACHED_EXACT.
  * Only for visitors with NO session cookie. The session is where the Discord
    login, flash messages and the password-step state live, so anyone with one
    gets a fresh render (sent as `private, no-store`) every time.
  * Only 200 responses that set no cookie are stored.
  * Key = host + full path + query string (the site answers on several
    domains, and the `ingame` query changes the layout).
  * Stored as files in one directory, so all gunicorn workers share it.
  * Bounded by max_mb: expired files go first, then the oldest.

Settings ([page_cache] in configuration.ini; the environment wins):
  enabled     / PAGE_CACHE_ENABLED      true|false            (default true)
  ttl         / PAGE_CACHE_TTL          seconds               (default 600)
  max_mb      / PAGE_CACHE_MAX_MB       size bound in MB      (default 200)
  dir         / PAGE_CACHE_DIR          cache directory       (default <tmp>/eqdb-page-cache)
  browser_ttl / PAGE_CACHE_BROWSER_TTL  max-age for proxies and browsers (default 60)

Clear it with `python page_cache.py clear` (or delete the directory). Workers
see that on their next request; no restart needed.
"""
import hashlib
import json
import os
import random
import sys
import tempfile
import time

CACHED_PREFIXES = ('/item/detail/', '/npc/detail/', '/spell/detail/', '/item/raw/',
                   '/spell/raw/', '/zone/', '/tradeskill/detail/')
CACHED_EXACT = ('/item/compare',)

_SUFFIX = '.page'
_PRUNE_CHANCE = 0.02          # about one write in fifty checks the size bound
_TMP_MAX_AGE = 60             # a temp file older than this is a leftover from a crash


def _truthy(v):
    return str(v).strip().lower() not in ('0', 'false', 'no', 'off', '')


class PageCache:
    def __init__(self, directory=None, ttl=600, max_bytes=200 * 1024 * 1024,
                 enabled=True, browser_ttl=60):
        self.dir = directory or os.path.join(tempfile.gettempdir(), 'eqdb-page-cache')
        self.ttl = int(ttl)
        self.max_bytes = int(max_bytes)
        self.enabled = bool(enabled) and self.ttl > 0
        self.browser_ttl = max(0, min(int(browser_ttl), self.ttl))

    @classmethod
    def from_config(cls, site_config=None, section='page_cache'):
        def get(key, env, default):
            v = os.environ.get(env)
            if v not in (None, ''):
                return v
            if site_config is not None:
                try:
                    return site_config.get(section, key, fallback=default)
                except Exception:
                    return default
            return default
        return cls(directory=get('dir', 'PAGE_CACHE_DIR', '') or None,
                   ttl=int(get('ttl', 'PAGE_CACHE_TTL', 600)),
                   max_bytes=int(float(get('max_mb', 'PAGE_CACHE_MAX_MB', 200)) * 1024 * 1024),
                   enabled=_truthy(get('enabled', 'PAGE_CACHE_ENABLED', 'true')),
                   browser_ttl=int(get('browser_ttl', 'PAGE_CACHE_BROWSER_TTL', 60)))

    # -- storage -------------------------------------------------------------
    def _path(self, key):
        h = hashlib.sha256(key.encode('utf-8', 'surrogateescape')).hexdigest()
        return os.path.join(self.dir, h[:2], h + _SUFFIX)

    def get(self, key):
        """(headers dict, body bytes) for a fresh entry, else None."""
        p = self._path(key)
        try:
            if time.time() - os.path.getmtime(p) > self.ttl:
                return None
            with open(p, 'rb') as f:
                head = f.readline()
                body = f.read()
            meta = json.loads(head)
            if meta.get('key') != key:          # hash collision guard
                return None
            return meta.get('headers') or {}, body
        except (OSError, ValueError):
            return None

    def put(self, key, headers, body):
        p = self._path(key)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix='.tmp')
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(json.dumps({'key': key, 'headers': headers}).encode() + b'\n')
                    f.write(body)
                os.replace(tmp, p)              # atomic: readers never see half a file
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            return False
        if random.random() < _PRUNE_CHANCE:
            self.prune()
        return True

    def _entries(self):
        out = []
        try:
            subs = os.listdir(self.dir)
        except OSError:
            return out
        for sub in subs:
            d = os.path.join(self.dir, sub)
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for n in names:
                p = os.path.join(d, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append((st.st_mtime, st.st_size, p, n.endswith('.tmp')))
        return out

    def prune(self):
        """Drop expired entries and crash leftovers, then the oldest until under max_bytes.
        Returns the bytes left."""
        now = time.time()
        keep, total = [], 0
        for mtime, size, p, is_tmp in self._entries():
            if is_tmp:
                if now - mtime > _TMP_MAX_AGE:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                continue
            if now - mtime > self.ttl:
                try:
                    os.remove(p)
                except OSError:
                    pass
                continue
            keep.append((mtime, size, p))
            total += size
        if total > self.max_bytes:
            keep.sort()
            for mtime, size, p in keep:
                if total <= self.max_bytes * 0.9:
                    break
                try:
                    os.remove(p)
                    total -= size
                except OSError:
                    pass
        return total

    def clear(self):
        n = 0
        for _m, _s, p, _t in self._entries():
            try:
                os.remove(p)
                n += 1
            except OSError:
                pass
        return n


def cacheable_path(path):
    return path in CACHED_EXACT or path.startswith(CACHED_PREFIXES)


def init_app(app, site_config=None, cache=None):
    """Hook the cache into a Flask app. Call it before other before_request hooks
    are registered so a hit returns before any of them run."""
    from flask import g, request, session

    cache = cache or PageCache.from_config(site_config)
    app.extensions['page_cache'] = cache

    def _key():
        host = (request.headers.get('X-Forwarded-Host') or request.host or '').split(',')[0].strip().lower()
        return host + request.full_path

    @app.before_request
    def _page_cache_lookup():
        if not cache.enabled or request.method not in ('GET', 'HEAD') \
                or not cacheable_path(request.path):
            return None
        if request.cookies.get(app.config.get('SESSION_COOKIE_NAME', 'session')):
            g._page_cache = 'bypass'
            return None
        key = _key()
        hit = cache.get(key)
        if hit is not None:
            headers, body = hit
            resp = app.response_class(body, status=200)
            for k, v in headers.items():
                resp.headers[k] = v
            resp.headers['Cache-Control'] = 'public, max-age=%d' % cache.browser_ttl
            resp.headers['Vary'] = 'Cookie'
            resp.headers['X-Page-Cache'] = 'HIT'
            g._page_cache = 'hit'
            return resp
        g._page_cache = key
        return None

    @app.after_request
    def _page_cache_store(resp):
        state = g.pop('_page_cache', None)
        if state is None or state == 'hit':
            return resp
        resp.headers['Vary'] = 'Cookie'
        if state == 'bypass':
            resp.headers['Cache-Control'] = 'private, no-store'
            resp.headers['X-Page-Cache'] = 'BYPASS'
            return resp
        ok = (resp.status_code == 200 and not resp.direct_passthrough
              and not resp.is_streamed and 'Set-Cookie' not in resp.headers
              and not getattr(session, 'modified', False))
        if not ok:
            resp.headers['Cache-Control'] = 'private, no-store'
            return resp
        body = resp.get_data()
        headers = {'Content-Type': resp.headers.get('Content-Type', 'text/html; charset=utf-8')}
        cache.put(state, headers, body)
        resp.headers['Cache-Control'] = 'public, max-age=%d' % cache.browser_ttl
        resp.headers['X-Page-Cache'] = 'MISS'
        return resp

    return cache


if __name__ == '__main__':
    import configparser
    cfg = configparser.RawConfigParser()
    ini = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'configuration.ini')
    if os.path.exists(ini):
        cfg.read(ini)
    c = PageCache.from_config(cfg)
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if cmd == 'clear':
        print('removed %d cached pages from %s' % (c.clear(), c.dir))
    elif cmd == 'prune':
        print('%d bytes left in %s' % (c.prune(), c.dir))
    else:
        ents = c._entries()
        print('%s: %d files, %d bytes, enabled=%s, ttl=%ds' % (
            c.dir, len(ents), sum(e[1] for e in ents), c.enabled, c.ttl))
