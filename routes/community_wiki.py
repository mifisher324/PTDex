"""Public community forums for the NMS server.

- Anyone may VIEW pages (public).
- Any logged-in Discord user may CREATE a page.
- Only the ORIGINAL AUTHOR or a MODERATOR may EDIT or DELETE one. It used to be
  that any signed-in user could edit anybody's post.
- Pages are markdown files under ``community-wiki/``. Because the content is
  publicly editable, rendered markdown is sanitized with bleach (whitelist of
  safe tags/attributes/protocols) to prevent stored XSS.

OWNERSHIP
    ``_index.json`` records ``author_id`` (the Discord id) at creation and never
    overwrites it; ``editor`` remains the LAST person to save, which is what the
    page footer shows. Ownership is checked on the id, not the display name --
    names are changeable and would let someone rename their way into a post.

    LEGACY PAGES have no ``author_id``, because nothing recorded one before this
    change. They are treated as UNOWNED and only a moderator may edit or delete
    them; see `can_edit`. Falling back to matching the stored editor NAME was
    considered and rejected for the reason above.

MODERATORS
    Reuses the existing ``[leaderboard] staff_discord_ids`` allowlist rather than
    inventing a second list to keep in sync.

This is separate from the legacy staff-only wiki (routes/wiki.py, /wiki).
"""
import os
import re
import json
import time

import markdown
import bleach
from flask import (Blueprint, render_template, request, redirect, url_for,
                   abort, flash, current_app)

bp = Blueprint('community_wiki', __name__, template_folder='templates')

WIKI_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'community-wiki'))
os.makedirs(WIKI_DIR, exist_ok=True)
INDEX_PATH = os.path.join(WIKI_DIR, '_index.json')

# --- First-run seed --------------------------------------------------------- #
# Forum posts live as files under community-wiki/. That directory is USER DATA
# (like a database): it must be EXCLUDED from the deploy package and preserved
# across deploys. If it's shipped/overwritten, every deploy resets the live forum
# and all posts vanish. We seed a starter home page here ONLY when the directory
# has no pages yet (a fresh install); existing content is never touched.
_SEED_HOME = """# Welcome to the Forums

Written and maintained by the **Project Triune** community.

## How to contribute
1. **Sign in with Discord** — click the profile circle in the **top-right** of any page.
2. Hit **Edit** on any page, or **New Page** from the Forums home, to add your own guide or post.
3. Write in **Markdown** — headings, lists, links, tables, and code blocks are all supported. Raw HTML is sanitized for safety.

## Good pages to start
- Leveling & progression tips
- Best-in-slot gear by slot
- Trio synergies and build ideas
- Zone and quest walkthroughs

*Play three classes. Forge one hero.*
"""


def _seed_if_empty():
    """Write the starter home page only when the forum has no pages yet -- so a
    fresh install has content, but existing posts are never clobbered."""
    try:
        if any(f.endswith('.md') for f in os.listdir(WIKI_DIR)):
            return
    except OSError:
        return
    with open(os.path.join(WIKI_DIR, 'home.md'), 'w', encoding='utf-8') as f:
        f.write(_SEED_HOME)
    if not os.path.exists(INDEX_PATH):
        with open(INDEX_PATH, 'w', encoding='utf-8') as f:
            json.dump({'home': {'title': 'Welcome to the Forums', 'section': 'General',
                                'subsection': '', 'pinned': True, 'editor': 'NMS',
                                'updated': 1784937600}}, f, indent=2)


_seed_if_empty()

SLUG_RE = re.compile(r'[^a-z0-9-]+')
ALLOWED_TAGS = ['p', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li',
                'strong', 'em', 'b', 'i', 'u', 'del', 'code', 'pre', 'blockquote',
                'a', 'img', 'hr', 'span', 'table', 'thead', 'tbody', 'tr', 'th', 'td']
ALLOWED_ATTRS = {'a': ['href', 'title'], 'img': ['src', 'alt', 'title'],
                 'th': ['align'], 'td': ['align'], 'span': []}
ALLOWED_PROTOCOLS = ['http', 'https', 'mailto']


