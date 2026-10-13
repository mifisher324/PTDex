"""Leaderboard V2 blueprint -- public read routes.

Follows the existing EQDB blueprint pattern (see routes/items.py etc.): thin
controllers that validate input and delegate to the domain layer
(``leaderboard_read``).  Encounter ingestion and staff tools are not part of the
public build; an optional private extension can register them (see README).
"""
from flask import (Blueprint, render_template, request, redirect, url_for, flash,
                   jsonify, current_app)

import leaderboard_read as lbr

leaderboard_pages = Blueprint('leaderboard', __name__, template_folder='templates')

VALID_GROUP_SIZES = (1, 2, 3)
VALID_CATEGORIES = lbr.CATEGORIES


def _season_ctx():
    """(season, seasons) for the page. ?season=season-1 opens a past season;
    anything else -- missing, unknown -- shows the season running now."""
    try:
        season = lbr.resolve_season(request.args.get('season'))
        seasons = lbr.list_seasons()
    except Exception:
        current_app.logger.exception('season lookup failed')
        season, seasons = None, []
    return season, seasons


@leaderboard_pages.route('/leaderboard/')
def leaderboard_home():
    """The race board. Falls back to the plain boss list if the board query
    fails -- a landing page must never 500 over a decoration."""
    season, seasons = _season_ctx()
    sid = season['id'] if season else None
    try:
        board = lbr.get_race_board(season_id=sid)
    except Exception:
        current_app.logger.exception('race board failed; falling back to boss list')
        board = None
    ldon = lbr.season_has_ldon(season)
    try:
        missions = lbr.get_mission_board(season_id=sid) if ldon else None
    except Exception:
        current_app.logger.exception('mission board failed')
        missions = None
    if board and not ldon:
        # LDoN raid boards exist only in Season 2 (the race board is {'bosses': [...], ...})
        board = dict(board, bosses=[b for b in board.get('bosses', [])
                                    if not lbr.is_ldon_board(b.get('boss_key'))])
    return render_template('leaderboard_home.html',
                           bosses=[b for b in lbr.list_bosses() if b['kind'] != 'mission'
                                   and (ldon or not lbr.is_ldon_board(b['boss_key']))],
                           board=board, missions=missions,
                           season=season, seasons=seasons)


@leaderboard_pages.route('/leaderboard/top-players')
def season_top_players():
    """Top Players for one season: live while it runs, frozen when it ends (season_top_players.py)."""
    import season_top_players as stp
    season, seasons = _season_ctx()
    data = {'players': [], 'frozen': False, 'frozen_at': None, 'weights': {}}
    if season:
        try:
            data = stp.board(season)
        except Exception:
            current_app.logger.exception('season top players failed')
    return render_template('leaderboard_top_players.html', data=data, season=season, seasons=seasons)


@leaderboard_pages.route('/leaderboard/ldon')
def ldon_speedruns():
    """LDoN Speedruns: the fastest adventure wins in every LDoN dungeon (ldon_speedruns.py)."""
    try:
        import ldon_speedruns as ls      # optional: server-specific LDoN content (private overlay)
    except ImportError:
        return redirect(url_for('leaderboard.leaderboard_home'))
    season, seasons = _season_ctx()
    try:
        data = ls.board(season)
    except Exception:
        current_app.logger.exception('ldon speedruns failed')
        data = {'recorded': False, 'themes': [], 'runs': 0, 'dungeons': 0}
    return render_template('leaderboard_ldon.html', data=data, season=season, seasons=seasons)


@leaderboard_pages.route('/leaderboard/characters')
def character_rankings():
    season, seasons = _season_ctx()
    return render_template('leaderboard_characters.html',
                           data=lbr.get_character_rankings(
                               limit=100, season_id=season['id'] if season else None),
                           season=season, seasons=seasons)


@leaderboard_pages.route('/leaderboard/boss/<boss_key>')
def boss_rankings(boss_key):
    # Group size + active tab come from the query string so the page is a stable,
    # bookmarkable URL: /leaderboard/boss/kerafyrm?group=1
    try:
        group_size = int(request.args.get('group', 1))
    except (TypeError, ValueError):
        group_size = 1
    if group_size not in VALID_GROUP_SIZES:
        group_size = 1

    season, seasons = _season_ctx()
    if lbr.is_ldon_board(boss_key) and not lbr.season_has_ldon(season):
        # LDoN / Monster Mission boards exist only in Season 2: open that season instead
        return redirect(url_for('leaderboard.boss_rankings', boss_key=boss_key,
                                group=request.args.get('group'), season=lbr.LDON_SEASON_KEY))
    data = lbr.get_boss_rankings(boss_key, group_size,
                                 season_id=season['id'] if season else None)
    if data is None:
        flash('Unknown boss.')
        return redirect(url_for('leaderboard.leaderboard_home'))
    if data['boss']['kind'] == 'mission' and group_size != 1:
        # Monster Missions are solo only; a duo/trio tab would always be empty.
        return redirect(url_for('leaderboard.boss_rankings', boss_key=boss_key,
                                season=request.args.get('season')))
    return render_template('leaderboard_boss.html', data=data,
                           bosses=lbr.list_bosses(), group_sizes=VALID_GROUP_SIZES,
                           season=season, seasons=seasons)


@leaderboard_pages.route('/leaderboard/encounter/<int:encounter_id>')
def encounter_detail(encounter_id):
    data = lbr.get_encounter_detail(encounter_id)
    if data is None:
        flash('Encounter not found.')
        return redirect(url_for('leaderboard.leaderboard_home'))
    return render_template('leaderboard_encounter.html', data=data)


@leaderboard_pages.route('/leaderboard/character/<int:character_id>')
def character_profile(character_id):
    season, seasons = _season_ctx()
    data = lbr.get_character_profile(character_id,
                                     season_id=season['id'] if season else None)
    if data is None:
        flash('No leaderboard data for that character yet.')
        return redirect(url_for('leaderboard.character_rankings'))
    return render_template('leaderboard_character.html', data=data,
                           season=season, seasons=seasons)
