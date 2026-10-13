import configparser
import json
import logging
import os
import secrets
import time

import db
from datetime import timedelta

from flask import Flask, render_template, request, flash, redirect, url_for, jsonify, session
from flask_discord import DiscordOAuth2Session, requires_authorization, Unauthorized

import spell
import utils
import local
import logic
from routes import apis, pets, spells, zones, tradeskills, npcs, items, factions, characters, wiki, community_wiki
from routes import guilds as guild_routes
from routes import leaderboard as leaderboard_routes
from routes import build_simulator as build_simulator_routes
from routes import aas as aa_routes

# Application Setup
here = os.path.dirname(__file__)
site_config = configparser.RawConfigParser()
ini_path = os.path.join(here, 'configuration.ini')
site_config.read_file(open(ini_path))

SITE_TYPE = os.environ.get('SITE_TYPE', site_config.get('DEFAULT', 'site_type'))
SITE_VERSION = os.environ.get('SITE_VERSION', site_config.get('DEFAULT', 'site_version'))

app = Flask(__name__)
db.init_app(app)  # return each request's pooled connection when it ends
import page_cache
page_cache.init_app(app, site_config)  # shared cache for signed-out item/npc/spell/zone pages

# [site] settings: public, non-secret values the templates use (see configuration.ini.example).
def _site_bool(key, default='false'):
    return site_config.get('site', key, fallback=default).strip().lower() in ('1', 'true', 'yes', 'on')


SITE_SETTINGS = {
    'name': site_config.get('site', 'name', fallback='EQDB'),
    'base_url': site_config.get('site', 'base_url', fallback='').rstrip('/'),
    'patcher_url': site_config.get('site', 'patcher_url', fallback=''),
    'discord_invite': site_config.get('site', 'discord_invite', fallback=''),
    'support_url': site_config.get('site', 'support_url', fallback=''),
    'partner_feed_enabled': _site_bool('partner_feed_enabled'),
    'partner_feed_path': site_config.get('site', 'partner_feed_path', fallback=''),
}
# True only when the optional private extension (eqdb_private) is installed; templates
# use it to hide links to pages that the public build does not have.
app.config.setdefault('PRIVATE_FEATURES', False)

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

flask_log_path = site_config.get('path', 'flask_log')
if os.path.dirname(flask_log_path):
    os.makedirs(os.path.dirname(flask_log_path), exist_ok=True)
fh = logging.FileHandler(flask_log_path)
app.logger.addHandler(fh)
app.logger.setLevel(logging.INFO)   # keep boot/calibration INFO lines in the log

app.secret_key = os.environ.get('FLASK_SECRET_KEY') or site_config.get('flask', 'secret_key', fallback='')
if not app.secret_key:
    # No configured key: sessions still work, but they reset whenever the process
    # restarts and are not shared between gunicorn workers. Set FLASK_SECRET_KEY.
    app.secret_key = secrets.token_hex(32)
    app.logger.warning('FLASK_SECRET_KEY is not set; using a random per-process key')
# Persist the login for the whole session (30 days), site-wide. SameSite=Lax so the
# session cookie survives the Discord OAuth top-level redirect back; Secure in prod.
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
if SITE_TYPE != 'Development':
    app.config['SESSION_COOKIE_SECURE'] = True
app.config['SITE_TYPE'] = SITE_TYPE
app.config['SITE_VERSION'] = SITE_VERSION
app.config['DISCORD_CLIENT_ID'] = os.environ.get('DISCORD_CLIENT_ID', site_config.get('discord', 'client_id'))
app.config['DISCORD_CLIENT_SECRET'] = os.environ.get('DISCORD_CLIENT_SECRET', site_config.get('discord', 'client_secret'))

if os.environ.get('OAUTHLIB_INSECURE_TRANSPORT') == 'true' or SITE_TYPE == 'Development':
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = "true"

default_redirect = ("http://localhost:5001/callback" if SITE_TYPE == 'Development'
                    else (SITE_SETTINGS['base_url'] + "/callback/" if SITE_SETTINGS['base_url'] else ""))
app.config['DISCORD_REDIRECT_URI'] = os.environ.get('DISCORD_REDIRECT_URI', default_redirect)
app.config['DISCORD_BOT_TOKEN'] = ""

# Raid Leaderboard V2 config -- env vars win over configuration.ini (consistent with
# how DISCORD_CLIENT_ID / DB creds are handled above), with safe config fallbacks.
app.config['LEADERBOARD_STAFF_IDS'] = os.environ.get(
    'LEADERBOARD_STAFF_IDS',
    site_config.get('leaderboard', 'staff_discord_ids', fallback=''))
app.config['LEADERBOARD_INGEST_TOKEN'] = os.environ.get(
    'LEADERBOARD_INGEST_TOKEN',
    site_config.get('leaderboard', 'ingest_token', fallback=''))

# Forum moderators: may edit or delete anyone's forum post. Kept SEPARATE from
# the leaderboard staff list on purpose -- that list can approve and invalidate
# raid encounters, and moderating a forum should not carry the power to rewrite
# the leaderboard. Falls back to the staff list when unset, so existing staff
# keep moderating if this is never configured.
app.config['FORUM_MODERATOR_IDS'] = os.environ.get(
    'FORUM_MODERATOR_IDS',
    site_config.get('forums', 'moderator_ids',
                    fallback=app.config['LEADERBOARD_STAFF_IDS']))