# ---- auth (any logged-in Discord user; open in Development) ----
def _current_user():
    """Read the identity cached in the session at login, the same one the header
    and dashboard use -- so signing in once anywhere on the site (e.g. the profile
    circle on the main page) already logs you into the forums; no second sign-in."""
    try:
        from flask import session
        du = session.get('duser')
        if not du:
            from eqdb import _session_user   # backfill older sessions (fetch + cache once)
            du = _session_user()
        if du:
            return {'id': du['id'], 'name': du['name']}
    except Exception:
        pass
    if current_app.config.get('SITE_TYPE') == 'Development':
        return {'id': 'dev', 'name': 'dev'}
    return None


# ---- moderation / ownership ------------------------------------------------ #
# The seeded index page is the forum's front door and is re-created on boot when
# missing, so deleting it achieves nothing except confusion.
PROTECTED_SLUGS = {'home'}


def _moderator_ids():
    """Discord ids allowed to edit or remove anyone's post.

    These are NUMERIC Discord user ids (e.g. 100000000000000001), not usernames.
    Discord OAuth hands the site the id; a username is changeable, so matching on
    one would let somebody rename their way into moderator powers.

    Reads FORUM_MODERATOR_IDS, which falls back to the leaderboard staff list
    when unconfigured -- see the note in eqdb.py for why the two are separate.
    """
    raw = current_app.config.get('FORUM_MODERATOR_IDS')
    if raw is None:
        raw = current_app.config.get('LEADERBOARD_STAFF_IDS', '')
    if isinstance(raw, (set, list, tuple)):
        return {str(x) for x in raw}
    return {s.strip() for s in str(raw or '').split(',') if s.strip()}


def is_moderator(user):
    return bool(user) and str(user.get('id')) in _moderator_ids()


def can_edit(user, meta):
    """(allowed, reason). `meta` is the index entry for the page, or {} for a
    page that does not exist yet (anyone signed in may create)."""
    if not user:
        return False, 'You must sign in with Discord to do that.'
    if is_moderator(user):
        return True, None
    if not meta:                       # brand new page -- creation is open
        return True, None
    author = meta.get('author_id')
    if not author:
        # Predates author tracking. Unowned, so moderators only -- see module
        # docstring for why the editor NAME is not accepted as proof.
        return False, ('This post was written before the forum recorded authors, '
                       'so only a moderator can change it.')
    if str(author) == str(user.get('id')):
        return True, None
    return False, 'Only the author of this post or a moderator can change it.'


def can_delete(user, slug, meta):
    if slug in PROTECTED_SLUGS:
        return False, 'The forum home page cannot be deleted.'
    return can_edit(user, meta)


def _slugify(text):
    s = SLUG_RE.sub('-', (text or '').strip().lower()).strip('-')
    return re.sub(r'-{2,}', '-', s)


def _page_path(slug):
    # Hardened against traversal: slug is already [a-z0-9-], but double-check.
    if not slug or '/' in slug or '\\' in slug or '.' in slug:
        return None
    p = os.path.abspath(os.path.join(WIKI_DIR, slug + '.md'))
    if os.path.dirname(p) != WIKI_DIR:
        return None
    return p


