"""Top Players per season -- one ranked page per season, frozen when the season ends.

The Players page (/leaderboards) shows the season running now and, with ?season=, a closed season frozen:
Top Players, AA and Gear all freeze together (AA and Gear in lb_season_board). Each season also has its
Top Players page under the leaderboard (/leaderboard/top-players?season=...):
  - a season that is running (or upcoming) is computed live with that season's weights;
  - a closed season reads the snapshot taken once when it ended (lb_season_top_players). It is written by
    the leaderboard sync the first time it runs after the season's end, or by the first page view if that
    comes first, and never recalculated -- the season is frozen.
"""
import json
import time as _time
from datetime import datetime

from sqlalchemy import text

import db
import leaderboard_read as lbr

LIMIT = 100

# Player Score weights per season (sum 1000).
#   Season 1: gear 375, AA 275, parse 200, level 150 (no speed).
#   Season 2 (decided 2026-09-28): speedruns 375, parse 300, gear 200, level 75, AA 50.
SEASON_WEIGHTS = {
    'season-1': {'level': 150.0, 'aa': 275.0, 'gear': 375.0, 'parse': 200.0, 'speed': 0.0, 'progression': 0.0},
    # Season 2 (2026-09-29): progression sits under gear. speed 325, parse 250, gear 200, progression 150, level 50, AA 25.
    'season-2': {'level': 50.0, 'aa': 25.0, 'gear': 200.0, 'parse': 250.0, 'speed': 325.0, 'progression': 150.0},
}


def speed_scores(season):
    """{character_id: speed score 0-100} for one season, averaged over EVERY board (a board not cleared
    counts 0). On each board a character scores (the board's fastest time / their best time) x 100 -- the
    record holder 100, twice as slow 50 -- taking their best across group sizes. Boards:
      - every active leaderboard board: raid bosses, the Plane of Time gauntlet, the LDoN raids and the
        Monster Missions (their valid runs in this season);
      - in Season 2, every LDoN adventure board: dungeon x objective x risk offered in the top band (63-75),
        from adventure_runs (every recorded run counts toward Season 2, as on the LDoN Speedruns tab).
    """
    best = {}          # board -> {character_id: best seconds}
    with lbr._session() as session:
        boards = [r[0] for r in session.execute(text("SELECT id FROM lb_boss WHERE active = 1")).all()]
        rows = session.execute(text(
            "SELECT e.boss_id, p.character_id, MIN(e.duration_seconds) FROM lb_encounter e "
            "JOIN lb_participant p ON p.encounter_id = e.id "
            "WHERE e.season_id = :s AND e.validation_status = 'valid' AND p.is_valid = 1 "
            "  AND p.character_id IS NOT NULL AND e.duration_seconds > 0 "
            "GROUP BY e.boss_id, p.character_id"), {'s': season['id']}).all()
    for bid, cid, secs in rows:
        best.setdefault(('lb', int(bid)), {})[int(cid)] = float(secs)
    all_boards = [('lb', b) for b in boards]

    try:
        # optional: server-specific LDoN content, present only with the private overlay
        import ldon_progress
        import ldon_source
        import ldon_speedruns
    except ImportError:
        ldon_progress = None
    if season.get('key') == 'season-2' and ldon_progress is not None:
        cat = ldon_progress._read_catalogue()
        types = {v: k for k, v in ldon_progress.OBJECTIVES.items()}
        cells = {(t['zone'], types.get(t['objective'], 0), bool(t['hard']))
                 for rows_ in cat.values() for t in rows_ if t['lo'] == ldon_speedruns.TOP_BAND}
        all_boards += [('adv',) + c for c in sorted(cells)]
        with db.get_db(ldon_source.engine()) as s:
            if ldon_speedruns._has_table(s):
                for zone, typ, hard, cid, secs in s.execute(text(
                        "SELECT zone, type, is_hard, character_id, MIN(run_seconds) FROM adventure_runs "
                        "WHERE min_level = :b AND run_seconds > 0 GROUP BY zone, type, is_hard, character_id"),
                        {'b': ldon_speedruns.TOP_BAND}).all():
                    best.setdefault(('adv', zone, int(typ), bool(hard)), {})[int(cid)] = float(secs)

    if not all_boards:
        return {}
    points = {}
    for board_key in all_boards:
        times = best.get(board_key)
        if not times:
            continue
        record = min(times.values())
        for cid, t in times.items():
            points[cid] = points.get(cid, 0.0) + record / t * 100.0
    return {cid: round(v / len(all_boards), 2) for cid, v in points.items()}



