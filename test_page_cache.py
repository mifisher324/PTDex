"""Page cache checks that need no database.

Run: python -m unittest test_page_cache -v
"""
import multiprocessing
import os
import shutil
import tempfile
import time
import unittest

from flask import Flask, flash, session

import page_cache


def make_app(cache_dir, ttl=600):
    app = Flask(__name__)
    app.secret_key = 'test'
    app.renders = 0
    page_cache.init_app(app, cache=page_cache.PageCache(cache_dir, ttl=ttl, max_bytes=10 ** 7))

    @app.route('/item/detail/<int:i>')
    def item(i):
        app.renders += 1
        who = session.get('duser', {}).get('name', 'guest')
        return 'item %d for %s render %d' % (i, who, app.renders)

    @app.route('/item/compare')
    def compare():
        app.renders += 1
        return 'compare render %d' % app.renders

    @app.route('/npc/detail/<int:i>')
    def npc(i):
        if i == 0:
            return 'not found', 404
        flash('note')                  # touches the session -> must not be stored
        return 'npc %d' % i

    @app.route('/login-as/<name>')
    def login(name):
        session['duser'] = {'name': name}
        return 'ok'

    @app.route('/item/search')
    def search():
        app.renders += 1
        return 'search %d' % app.renders
    return app


def _child_get(cache_dir, path, out):
    app = make_app(cache_dir)
    r = app.test_client().get(path)
    out.put((r.headers.get('X-Page-Cache'), r.get_data(as_text=True), app.renders))


class PageCacheTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='pc-test-')
        self.app = make_app(self.dir)
        self.c = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_miss_then_hit(self):
        r1 = self.c.get('/item/detail/5')
        r2 = self.c.get('/item/detail/5')
        self.assertEqual(r1.headers['X-Page-Cache'], 'MISS')
        self.assertEqual(r2.headers['X-Page-Cache'], 'HIT')
        self.assertEqual(r1.data, r2.data)
        self.assertEqual(self.app.renders, 1)
        self.assertIn('public', r2.headers['Cache-Control'])
        self.assertEqual(r2.headers['Vary'], 'Cookie')
        self.assertTrue(r2.headers['Content-Type'].startswith('text/html'))

    def test_query_is_part_of_key(self):
        self.c.get('/item/compare?a=1&b=2')
        r = self.c.get('/item/compare?a=1&b=3')
        self.assertEqual(r.headers['X-Page-Cache'], 'MISS')
        self.assertEqual(self.c.get('/item/compare?a=1&b=2').headers['X-Page-Cache'], 'HIT')

    def test_signed_in_bypasses_and_never_sees_or_writes_cache(self):
        self.c.get('/item/detail/7')                       # anonymous copy stored
        self.c.get('/login-as/Bob')                        # now has a session cookie
        r = self.c.get('/item/detail/7')
        self.assertEqual(r.headers['X-Page-Cache'], 'BYPASS')
        self.assertIn('for Bob', r.get_data(as_text=True))
        self.assertIn('no-store', r.headers['Cache-Control'])
        anon = make_app(self.dir).test_client().get('/item/detail/7')
        self.assertNotIn('Bob', anon.get_data(as_text=True))

    def test_non_200_and_session_writes_are_not_stored(self):
        self.assertIsNone(self.c.get('/npc/detail/0').headers.get('X-Page-Cache'))
        self.assertIsNone(self.c.get('/npc/detail/0').headers.get('X-Page-Cache'))
        r = make_app(self.dir).test_client().get('/npc/detail/3')
        self.assertIsNone(r.headers.get('X-Page-Cache'))
        self.assertEqual(page_cache.PageCache(self.dir)._entries(), [])

    def test_other_pages_untouched(self):
        self.c.get('/item/search')
        r = self.c.get('/item/search')
        self.assertIsNone(r.headers.get('X-Page-Cache'))
        self.assertEqual(self.app.renders, 2)

    def test_post_not_cached(self):
        self.assertEqual(self.c.post('/item/compare').status_code, 405)
        self.assertEqual(page_cache.PageCache(self.dir)._entries(), [])

    def test_ttl_expiry(self):
        app = make_app(self.dir, ttl=1)
        c = app.test_client()
        c.get('/item/detail/9')
        p = app.extensions['page_cache']._path('localhost/item/detail/9?')
        old = time.time() - 5
        os.utime(p, (old, old))
        self.assertEqual(c.get('/item/detail/9').headers['X-Page-Cache'], 'MISS')

    def test_off_switch_and_clear(self):
        os.environ['PAGE_CACHE_ENABLED'] = 'false'
        try:
            self.assertFalse(page_cache.PageCache.from_config(None).enabled)
        finally:
            del os.environ['PAGE_CACHE_ENABLED']
        self.assertTrue(page_cache.PageCache.from_config(None).enabled)
        self.assertEqual(page_cache.PageCache.from_config(None).ttl, 600)
        off = Flask(__name__)
        page_cache.init_app(off, cache=page_cache.PageCache(self.dir, enabled=False))
        off.add_url_rule('/item/detail/<int:i>', 'i', lambda i: 'x')
        self.assertIsNone(off.test_client().get('/item/detail/1').headers.get('X-Page-Cache'))
        self.c.get('/item/detail/1')
        self.c.get('/item/detail/2')
        self.assertEqual(self.app.extensions['page_cache'].clear(), 2)
        self.assertEqual(self.c.get('/item/detail/1').headers['X-Page-Cache'], 'MISS')

    def test_size_bound(self):
        pc = page_cache.PageCache(self.dir, max_bytes=5000)
        for i in range(50):
            pc.put('k%d' % i, {}, b'x' * 500)
        self.assertLessEqual(pc.prune(), 5000)
        self.assertIsNotNone(pc.get('k49'))            # newest kept
        self.assertIsNone(pc.get('k0'))                # oldest dropped

    def test_shared_across_processes(self):
        """A page rendered in one process is served from cache in another."""
        ctx = multiprocessing.get_context('spawn')
        q = ctx.Queue()
        p = ctx.Process(target=_child_get, args=(self.dir, '/item/detail/42', q))
        p.start()
        first = q.get(timeout=60)
        p.join(60)
        self.assertEqual(first[0], 'MISS')
        p = ctx.Process(target=_child_get, args=(self.dir, '/item/detail/42', q))
        p.start()
        second = q.get(timeout=60)
        p.join(60)
        self.assertEqual(second[0], 'HIT')
        self.assertEqual(second[1], first[1])
        self.assertEqual(second[2], 0)                  # second process never rendered


if __name__ == '__main__':
    unittest.main()