# ---------------------------------------------------------------------------
# Login-server player count. The public build has no login-server client: the
# endpoint reports "unavailable" until a provider is installed (an optional
# private extension may install one). If [loginserver] api_url or api_token is
# empty, no call to a login server is ever made.
# ---------------------------------------------------------------------------
app.config['LOGIN_API_URL'] = os.environ.get(
    'LOGIN_API_URL', site_config.get('loginserver', 'api_url', fallback=''))
app.config['LOGIN_API_TOKEN'] = os.environ.get(
    'LOGIN_API_TOKEN', site_config.get('loginserver', 'api_token', fallback=''))
app.config['PLAYER_COUNT_CORS_ORIGIN'] = os.environ.get(
    'PLAYER_COUNT_CORS_ORIGIN', site_config.get('loginserver', 'cors_origin', fallback='*'))

import time as _time
import threading as _threading


def _no_player_count():
    """Default provider: no login server configured."""
    return None


# Callable returning {'server', 'short_name', 'players', 'status', 'updated'} or None.
app.extensions['eqdb_player_count_provider'] = _no_player_count


def _pc_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = app.config['PLAYER_COUNT_CORS_ORIGIN']
    resp.headers['Access-Control-Allow-Headers'] = 'X-API-Key, Content-Type'
    resp.headers['Cache-Control'] = 'public, max-age=30'
    return resp


@app.route("/api/player-count", methods=['GET', 'OPTIONS'])
def player_count_api():
    """Read-only player count. Reports 'unavailable' when no provider is installed."""
    if request.method == 'OPTIONS':
        return _pc_cors(app.response_class(status=204))
    data = app.extensions['eqdb_player_count_provider']()
    if not data:
        return _pc_cors(jsonify({'server': None, 'players': None, 'status': 'unavailable'})), 503
    return _pc_cors(jsonify(data))


# ---------------------------------------------------------------------------
# Top Players API (secure, cached) -- composite ranking for client apps.
# Columns: rank, name, class, level, aa_spent, gear_score, player_score.
# ---------------------------------------------------------------------------
_TP_TTL = 60  # seconds -- get_top_players scores every character, so cache it
_tp_cache = {'data': None, 'ts': 0}

def _get_top_players_cached():
    now = _time.time()
    if _tp_cache['data'] is not None and (now - _tp_cache['ts']) < _TP_TTL:
        return _tp_cache['data']
    try:
        players = characters.get_top_players(100)
    except Exception as e:  # never take the endpoint down over a DB hiccup
        app.logger.warning('top players fetch failed: %s', e)
        return _tp_cache['data']  # serve stale if we have it, else None
    out = [{
        'rank': p['rank'],
        'name': p['name'],
        'class': p['class_name'],
        'level': p['level'],
        'aa_spent': p['aa'],
        'gear_score': p['gear_score'],
        'player_score': p['score'],
        'guild': p.get('guild_name', ''),
        'guild_id': p.get('guild_id'),   # additive; existing consumers unaffected
    } for p in players]
    _tp_cache['data'] = out
    _tp_cache['ts'] = now
    return out


@app.route("/api/top-players", methods=['GET', 'OPTIONS'])
def top_players_api():
    """Public: top players ranked by the composite score. ?limit=N (1-100)."""
    if request.method == 'OPTIONS':
        return _pc_cors(app.response_class(status=204))
    try:
        limit = int(request.args.get('limit', 25))
    except (TypeError, ValueError):
        limit = 25
    limit = max(1, min(limit, 100))
    data = _get_top_players_cached()
    if data is None:
        return _pc_cors(jsonify({'players': [], 'updated': int(_time.time())})), 503
    return _pc_cors(jsonify({'players': data[:limit], 'updated': int(_time.time())}))


@app.route("/api/top-player", methods=['GET', 'OPTIONS'])
def top_player_api():
    """Public: convenience endpoint for the single #1 player."""
    if request.method == 'OPTIONS':
        return _pc_cors(app.response_class(status=204))
    data = _get_top_players_cached()
    if not data:
        return _pc_cors(jsonify({'player': None, 'updated': int(_time.time())})), 503
    return _pc_cors(jsonify({'player': data[0], 'updated': int(_time.time())}))