def progression_scores(season):
    """{character_id: 0-100} LDoN/MM progression for Season 2 -- equal thirds of distinct LDoN wings
    cleared (of 48: guk/mir/mmc/ruj/tak a-j), LDoN raids cleared (of 14) and Monster Missions completed
    (of 5). A wing/raid/mission counts once a character has a valid clear; unfinished thirds add 0."""
    if season.get('key') != 'season-2':
        return {}
    WINGS, RAIDS, MISSIONS = 48, 14, 5
    wings, raids, missions = {}, {}, {}
    with lbr._session() as session:
        rows = session.execute(text(
            "SELECT b.boss_key, p.character_id FROM lb_encounter e "
            "JOIN lb_participant p ON p.encounter_id = e.id "
            "JOIN lb_boss b ON b.id = e.boss_id "
            "WHERE e.season_id = :s AND e.validation_status = 'valid' AND p.is_valid = 1 "
            "  AND p.character_id IS NOT NULL "
            "  AND (b.boss_key LIKE 'ldon_%' OR b.boss_key LIKE 'mm_%')"), {'s': season['id']}).all()
    for bk, cid in rows:
        cid = int(cid)
        if bk.startswith('ldon_'):
            raids.setdefault(cid, set()).add(bk)
        elif bk.startswith('mm_'):
            missions.setdefault(cid, set()).add(bk)
    try:
        import ldon_source
        import ldon_speedruns
        with db.get_db(ldon_source.engine()) as s:
            if ldon_speedruns._has_table(s):
                for zone, cid in s.execute(text(
                        "SELECT DISTINCT zone, character_id FROM adventure_runs WHERE run_seconds > 0")).all():
                    wings.setdefault(int(cid), set()).add(zone)
    except Exception:
        pass          # adventure_runs not present yet -> wings third stays 0
    out = {}
    for cid in (set(wings) | set(raids) | set(missions)):
        w = min(len(wings.get(cid, ())), WINGS) / WINGS
        r = min(len(raids.get(cid, ())), RAIDS) / RAIDS
        m = min(len(missions.get(cid, ())), MISSIONS) / MISSIONS
        out[cid] = round((w + r + m) / 3.0 * 100.0, 2)
    return out


def ensure_table(session):
    session.execute(text(
        "CREATE TABLE IF NOT EXISTS lb_season_top_players ("
        " season_id INT NOT NULL, rank_position INT NOT NULL, character_id INT NOT NULL,"
        " payload JSON NOT NULL, frozen_at DATETIME NOT NULL,"
        " PRIMARY KEY (season_id, rank_position)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"))
    session.commit()


def _compute(season):
    from routes.characters import get_top_players
    w = SEASON_WEIGHTS.get(season['key'], SEASON_WEIGHTS['season-1'])
    speed = speed_scores(season) if w.get('speed') else {}
    prog = progression_scores(season) if w.get('progression') else {}
    return get_top_players(limit=LIMIT, season_id=season['id'], weights=w, speed_scores=speed, progression_scores=prog)


def _snapshot(session, season_id):
    rows = session.execute(text(
        "SELECT payload, frozen_at FROM lb_season_top_players WHERE season_id = :s ORDER BY rank_position"),
        {'s': season_id}).all()
    if not rows:
        return None, None
    return [json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in rows], rows[0][1]


def ensure_board_table(session):
    session.execute(text(
        "CREATE TABLE IF NOT EXISTS lb_season_board ("
        " season_id INT NOT NULL, board VARCHAR(16) NOT NULL, rank_position INT NOT NULL,"
        " character_id INT NOT NULL, payload JSON NOT NULL, frozen_at DATETIME NOT NULL,"
        " PRIMARY KEY (season_id, board, rank_position)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"))
    session.commit()


def _board_snapshot(session, season_id, board_key):
    rows = session.execute(text(
        "SELECT payload FROM lb_season_board WHERE season_id = :s AND board = :b ORDER BY rank_position"),
        {'s': season_id, 'b': board_key}).all()
    return [json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in rows] if rows else None


def _live_boards(season):
    """The AA and Gear boards as they stand now; gear scored the way ``season`` scores it."""
    from routes.characters import top_aa_rankings, get_gear_rankings
    return {'aa': top_aa_rankings(), 'gear': get_gear_rankings(season_id=season['id'])}


def _freeze_boards(season, session):
    """Freeze the AA and Gear boards with the season (once). Returns rows written."""
    ensure_board_table(session)
    if _board_snapshot(session, season['id'], 'aa') is not None:
        return 0
    now, n = datetime.utcnow(), 0
    for key, rows in _live_boards(season).items():
        for i, p in enumerate(rows, 1):
            session.execute(text(
                "INSERT IGNORE INTO lb_season_board (season_id, board, rank_position, character_id, payload, frozen_at) "
                "VALUES (:s, :b, :r, :c, :p, :t)"),
                {'s': season['id'], 'b': key, 'r': i, 'c': p['id'], 'p': json.dumps(p, default=str), 't': now})
            n += 1
    session.commit()
    return n