def _load_index():
    try:
        with open(INDEX_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_index(idx):
    with open(INDEX_PATH, 'w', encoding='utf-8') as f:
        json.dump(idx, f, indent=2)


def _title_from(content, slug):
    for line in (content or '').splitlines():
        m = re.match(r'#\s+(.+)', line.strip())
        if m:
            return m.group(1).strip()
    return slug.replace('-', ' ').title()


def _render(content):
    raw = markdown.markdown(content or '', extensions=['tables', 'fenced_code', 'toc'])
    return bleach.clean(raw, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS,
                        protocols=ALLOWED_PROTOCOLS, strip=True)


def _fmt(ts):
    """A post/reply timestamp as a US Pacific date. The container's clock is UTC, so
    time.localtime() dated an evening (Pacific) post on the next day."""
    try:
        if not ts:
            return ''
        from datetime import datetime
        from leaderboard_read import to_pacific
        return to_pacific(datetime.utcfromtimestamp(float(ts))).strftime('%Y-%m-%d')
    except Exception:
        return ''


def _all_sections(idx):
    return sorted({(m.get('section') or 'General') for m in idx.values()} | {'General'})


@bp.route('/community')
def community_home():
    idx = _load_index()
    pages = []
    for fn in os.listdir(WIKI_DIR):
        if not fn.endswith('.md'):
            continue
        slug = fn[:-3]
        meta = idx.get(slug, {})
        title = meta.get('title')
        if not title:
            try:
                with open(os.path.join(WIKI_DIR, fn), encoding='utf-8') as f:
                    title = _title_from(f.read(), slug)
            except Exception:
                title = slug.replace('-', ' ').title()
        replies = [c for c in _load_comments(slug) if not c.get('removed')]
        last_reply = max((c.get('ts', 0) for c in replies), default=0)
        last = max(int(meta.get('updated') or 0), last_reply)
        pages.append({'slug': slug, 'title': title,
                      'section': meta.get('section') or 'General',
                      'subsection': meta.get('subsection') or '',
                      'pinned': bool(meta.get('pinned')),
                      'editor': meta.get('editor', ''), 'updated_str': _fmt(meta.get('updated')),
                      'replies': len(replies), 'last': last, 'last_str': _fmt(last),
                      'last_who': (max(replies, key=lambda c: c.get('ts', 0)).get('author', '')
                                   if last_reply and last_reply >= int(meta.get('updated') or 0)
                                   else meta.get('editor', ''))})
    # group: section (category) -> subsection -> [pages]. 'General' is always present.
    tree = {'General': {}}
    for p in pages:
        tree.setdefault(p['section'], {}).setdefault(p['subsection'], []).append(p)
    grouped = []
    # custom sections alphabetical, the catch-all 'General' last
    for sec in sorted(tree, key=lambda s: (s == 'General', s.lower())):
        subs = []
        # pages with no subsection first, then subsections alphabetical
        for sub in sorted(tree[sec], key=lambda s: (s != '', s.lower())):
            # pinned pages float to the top of their subsection
            plist = sorted(tree[sec][sub], key=lambda p: (not p['pinned'], p['title'].lower()))
            subs.append({'name': sub, 'pages': plist})
        grouped.append({'name': sec, 'subs': subs,
                        'count': sum(len(s['pages']) for s in subs),
                        'replies': sum(p['replies'] for s in subs for p in s['pages'])})
    # sidebar: what moved most recently, and what people are talking about
    recent = sorted(pages, key=lambda p: p['last'], reverse=True)[:8]
    discussed = [p for p in sorted(pages, key=lambda p: (p['replies'], p['last']), reverse=True) if p['replies']][:6]
    return render_template('community_index.html', sections=grouped, recent=recent,
                           discussed=discussed, total_pages=len(pages),
                           total_replies=sum(p['replies'] for p in pages), user=_current_user())


@bp.route('/community/preview', methods=['POST'])
def community_preview():
    """Render markdown -> sanitized HTML for the live editor preview. Uses the
    exact same bleach pipeline as the saved page, so preview == final output."""
    if not _current_user():
        abort(403)
    return _render(request.form.get('content', ''))


@bp.route('/community/new', methods=['GET', 'POST'])
def community_new():
    user = _current_user()
    if not user:
        return redirect(url_for('community_wiki.community_home'))
    if request.method == 'POST':
        slug = _slugify(request.form.get('title', ''))
        if not slug:
            flash('Please enter a valid page title.')
            return redirect(url_for('community_wiki.community_new'))
        if _page_path(slug) and os.path.exists(_page_path(slug)):
            return redirect(url_for('community_wiki.community_edit', slug=slug))
        return redirect(url_for('community_wiki.community_edit', slug=slug,
                                title=request.form.get('title', ''),
                                section=request.form.get('section', ''),
                                subsection=request.form.get('subsection', '')))
    return render_template('community_edit.html', slug='', title='', content='',
                           section=request.args.get('section', ''), subsection='',
                           all_sections=_all_sections(_load_index()), user=user, is_new=True)


# ---- comments --------------------------------------------------------------- #
# Each post's replies live in community-wiki/_comments/<slug>.json (user data, kept across
# deploys with the posts). Any signed-in Discord user may reply; the reply's author or a
# moderator may remove it. A removed reply keeps its place as "[removed]" so the thread still
# reads in order. Writes take an exclusive file lock: gunicorn runs several workers.
COMMENTS_DIR = os.path.join(WIKI_DIR, '_comments')
COMMENT_MAX = 4000            # characters
COMMENT_GAP = 15              # seconds between one user's replies (flood guard)


def _comments_path(slug):
    if not _page_path(slug):
        return None
    return os.path.join(COMMENTS_DIR, slug + '.json')


def _load_comments(slug):
    path = _comments_path(slug)
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


class _CommentLock:
    def __init__(self, slug):
        os.makedirs(COMMENTS_DIR, exist_ok=True)
        self.f = open(os.path.join(COMMENTS_DIR, '.lock'), 'w')

    def __enter__(self):
        try:
            import fcntl
            fcntl.flock(self.f, fcntl.LOCK_EX)
        except ImportError:          # Windows dev: single process
            pass
        return self

    def __exit__(self, *a):
        try:
            import fcntl
            fcntl.flock(self.f, fcntl.LOCK_UN)
        except ImportError:
            pass
        self.f.close()


def _save_comments(slug, comments):
    path = _comments_path(slug)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(comments, f, indent=1)
    os.replace(tmp, path)


def can_remove_comment(user, comment):
    if not user:
        return False
    return is_moderator(user) or str(comment.get('author_id')) == str(user.get('id'))


@bp.route('/community/<slug>/comment', methods=['POST'])
def community_comment(slug):
    user = _current_user()
    slug = _slugify(slug)
    path = _page_path(slug)
    if not path or not os.path.exists(path):
        abort(404)
    if not user:
        flash('Sign in with Discord to reply.')
        return redirect(url_for('community_wiki.community_view', slug=slug))
    body = (request.form.get('body') or '').strip()
    if not body:
        return redirect(url_for('community_wiki.community_view', slug=slug) + '#replies')
    if len(body) > COMMENT_MAX:
        flash('Replies are limited to %d characters.' % COMMENT_MAX)
        return redirect(url_for('community_wiki.community_view', slug=slug) + '#replies')
    now = int(time.time())
    with _CommentLock(slug):
        comments = _load_comments(slug)
        last = max((c.get('ts', 0) for c in comments if str(c.get('author_id')) == str(user['id'])), default=0)
        if now - last < COMMENT_GAP:
            flash('Please wait a few seconds before replying again.')
            return redirect(url_for('community_wiki.community_view', slug=slug) + '#replies')
        cid = max((c.get('id', 0) for c in comments), default=0) + 1
        comments.append({'id': cid, 'author_id': str(user['id']), 'author': user['name'],
                         'body': body, 'ts': now})
        _save_comments(slug, comments)
    return redirect(url_for('community_wiki.community_view', slug=slug) + '#c%d' % cid)


@bp.route('/community/<slug>/comment/<int:cid>/delete', methods=['POST'])
def community_comment_delete(slug, cid):
    user = _current_user()
    slug = _slugify(slug)
    if not _page_path(slug):
        abort(404)
    with _CommentLock(slug):
        comments = _load_comments(slug)
        for c in comments:
            if c.get('id') == cid and not c.get('removed'):
                if not can_remove_comment(user, c):
                    flash('Only the author of that reply or a moderator can remove it.')
                    break
                c['removed'] = True
                c['removed_by'] = str(user['id'])
                c['body'] = ''
                _save_comments(slug, comments)
                current_app.logger.info('forum reply %s/%d removed by %s (%s)', slug, cid,
                                        user.get('name'), user.get('id'))
                break
    return redirect(url_for('community_wiki.community_view', slug=slug) + '#replies')


@bp.route('/community/<slug>')
def community_view(slug):
    slug = _slugify(slug)
    path = _page_path(slug)
    user = _current_user()
    if not path or not os.path.exists(path):
        # Missing page: offer creation to logged-in users, else 404.
        if user:
            return render_template('community_page.html', slug=slug,
                                   title=slug.replace('-', ' ').title(),
                                   body=None, user=user, meta=None, missing=True)
        abort(404)
    with open(path, encoding='utf-8') as f:
        content = f.read()
    stored = _load_index().get(slug, {})
    meta = dict(stored)
    if meta.get('updated'):
        meta['updated_str'] = _fmt(meta['updated'])
    # Decide permissions HERE, once, so the template only shows controls the
    # server would actually honour -- the routes re-check regardless.
    may_edit, edit_why = can_edit(user, stored)
    may_delete, _ = can_delete(user, slug, stored)
    comments = []
    for c in _load_comments(slug):
        comments.append({'id': c.get('id'), 'author': c.get('author', ''), 'when': _fmt(c.get('ts')),
                         'removed': bool(c.get('removed')),
                         'html': '' if c.get('removed') else _render(c.get('body', '')),
                         'can_remove': not c.get('removed') and can_remove_comment(user, c)})
    return render_template('community_page.html', slug=slug,
                           title=meta.get('title') or _title_from(content, slug),
                           body=_render(content), user=user, meta=meta, missing=False,
                           can_edit=may_edit, can_delete=may_delete,
                           edit_why=edit_why, is_mod=is_moderator(user),
                           comments=comments, comment_max=COMMENT_MAX)


@bp.route('/community/<slug>/edit', methods=['GET', 'POST'])
def community_edit(slug):
    user = _current_user()
    if not user:
        flash('You must sign in with Discord to edit the wiki.')
        return redirect(url_for('community_wiki.community_view', slug=slug))
    slug = _slugify(slug)
    path = _page_path(slug)
    if not path:
        abort(404)

    idx = _load_index()
    exists = os.path.exists(path)
    # Authorise against the STORED metadata, never anything the form supplied.
    # An unsaved page has no meta, so creating is open to any signed-in user.
    meta = idx.get(slug, {}) if exists else {}
    allowed, why = can_edit(user, meta)
    if not allowed:
        flash(why)
        return redirect(url_for('community_wiki.community_view', slug=slug))

    if request.method == 'POST':
        content = request.form.get('content', '')
        prev = idx.get(slug, {})
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        entry = {'title': _title_from(content, slug),
                 'section': (request.form.get('section') or '').strip() or 'General',
                 'subsection': (request.form.get('subsection') or '').strip(),
                 'pinned': prev.get('pinned', False),   # preserve pin state
                 'updated': int(time.time()), 'editor': user['name']}
        # Authorship is set ONCE, by whoever first saves the page, and carried
        # forward untouched on every later edit -- a moderator fixing a typo must
        # not become the owner of someone else's post.
        entry['author_id'] = prev.get('author_id') or str(user['id'])
        entry['author'] = prev.get('author') or user['name']
        entry['created'] = prev.get('created') or int(time.time())
        idx[slug] = entry
        _save_index(idx)
        flash('Page saved.')
        return redirect(url_for('community_wiki.community_view', slug=slug))

    content = ''
    if exists:
        with open(path, encoding='utf-8') as f:
            content = f.read()
    elif request.args.get('title'):
        content = '# %s\n\n' % request.args.get('title')
    return render_template('community_edit.html', slug=slug,
                           title=_title_from(content, slug), content=content,
                           section=meta.get('section') or request.args.get('section', ''),
                           subsection=meta.get('subsection') or request.args.get('subsection', ''),
                           all_sections=_all_sections(idx), user=user, is_new=not exists)


@bp.route('/community/<slug>/delete', methods=['POST'])
def community_delete(slug):
    """Remove a post. Author or moderator only, POST only.

    The file is MOVED to community-wiki/_trash/ rather than unlinked: these are
    user-written posts, an accidental delete is unrecoverable otherwise, and the
    directory is already excluded from the deploy package so nothing is shipped.
    _trash holds no '.md' at the top level, so listings never pick it up.
    """
    user = _current_user()
    slug = _slugify(slug)
    path = _page_path(slug)
    if not path or not os.path.exists(path):
        abort(404)

    idx = _load_index()
    allowed, why = can_delete(user, slug, idx.get(slug, {}))
    if not allowed:
        flash(why)
        return redirect(url_for('community_wiki.community_view', slug=slug))

    trash = os.path.join(WIKI_DIR, '_trash')
    os.makedirs(trash, exist_ok=True)
    try:
        os.replace(path, os.path.join(trash, '%s-%d.md' % (slug, int(time.time()))))
    except OSError:
        current_app.logger.exception('forum delete failed for %s', slug)
        flash('That post could not be deleted. Nothing was changed.')
        return redirect(url_for('community_wiki.community_view', slug=slug))

    if slug in idx:
        del idx[slug]
        _save_index(idx)
    current_app.logger.info('forum post %r deleted by %s (%s)',
                            slug, user.get('name'), user.get('id'))
    flash('Post deleted.')
    return redirect(url_for('community_wiki.community_home'))