# ---------------------------------------------------------------------------
# Gear-score calibration self-heals at STARTUP (lead-dev directive): the site
# code and the leaderboard-DB calibration (lb_item_score + reference_max) must
# never drift apart, so every boot rebuilds them in a background thread and
# then re-warms the ranking caches. A stale DB otherwise clamps players at
# 1000. Disable with GEAR_CALIBRATE_ON_BOOT=0 if a boot must skip it.
def _recalibrate_gear_scores_on_boot():
    try:
        t0 = _time.time()
        # One worker calibrates; the other gunicorn workers wait for it and only warm their own
        # caches (all 4 used to run the full calibration at once against the same database).
        import fcntl
        lock = open('/tmp/eqdb_gear_calibration.lock', 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            leader = True
        except OSError:
            leader = False
        if leader:
            app.logger.info('gear calibration: rebuilding lb_item_score + reference_max...')
            import build_gear_scores
            build_gear_scores.main()
        else:
            app.logger.info('gear calibration: another worker is calibrating; waiting for it')
            fcntl.flock(lock, fcntl.LOCK_EX)  # returns when the calibrating worker is done
        fcntl.flock(lock, fcntl.LOCK_UN)
        # bust the ranking caches so corrected scores appear immediately
        _tp_cache['data'], _tp_cache['ts'] = None, 0
        characters._rank_cache['data'], characters._rank_cache['ts'] = None, 0
        _get_top_players_cached()
        characters.get_player_rank_map()
        app.logger.info('gear calibration: done in %.1fs', _time.time() - t0)
        # the Players page's running-season boards, so the first click is not a 6 s compute
        import season_top_players as _stp
        _stp.warm_running_season()
        app.logger.info('season boards warmed in %.1fs', _time.time() - t0)
    except Exception as e:
        app.logger.error('gear calibration on boot FAILED (scores may clamp at 1000): %s', e)
        # still warm the caches so the site serves
        _get_top_players_cached()
        characters.get_player_rank_map()
        try:
            import season_top_players as _stp
            _stp.warm_running_season()
        except Exception as e2:
            app.logger.error('season boards warm FAILED: %s', e2)

if os.environ.get('GEAR_CALIBRATE_ON_BOOT', '1') != '0':
    _threading.Thread(target=_recalibrate_gear_scores_on_boot, daemon=True).start()
else:
    _threading.Thread(target=_get_top_players_cached, daemon=True).start()
    _threading.Thread(target=characters.get_player_rank_map, daemon=True).start()

discord = DiscordOAuth2Session(app)


def _forwarded_origin(header, fallback):
    """First value of a possibly comma-listed X-Forwarded-* header.

    Proxies APPEND to these headers left-to-right (client, proxy1, proxy2, ...),
    so the LEFTMOST entry is the host/scheme the browser actually used. ProxyFix
    and request.host resolve the *nearest* proxy instead, which behind a chain of
    proxies can be an internal upstream name -- not the domain the user is on.
    Taking the leftmost value keeps the callback on the request's true origin.
    """
    raw = request.headers.get(header)
    if not raw:
        return fallback
    first = raw.split(',')[0].strip()
    return first or fallback


def _discord_redirect_uri():
    """Build the OAuth redirect URI as <origin>/<prefix>/callback/.

    Discord matches redirect_uri as an exact string, so login and callback must
    produce byte-identical values or the token exchange fails. The host MUST be
    the origin the browser is on: a user on one domain has to get that domain's
    callback.

    This ALWAYS derives from the request in production. One deployment can serve
    several domains, so a static DISCORD_REDIRECT_URI env var could only ever be
    correct for ONE of them.
    The override is therefore honoured ONLY in local Development, where there is
    no proxy to infer the host from. X-Forwarded-Prefix is honoured so a site
    mounted under a sub-path (e.g. /server-3/) gets that prefix back.
    """
    if SITE_TYPE == 'Development':
        override = os.environ.get('DISCORD_REDIRECT_URI')
        if override:
            return override
    scheme = _forwarded_origin('X-Forwarded-Proto', request.scheme)
    host = _forwarded_origin('X-Forwarded-Host', request.host)
    prefix = request.headers.get('X-Forwarded-Prefix', '').rstrip('/')
    return f"{scheme}://{host}{prefix}/callback/"


def _session_user():
    """The Discord identity cached in the session at login -- read this everywhere
    (header, dashboard, forums) instead of calling discord.fetch_user() per page.

    That API call is rate-limited, and its intermittent failures used to make the
    header flicker back to 'signed out' from page to page, so users thought their
    login hadn't persisted. Caching it in the session fixes that: one fetch at
    login, then pure cookie reads for the rest of the session. Backfills older
    sessions that still have a token but no cached identity."""
    du = session.get('duser')
    # Sessions cached before display names (no 'username' key) are refreshed once below.
    if du and 'username' in du:
        return du
    try:
        if discord.authorized:
            u = discord.fetch_user()
            # 'name' is what the site shows (header, forum posts and replies): the Discord DISPLAY
            # name (global_name), falling back to the account name when none is set.
            display = None
            try:
                display = (discord.request('/users/@me') or {}).get('global_name')
            except Exception:
                pass
            du = {'id': str(u.id), 'name': display or u.name, 'username': u.name, 'avatar': u.avatar_url}
            session['duser'] = du
            session.permanent = True
            return du
    except Exception:
        pass
    return None


@app.context_processor
def inject_discord_user():
    """Expose login state to every template (the header profile chip uses it).
    Pure session read -- no per-page Discord API call, so it persists site-wide."""
    return {'discord_user': _session_user(),
            'private_features': app.config.get('PRIVATE_FEATURES', False),
            'site': SITE_SETTINGS}

app_path = site_config.get('path', 'app_log')
if os.path.dirname(app_path):
    os.makedirs(os.path.dirname(app_path), exist_ok=True)
formatter = logging.Formatter(
    '%(asctime)s: %(levelname)s - %(funcName)s - %(message)s',
    '%m/%d/%Y-%I%M%S %p')

app_log = logging.getLogger('app_log')
afh = logging.FileHandler(app_path)
afh.setFormatter(formatter)
app_log.addHandler(afh)
app_log.setLevel(logging.DEBUG)
ALLOWED_EXTENSIONS = {'txt'}

""" BLUEPRINTS """
app.register_blueprint(apis.api_pages)
app.register_blueprint(pets.pet_pages)
app.register_blueprint(spells.spell_pages)
app.register_blueprint(zones.zone_pages)
app.register_blueprint(tradeskills.tradeskill_pages)
app.register_blueprint(npcs.npc_pages)
app.register_blueprint(items.item_pages)
app.register_blueprint(factions.faction_pages)
app.register_blueprint(characters.bp)
app.register_blueprint(guild_routes.bp)
app.register_blueprint(wiki.bp)
app.register_blueprint(community_wiki.bp)
app.register_blueprint(leaderboard_routes.leaderboard_pages)
app.register_blueprint(build_simulator_routes.bp)
app.register_blueprint(aa_routes.bp)


""" MAIN METHODS """


@app.route("/site_error")
@app.errorhandler(Exception)
def all_exception_handler(error):
    app.logger.exception(error)
    referrer = request.path
    if SITE_TYPE == 'Development':
        raise error
    logic.send_error_email(error, referrer)
    return render_template('site_error.html')


@app.route("/login/")
def login():
    discord.redirect_uri = _discord_redirect_uri()
    app.config['DISCORD_REDIRECT_URI'] = discord.redirect_uri
    # Log the raw forwarded headers next to the chosen URI: if a callback ever
    # lands on the wrong domain, this line shows exactly what the proxy sent.
    app.logger.info('discord login redirect_uri=%s host=%r xfh=%r xfp=%r xfpre=%r',
                    discord.redirect_uri, request.host,
                    request.headers.get('X-Forwarded-Host'),
                    request.headers.get('X-Forwarded-Proto'),
                    request.headers.get('X-Forwarded-Prefix'))
    return discord.create_session(scope=['identify'])


@app.route("/logout/")
def logout():
    session.pop('duser', None)
    try:
        discord.revoke()
    except Exception:
        app.logger.exception('discord revoke failed; clearing session anyway')
    session.clear()
    return redirect(url_for('main_page'))


@app.route("/user/weights/delete/<int:wid>")
@requires_authorization
def delete_weights_set(wid):
    user = discord.fetch_user()
    success = local.delete_weights_set(user, wid)
    if not success:
        flash('You are not allowed to view, modify, or delete this set.')
        return redirect(url_for('error'))
    flash('Item Weights Set deleted')
    return redirect(url_for('user_home'))


@app.route("/user/weights/update/<int:wid>", methods=['POST'])
@requires_authorization
def update_weights_set(wid):
    user = discord.fetch_user()

    filters = {}
    for i in range(1, 100):
        thing = 'weight_%s' % i
        thing_val = 'stat_weight_%s' % i
        if thing in request.form:
            if request.form[thing] in filters:
                flash('Cannot add the same item stat weight twice.')
                return redirect(url_for('get_restrict_set', wid=wid))
            if 'none' in request.form[thing]:
                continue

            filters.update({
                request.form[thing]: float(request.form[thing_val])})
        else:
            continue
    result = local.update_weights_set(wid, filters, user=user)
    if not result:
        flash('You are not allowed to view or modify this set.')
        return redirect(url_for('error'))
    flash('Item Weights Set updated!')
    return redirect(url_for('get_weights_set', wid=wid))


@app.route("/user/weights/<int:wid>")
@requires_authorization
def get_weights_set(wid):
    user = discord.fetch_user()
    data = local.get_weight_set(wid, user)
    if not data:
        flash('You are not allowed to view or modify this set.')
        return redirect(url_for('error'))
    return render_template('weights_detail.html', data=data, wid=wid)


@app.route("/user/weights/create", methods=['GET', 'POST'])
@requires_authorization
def create_weights_set():
    user = discord.fetch_user()
    if request.method == 'GET':
        return render_template("create_weights.html")
    else:
        filters = {}
        for i in range(1, 100):
            thing = 'weight_%s' % i
            thing_val = 'stat_weight_%s' % i
            if thing in request.form:
                if request.form[thing] in filters:
                    flash('Cannot add the same item stat filter twice.')
                    return redirect(url_for('create_weights_set'))
                if 'none' in request.form[thing]:
                    continue

                filters.update({
                    request.form[thing]: float(request.form[thing_val])})
            else:
                continue
        name = request.form['set_name']
        if len(name) == 0:
            flash('Must enter a set name')
            return redirect(url_for('create_weights_set'))

        flash('Item Stat Weight Set created successfully!')
        wid = local.add_weights_set(user, name, filters)
        return redirect(url_for('get_weights_set', wid=wid))


@app.route("/user/gear/delete/<int:glid>", methods=['POST'])
@requires_authorization
def delete_gear_list(glid):
    """NOTE: THIS FEATURE IS CURRENTLY DARK AS IT IS BEING EVALUATED WHETHER EQDB SHOULD PROVIDE THIS OR NOT"""
    return 'Not Implemented'
    user = discord.fetch_user()
    return 'NYI'


@app.route("/user/gear/update/<int:item_id>", methods=['POST'])
@requires_authorization
def update_gear_list(item_id):
    """NOTE: THIS FEATURE IS CURRENTLY DARK AS IT IS BEING EVALUATED WHETHER EQDB SHOULD PROVIDE THIS OR NOT"""
    return 'Not Implemented'
    user = discord.fetch_user()
    glid = None
    slot = None
    aug_slot = None
    if 'glid' not in request.form:
        flash('Must select gear list to update')
    else:
        glid = request.form['glid']
    if 'slot' in request.form:
        slot = request.form['slot']
        if slot == 'None':
            flash('Must select a slot for this item.')
            return redirect(url_for('error'))
    if 'augslot' in request.form:
        aug_slot = request.form['augslot']
    result = local.update_gear_list(user, glid, item_id, slot, aug_slot)
    if result is None:
        flash('You are not authorized to view, update, or delete this gear list.')
        return redirect(url_for('error'))
    elif not result:
        flash('You must select an augment slot for this item.')
        return redirect(url_for('error'))
    else:
        flash('Item added!')
        return redirect(url_for('item_detail', item_id=item_id))


@app.route("/user/gear/<int:glid>")
@requires_authorization
def get_gear_list(glid):
    """NOTE: THIS FEATURE IS CURRENTLY DARK AS IT IS BEING EVALUATED WHETHER EQDB SHOULD PROVIDE THIS OR NOT"""
    return 'Not Implemented'
    user = discord.fetch_user()
    data = local.get_gear_list(user, glid)
    return render_template('gear_list_detail.html', data=data)


@app.route("/user/gear/create", methods=['GET', 'POST'])
@requires_authorization
def create_gear_list():
    """NOTE: THIS FEATURE IS CURRENTLY DARK AS IT IS BEING EVALUATED WHETHER EQDB SHOULD PROVIDE THIS OR NOT"""
    return 'Not Implemented'
    user = discord.fetch_user()
    if request.method == 'GET':
        return render_template('create_gear_list.html')
    else:
        name = request.form['set_name']
        if len(name) == 0:
            flash('Must enter a set name')
            return redirect(url_for('create_weights_set'))
        if 'public' in request.form:
            is_public = True
        else:
            is_public = False
        glid = local.create_gear_list(user, name, is_public)
        flash('Gear list created')
        return render_template(url_for('get_gear_list', glid=glid))


@app.route("/user/")
@requires_authorization
def user_home():
    du = _session_user()
    if not du:
        return redirect(url_for('login'))
    main_id = local.get_main_id(du['id'])
    data = characters.get_dashboard(du['id'], main_id)
    duser = {'name': du['name'], 'avatar': du.get('avatar')}
    raw_staff = app.config.get('LEADERBOARD_STAFF_IDS', '')
    if isinstance(raw_staff, (set, list, tuple)):
        staff_ids = {str(x) for x in raw_staff}
    else:
        staff_ids = {s.strip() for s in str(raw_staff).split(',') if s.strip()}
    return render_template('user_home.html', data=data, duser=duser,
                           is_staff=str(du['id']) in staff_ids,
                           item_quality=local.get_item_quality(du['id']))


@app.route("/user/item-quality", methods=['POST'])
@requires_authorization
def set_item_quality():
    """Which version of an item the NPC loot lists link to (Base / Enchanted / Legendary)."""
    du = _session_user()
    if not du:
        return redirect(url_for('login'))
    local.set_item_quality(du['id'], request.form.get('item_quality', 'Base'))
    return redirect(url_for('user_home'))


@app.route("/callback/")
def callback():
    discord.redirect_uri = _discord_redirect_uri()
    app.config['DISCORD_REDIRECT_URI'] = discord.redirect_uri
    discord.callback()
    session.permanent = True
    session.pop('duser', None)   # drop any prior identity, then cache the new one once
    _session_user()
    return redirect(url_for("user_home"))


@app.route("/install-help")
def install_help():
    """Client installation & troubleshooting guide."""
    return render_template('install_help.html')


@app.errorhandler(Unauthorized)
def redirect_unauthorized(e):
    return redirect(url_for("login"))


@app.route("/search/all", methods=['POST'])
def all_search():
    name = request.form['all_names']
    if len(name) > 50:
        flash('Search by name limited to 50 characters.')
        return redirect(url_for('main_page'))
    elif len(name) < 3:
        flash('Search by name requires at least 3 characters.')
        return redirect(url_for('main_page'))
    if not name.isascii():
        flash('Only ASCII characters are allowed.')
        return redirect(url_for('main_page'))
    data = logic.all_search(name=name)
    return render_template('all_search_result.html', search_txt=name, data=data)


@app.route("/tooltip/<item_id>", methods=['GET', 'POST'])
def tooltip(item_id):
    return render_template('tooltip.html', item=logic.get_item_data(item_id))


@app.route("/spell-tooltip/<spell_id>", methods=['GET', 'POST'])
def spell_tooltip(spell_id):
    _, slots = spell.get_spell_tooltip(spell_id)
    return render_template('spell_tooltip.html', slots=slots)


@app.route("/about", methods=['GET'])
def about():
    with open(os.path.join(here, 'about.txt'), 'r') as fh:
        data = fh.read()
    return render_template('changelog.html', data=data)


@app.route("/changelog", methods=['GET'])
def changelog():
    with open(os.path.join(here, 'changelog.txt'), 'r') as fh:
        data = fh.read()
    return render_template('changelog.html', data=data)


@app.route("/error", methods=['GET'])
def error():
    return render_template('blank.html')


# Homepage "Top Players" board cache. Computing the composite ranking on each
# landing hit would be wasteful, so cache the small board for a few minutes.
_HOME_PLAYERS_CACHE = {'at': 0.0, 'rows': []}
_HOME_PLAYERS_TTL = 300  # seconds


def _homepage_top_players():
    import time
    now = time.time()
    if now - _HOME_PLAYERS_CACHE['at'] < _HOME_PLAYERS_TTL and _HOME_PLAYERS_CACHE['rows']:
        return _HOME_PLAYERS_CACHE['rows']
    try:
        # the running season's Top Players (Season 2: speed + LDoN progression), not the all-time board
        import leaderboard_read as _lbr
        import season_top_players as _stp
        rows = _stp.board(_lbr.resolve_season(None))['players'][:15]
    except Exception:
        app.logger.exception('homepage top players failed')
        rows = _HOME_PLAYERS_CACHE['rows']  # keep last good board if we have one
    _HOME_PLAYERS_CACHE['at'] = now
    _HOME_PLAYERS_CACHE['rows'] = rows
    return rows


@app.route("/", methods=['GET'])
def main_page():
    # Live "Top Players" board (cached). Never let a data issue take the
    # landing page down - fall back to an empty board.
    return render_template('main.html', top_players=_homepage_top_players(),
                           score_weights=characters.SCORE_WEIGHTS)


@app.route("/debug/tester", methods=['GET'])
def tester():
    if SITE_TYPE != 'Development':
        flash("Unauthorized")
        return redirect(url_for('error'))
    # data, slots = logic._debugger()
    return render_template('blank.html')


@app.route("/search/item", methods=['POST'])
def item_search():
    # Do some validations
    filters = {}
    g_slot = None
    show_dmg_delay = False
    show_spells = False
    full_detail = False
    show_click = False
    show_proc = False
    data = request.form

    # Make sure _something_ was provided
    if (request.form['g_class_1'] == 'None' and
        request.form['g_class_2'] == 'None' and
        request.form['g_class_3'] == 'None' and
        request.form['g_slot'] == 'None' and
        request.form['i_type'] == 'None' and
        request.form.get('spell_type', '') == '' and
        'pet_search' not in data):
        flash('Must request a slot, class, item type other than all, or focus effect, or select Search for Pet Items')
        return redirect(url_for('error'))

    if 'g_class_1' in request.form:
        if request.form['g_class_1'] != 'None':
            filters.update({'g_class_1': request.form['g_class_1']})
    if 'g_class_2' in request.form:
        if request.form['g_class_2'] != 'None':
            filters.update({'g_class_2': request.form['g_class_2']})
    if 'g_class_3' in request.form:
        if request.form['g_class_3'] != 'None':
            filters.update({'g_class_3': request.form['g_class_3']})

    if 'g_slot' in request.form:
        if request.form['g_slot'] != 'None':
            filters.update({'g_slot': request.form['g_slot']})
            g_slot = request.form['g_slot']
            if 'Primary' in g_slot or 'Secondary' in g_slot or 'Range' in g_slot:
                show_dmg_delay = True

    # Handle Augment filtering (exclude augments, or search augments only)
    aug_filter = data.get('aug_filter')
    if aug_filter == 'exclude':
        filters.update({'exclude_augs': True})
    elif aug_filter == 'only':
        filters.update({'i_type': 'Augment'})

    # Handle Name
    if 'item_name' in request.form:
        if len(request.form['item_name']) > 0:
            filters.update({'item_name': request.form['item_name']})

    # Handle Skill Mod
    if request.form['skillmodtype'] != 'None':
        filters.update({'skillmodtype': request.form['skillmodtype']})

    # Handle Item Type
    if request.form['i_type'] != 'None':
        i_type = request.form['i_type']

        # Do some validations here to stop insanity
        if g_slot is not None:
            # Handle Bows not in Range
            if 'Bow' in i_type and 'Range' not in g_slot:
                flash(f'Cannot search for item type "Bow" in slot {g_slot}')
                return redirect(url_for('error'))

            # Handle ranged with other weapon types
            if (('Bow' not in i_type and 'Any' not in i_type and 'Augment' not in i_type and 'Thrown' not in i_type) and
                 'Range' in g_slot):
                flash(f'Range slot can only search for "Thrown", "Bow", or "Any"')
                return redirect(url_for('error'))

            # Handle Shield not in Secondary
            if 'Shield' in i_type and 'Secondary' not in g_slot and 'Back' not in g_slot:
                flash(f'Cannot search for item type "Shield" in slot {g_slot}')
                return redirect(url_for('error'))

            # Handle 2H not in Primary
            if (i_type in ['Two Hand Slash', 'Two Hand Blunt', 'Two Hand Piercing', 'Any 2H Weapon'] and
                    'Primary' not in g_slot):
                flash(f'Cannot search for Two-Handed items in slot {g_slot}')
                return redirect(url_for('error'))

            # Handle arrows in non-ammo slots
            if i_type == 'Arrow' and g_slot != 'Ammo':
                flash(f'Arrows can only go in the Ammo slot.')
                return redirect(url_for('error'))

            # Handle weapons in non-weapon slots
            if (i_type not in ['Any', 'Shield', 'Bow', 'Augment', 'Arrow', 'Thrown'] and
                g_slot not in ['Primary', 'Secondary']):
                flash(f'Cannot search for weapons in slot {g_slot}')
                return redirect(url_for('error'))

        filters.update({'i_type': i_type})

    # Handle Eras
    base_era_list = ['Classic', 'Kunark', 'Velious', 'Luclin', 'Planes']
    remove_era_list = request.form.getlist('eras')
    era_list = []
    for era in base_era_list:
        if era in remove_era_list:
            continue
        else:
            era_list.append(era)
    filters.update({'eras': era_list})

    # Handle Proc Weapon Items
    if 'proc' in data:
        if 'True' in data['proc']:
            if g_slot:
                if g_slot == 'Primary' or g_slot == 'Secondary' or g_slot == 'Range':
                    filters.update({'proc': True})
                    show_proc = True
                    if 'proc_level' in data:
                        if int(data['proc_level']) > 0:
                            filters.update({'proclevel2': data['proc_level']})
    # The armor search form has no proc_type control, so an unguarded read raised
    # BadRequestKeyError (400) on every armor search. Absent means no proc filter.
    if 'proc_type' in data and data['proc_type'] != 'None':
        filters.update({'proc_type': data['proc_type']})
        show_proc = True

    # Handle Click Effect Items
    if 'click' in data:
        if 'True' in data['click']:
            filters.update({'click': True})
            show_click = True
            if 'click_level' in data:
                if int(data['click_level']) > 0:
                    filters.update({'clicklevel2': data['click_level']})

    # Handle Sympathetics
    if 'sympathetic' in data:
        if data['sympathetic'] != 'None':
            filters.update({'sympathetic': data['sympathetic']})
            show_click = True

    # Handle Stat Requirements
    reduce_changed = False
    reduce_restrictions = {
        'ac': False,
        'hp': False,
        'mana': False,
        'astr': False,
        'asta': False,
        'aagi': False,
        'adex': False,
        'awis': False,
        'aint': False,
        'acha': False,
        'heroic_str': False,
        'heroic_sta': False,
        'heroic_agi': False,
        'heroic_dex': False,
        'heroic_wis': False,
        'heroic_int': False,
        'heroic_cha': False,
        'fr': False,
        'cr': False,
        'pr': False,
        'dr': False,
        'mr': False,
        'attack': False,
        'haste': False,
        'regen': False,
        'manaregen': False,
        'spelldmg': False,
        'healamt': False,
        'accuracy': False,
        'avoidance': False,
        'combateffects': False,
        'damageshield': False,
        'dotshielding': False,
        'shielding': False,
        'spellshield': False,
        'strikethrough': False,
        'stunresist': False,
    }
    for i in range(1, 100):
        thing = 'stat_%s' % i
        thing_val = 'stat_val_%s' % i
        if thing in request.form:
            if request.form[thing] in filters:
                flash('Cannot add the same item stat filter twice.')
                return redirect(url_for('error'))
            if 'none' in request.form[thing]:
                continue

            filters.update({
                request.form[thing]: float(request.form[thing_val])})
            if 'reduce_restrict' in request.form:
                reduce_changed = True
                reduce_restrictions.update({request.form[thing]: True})
        else:
            continue

    # Handle Weights
    show_values = False
    weights = {}
    for i in range(1, 100):
        thing = 'weight_%s' % i
        thing_val = 'stat_weight_%s' % i
        if thing in request.form:
            show_values = True
            if request.form[thing] in weights:
                flash('Cannot add the same item stat weight twice.')
                return redirect(url_for('error'))
            if 'none' in request.form[thing]:
                continue
            weights.update({
                request.form[thing]: float(request.form[thing_val])})
            if 'show_weight_detail' in request.form:
                reduce_changed = True
                reduce_restrictions.update({request.form[thing]: True})
        else:
            continue

    # Handle Show Table
    if 'show_full_detail' in request.form:
        full_detail = True

    if not reduce_changed:
        # Flip them all to true
        for entry in reduce_restrictions:
            reduce_restrictions[entry] = True

    # Handle Ignore Zero
    if 'ignore_zero' in request.form:
        if not show_values:
            flash('Include zero weight items requires at least one weight.')
            return redirect(url_for('error'))
        ignore_zero = False
    else:
        ignore_zero = True

    # Handle no rent
    if 'pet_search' in request.form:
        filters.update({'pet_search': True})

    # Handle focus effects
    if 'spell_type' in request.form:
        if request.form['spell_type'] is not None and request.form['spell_type'] != '':
            filters.update({'focus_type': request.form['spell_type']})
            filters.update({'sub_type': request.form['focus_type']})
            show_spells = True

    # Handle elem damage
    if 'elemdmgtype' in data:
        if g_slot:
            if g_slot == 'Primary' or g_slot == 'Secondary' or g_slot == 'Range':
                if data['elemdmgtype'] != 'all':
                    filters.update({'elemdmgtype': data['elemdmgtype']})

    # Handle bane damage
    if 'banetype' in data:
        if g_slot:
            if g_slot == 'Primary' or g_slot == 'Secondary' or g_slot == 'Range':
                if data['banetype'] != 'all':
                    bane_id = int(data['banetype'].split('_')[1])
                    if 'body' in data['banetype']:
                        filters.update({'banedmgbody': bane_id})
                    else:
                        filters.update({'banedmgrace': bane_id})

    # Oh god, we've got everything...lets get a list of items
    ret_items, focus, worn, inst = logic.get_items_with_filters(weights, ignore_zero, **filters)

    # Show damage and delay whenever the results actually contain weapons. The old rule
    # set show_dmg_delay only when a Primary/Secondary/Range slot was chosen, so a weapon
    # search by item type (Two Hand Slash, Bow, ...) listed no damage or delay at all.
    if not show_dmg_delay:
        show_dmg_delay = any(getattr(e, 'damage', 0) for e in ret_items)
    return render_template('item_search_results.html', data=ret_items, reduce=reduce_restrictions,
                           show_dmg_delay=show_dmg_delay, show_focus=focus, full_detail=full_detail,
                           show_values=show_values, show_click=show_click, show_proc=show_proc, show_worn=worn,
                           show_inst=inst, show_spells=show_spells)


@app.route("/search/weapon", methods=['GET', 'POST'])
def weapon_search():
    if discord.authorized:
        user = discord.fetch_user()
    else:
        user = None
    if request.method == 'GET':
        skills = utils.get_all_skills()
        if user:
            restricts = local.get_restrict_sets(user)
            weights = local.get_weights_sets(user)
        else:
            restricts = []
            weights = []
        return render_template('weapon_search.html',
                               skills=skills,
                               restricts=restricts,
                               restrict_json=json.dumps(restricts),
                               weights=weights,
                               weights_json=json.dumps(weights))
    else:
        return redirect(url_for('item_search'), code=307)


@app.route("/search/stats", methods=['GET'])
def stat_search():
    slots = ['Charm', 'Ear', 'Head', 'Face', 'Neck', 'Shoulders', 'Arms', 'Back',
             'Wrist', 'Range', 'Hands', 'Primary', 'Secondary', 'Fingers', 'Chest',
             'Legs', 'Feet', 'Waist', 'Ammo']
    classes = ['Warrior', 'Cleric', 'Paladin', 'Ranger', 'Shadow Knight', 'Druid',
               'Monk', 'Bard', 'Rogue', 'Shaman', 'Necromancer', 'Wizard', 'Magician',
               'Enchanter', 'Beastlord', 'Berserker']
    stat_options = [
        ('hp', 'Health'), ('mana', 'Mana'), ('ac', 'AC'),
        ('astr', 'STR'), ('asta', 'STA'), ('aagi', 'AGI'), ('adex', 'DEX'),
        ('awis', 'WIS'), ('aint', 'INT'), ('acha', 'CHA'),
        ('heroic_str', 'Heroic STR'), ('heroic_sta', 'Heroic STA'),
        ('heroic_agi', 'Heroic AGI'), ('heroic_dex', 'Heroic DEX'),
        ('heroic_wis', 'Heroic WIS'), ('heroic_int', 'Heroic INT'),
        ('heroic_cha', 'Heroic CHA'),
        ('attack', 'Attack'), ('haste', 'Haste'), ('regen', 'HP Regen'),
        ('manaregen', 'Mana Regen'), ('spelldmg', 'Spell Damage'),
        ('healamt', 'Heal Amount'), ('accuracy', 'Accuracy'), ('avoidance', 'Avoidance'),
        ('combateffects', 'Combat Effects'), ('damageshield', 'Damage Shield'),
        ('dotshielding', 'DoT Shielding'), ('shielding', 'Shielding'),
        ('spellshield', 'Spell Shield'), ('strikethrough', 'Strikethrough'),
        ('stunresist', 'Stun Resist'),
        ('mr', 'Magic Resist'), ('fr', 'Fire Resist'), ('cr', 'Cold Resist'),
        ('dr', 'Disease Resist'), ('pr', 'Poison Resist'),
    ]
    return render_template('stat_search.html', slots=slots, classes=classes,
                           stat_options=stat_options)


@app.route("/search/armor", methods=['GET', 'POST'])
def armor_search():
    if discord.authorized:
        user = discord.fetch_user()
    else:
        user = None
    if request.method == 'GET':
        skills = utils.get_all_skills()
        if user:
            restricts = local.get_restrict_sets(user)
            weights = local.get_weights_sets(user)
        else:
            restricts = []
            weights = []
        return render_template('item_search.html',
                               skills=skills,
                               restricts=restricts,
                               restrict_json=json.dumps(restricts),
                               weights=weights,
                               weights_json=json.dumps(weights))
    else:
        return redirect(url_for('item_search'), code=307)


@app.route('/<path:filename>')
def serve_patch_files(filename):
    """Catch-all, end of the route chain: any unmatched URL lands on the home page.
    (The endpoint name is kept so an extension can serve files from here instead.)"""
    return redirect(url_for('main_page'))


# --- Optional private extension ------------------------------------------- #
# If an `eqdb_private` package is importable, it is given the app and the parsed
# configuration and may register extra blueprints, hooks and providers. Nothing
# here depends on it; without it the site runs with the public feature set.
app.config['SITE_SETTINGS'] = SITE_SETTINGS
app.extensions['eqdb_public'] = {
    'discord': discord,
    'session_user': _session_user,
    'discord_redirect_uri': _discord_redirect_uri,
    'pc_cors': _pc_cors,
    'top_players_cached': _get_top_players_cached,
}
try:
    import eqdb_private
except ModuleNotFoundError as e:
    if e.name != 'eqdb_private':
        raise  # a dependency missing INSIDE the extension must not be silently skipped
    eqdb_private = None
if eqdb_private:
    eqdb_private.register(app, site_config)
    logging.getLogger(__name__).warning('eqdb: private extension loaded')
else:
    logging.getLogger(__name__).warning('eqdb: private extension not present; public feature set only')


if __name__ == "__main__":
    app.debug = True
    app.run(port=5001)