# The running season's boards score every character (Top Players: speed + progression + gear;
# then the AA and Gear boards), about 5-6 s of database work. Cached per worker for LIVE_TTL
# seconds, like the home page's Top Players; a failed refresh serves the last good copy.


def season_boards(season):
    """Everything the Players page shows for one season: Top Players, AA and Gear.
    Running season: live (that season's weights and gear scoring), cached LIVE_TTL seconds.
    Closed season: the frozen snapshot."""
    if not season or season.get('state') == 'upcoming':
        return {'players': [], 'aa': [], 'gear': [], 'frozen': False, 'frozen_at': None}
    if season.get('state') == 'closed':
        tp = board(season)
        with lbr._session() as session:
            ensure_board_table(session)
            aa = _board_snapshot(session, season['id'], 'aa')
            if aa is None:
                _freeze_boards(season, session)
                aa = _board_snapshot(session, season['id'], 'aa')
            gear = _board_snapshot(session, season['id'], 'gear')
        return {'players': tp['players'], 'aa': aa or [], 'gear': gear or [],
                'frozen': True, 'frozen_at': tp['frozen_at']}
    now = _time.time()
    cached = _live_cache.get(season['id'])
    if cached and now - cached['at'] < LIVE_TTL:
        return cached['data']
    try:
        tp = board(season)
        live = _live_boards(season)
    except Exception:
        if cached:
            return cached['data']
        raise
    data = {'players': tp['players'], 'aa': live['aa'], 'gear': live['gear'], 'frozen': False, 'frozen_at': None}
    _live_cache[season['id']] = {'at': now, 'data': data}
    return data


def warm_running_season():
    """Build the running season's boards into the cache (called at boot, after the gear calibration)."""
    season = lbr.resolve_season(None)
    if season and season.get('state') != 'closed':
        _live_cache.pop(season['id'], None)
        _board_cache.pop(season['id'], None)
        season_boards(season)  # calls board(), so both caches fill


def freeze(season, session=None):
    """Write the season's snapshot if it has none. Returns the number of rows written (0 if it existed)."""
    own = session is None and not db.in_request()
    session = session or db.session(lbr.engine)
    try:
        ensure_table(session)
        players, _ = _snapshot(session, season['id'])
        if players is not None:
            return _freeze_boards(season, session)
        players = _compute(season)
        now = datetime.utcnow()
        for p in players:
            session.execute(text(
                "INSERT IGNORE INTO lb_season_top_players (season_id, rank_position, character_id, payload, frozen_at) "
                "VALUES (:s, :r, :c, :p, :t)"),
                {'s': season['id'], 'r': p['rank'], 'c': p['id'], 'p': json.dumps(p, default=str), 't': now})
        session.commit()
        return len(players) + _freeze_boards(season, session)
    finally:
        if own:
            session.close()


def freeze_closed_seasons(verbose=True):
    """Called by the leaderboard sync: snapshot every closed season that has none yet."""
    n = 0
    for s in lbr.list_seasons():
        if s.get('state') == 'closed':
            wrote = freeze(s)
            if wrote and verbose:
                print("top players: froze %s (%d rows)" % (s['key'], wrote))
            n += wrote
    return n


LIVE_TTL = 60
_live_cache = {}   # season id -> {'at': time, 'data': {...}}  (season_boards: players + AA + gear)
_board_cache = {}  # season id -> {'at': time, 'data': {...}}  (board: Top Players only)


def board(season):
    """{'players': [...], 'frozen': bool, 'frozen_at': datetime|None, 'weights': {...}}"""
    w = SEASON_WEIGHTS.get(season['key'], SEASON_WEIGHTS['season-1'])
    if season.get('state') == 'upcoming':
        # nobody can have a parse or a run in a season that has not started: no table until it opens
        return {'players': [], 'upcoming': True, 'frozen': False, 'frozen_at': None, 'weights': w}
    if season.get('state') == 'closed':
        with lbr._session() as session:
            ensure_table(session)
            players, at = _snapshot(session, season['id'])
        if players is None:
            freeze(season)
            with lbr._session() as session:
                players, at = _snapshot(session, season['id'])
        return {'players': players or [], 'frozen': True, 'frozen_at': at, 'weights': w}
    # running season: scoring every character takes seconds -- cached per worker LIVE_TTL seconds
    # (the Season Standings page, /leaderboard/top-players, comes straight here)
    now = _time.time()
    cached = _board_cache.get(season['id'])
    if cached and now - cached['at'] < LIVE_TTL:
        return cached['data']
    try:
        players = _compute(season)
    except Exception:
        if cached:
            return cached['data']
        raise
    data = {'players': players, 'frozen': False, 'frozen_at': None, 'weights': w}
    _board_cache[season['id']] = {'at': now, 'data': data}
    return data
