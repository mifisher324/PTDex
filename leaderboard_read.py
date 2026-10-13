"""Leaderboard V2 read layer.

Everything the website renders is served from here.  These functions ONLY read
already-finalized data (ranking caches, parses, personal bests) -- they never
compute rankings live, matching the spec's performance rule.  Live computation
lives in ``leaderboard.py`` and runs at finalize time.
"""
from datetime import datetime
from decimal import Decimal

import db
from sqlalchemy import func
from sqlalchemy.orm import Session

from create_leaderboard_db import (engine, Boss, BossVersion, Season, Ruleset,
                                   ParseColor, Encounter, Participant,
                                   DamageSegment, Parse, PersonalBest,
                                   RankingCache, ValidationFlag)
import leaderboard as lb

GROUP_SIZES = (1, 2, 3)
CATEGORIES = (lb.CAT_SPEED, lb.CAT_INDIVIDUAL_DPS, lb.CAT_GROUP_DPS, lb.CAT_RECENT)
CATEGORY_LABELS = {
    lb.CAT_SPEED: 'Speed Rankings',
    lb.CAT_INDIVIDUAL_DPS: 'Individual DPS',
    lb.CAT_GROUP_DPS: 'Group DPS',
    lb.CAT_RECENT: 'Recent Valid Kills',
}


def _session():
    # the request's shared leaderboard connection (see db.py); its own short session outside a request
    return db.get_db(engine)


# --------------------------------------------------------------------------- #
# Seasons                                                                      #
# --------------------------------------------------------------------------- #
# Every public read is scoped to ONE season. The default is the season that is
# running right now, so the day Season 2 opens every board, the parse rankings,
# the player-score parse component and the character pages all switch to it with
# no deploy. Season 1 stays readable, frozen, via ?season=season-1.
def _utcnow():
    return datetime.utcnow()              # the DB clock is naive UTC


_season_cache = {'rows': None, 'ts': 0.0}
_SEASON_TTL = 60


def _season_rows():
    """[(id, key, name, started_at, ended_at, active)] newest first. Cached for a
    minute: every page asks, and the answer only changes on a season boundary --
    which the state calculation below handles live from the clock anyway."""
    import time
    now = time.time()
    if _season_cache['rows'] is not None and now - _season_cache['ts'] < _SEASON_TTL:
        return _season_cache['rows']
    with _session() as session:
        rows = [(s.id, s.season_key, s.name, s.started_at, s.ended_at, bool(s.active))
                for s in (session.query(Season)
                          .order_by(Season.started_at.desc(), Season.id.desc()).all())]
    _season_cache['rows'] = rows
    _season_cache['ts'] = now
    return rows


def _season_dict(row, now):
    sid, key, name, start, end, _active = row
    if start is not None and now < start:
        state = 'upcoming'
    elif end is not None and now >= end:
        state = 'closed'
    else:
        state = 'current'
    return {'id': sid, 'key': key, 'name': name,
            'started_at': start, 'ended_at': end, 'state': state,
            'start_short': _fmt_when(start)[0] if start else None,
            'end_short': _fmt_when(end)[0] if end else None}


def list_seasons():
    """All seasons, newest first, each tagged current / closed / upcoming."""
    now = _utcnow()
    return [_season_dict(r, now) for r in _season_rows()]


def resolve_season(season_key=None):
    """The season a page should show: the one named in ``season_key`` if it
    exists, else the one running now (same rule as create_leaderboard_db.season_at).
    Returns a season dict or None."""
    now = _utcnow()
    rows = _season_rows()
    if season_key:
        for r in rows:
            if r[1] == season_key:
                return _season_dict(r, now)
    for r in rows:
        start, end = r[3], r[4]
        if (start is None or now >= start) and (end is None or now < end):
            return _season_dict(r, now)
    for r in rows:                                   # undated installs
        if r[5]:
            return _season_dict(r, now)
    return None


def season_running_now_id():
    """The season running now BY DATE (not the site's default). For live pages such as Players."""
    now = _utcnow()
    for r in _season_rows():
        start, end = r[3], r[4]
        if (start is None or now >= start) and (end is None or now < end):
            return r[0]
    return None


def current_season_id():
    s = resolve_season()
    return s['id'] if s else None


def _raid_boss_ids(session):
    """Boss ids that count toward raid parses. Monster Missions are excluded: they
    are solo runs in a fixed monster form and have their own section."""
    return [b[0] for b in session.query(Boss.id).filter(Boss.kind != 'mission').all()]


# --------------------------------------------------------------------------- #
# Parse colors                                                                #
# --------------------------------------------------------------------------- #
def parse_color_bands(session):
    """All configured parse-color bands, ordered, as plain dicts (for templates)."""
    return [{'min': float(c.min_pct), 'max': float(c.max_pct),
             'label': c.label, 'color': c.color_hex}
            for c in session.query(ParseColor).order_by(ParseColor.sort_order).all()]


def _band_for(bands, value):
    """The band a parse score falls in. Bands are integer ranges (Gray 1-24 ...
    Legend 100) with gaps between them -- e.g. 98.5 sits between Orange's 98 and
    Pink's 99 -- so we pick the highest band whose floor the value has reached.
    That keeps fractional percentiles from falling through to a default color."""
    if value is None or not bands:
        return None
    v = float(value)
    reached = [b for b in bands if b['min'] <= v]
    return max(reached, key=lambda b: b['min']) if reached \
        else min(bands, key=lambda b: b['min'])


def color_for(bands, percentile):
    """Pick the hex color for a percentile from a preloaded ``bands`` list."""
    b = _band_for(bands, percentile)
    return b['color'] if b else '#888888'


def _decorate_parse(bands, value):
    """Return {'value': float, 'color': hex} for a percentile (or None)."""
    if value is None:
        return None
    return {'value': round(float(value), 2), 'color': color_for(bands, value)}


def get_parse_bands():
    """Parse-color bands (Gray..Legend) as plain dicts. Also used as trophy tiers."""
    with _session() as session:
        return parse_color_bands(session)


def trophy_for(bands, value):
    """Map a parse score (0-100) to its band -> {'label','color','value'}. The
    leaderboard's WarcraftLogs-style parse bands double as the player trophy tiers."""
    b = _band_for(bands, value)
    if b is None:
        return None
    return {'label': b['label'], 'color': b['color'], 'value': round(float(value), 1)}


def get_parse_scores(season_id=None):
    """{character_id: coverage-adjusted overall parse 0-100} for ONE season
    (default: the current one) over raid bosses only.

    A character's best parse on each BOSS (their peak across parse types and group
    sizes) is summed over every active boss -- bosses they haven't killed count as
    ZERO -- then divided by the number of active bosses (those with >=1 server kill,
    so a boss nobody can clear yet doesn't cap everyone). This rewards breadth: the
    character board means 'best overall player' while per-boss boards stay the venue
    for specialists. avg-over-killed would let one 98 kill outrank a ten-boss 82.
    Feeds the parse leaderboard, the player-score parse component, the card trophy,
    the homepage Parse column and the #1 crown. Carried 0-damage passengers (all
    null parses) contribute nothing and never appear."""
    if season_id is None:
        season_id = current_season_id()
    with _session() as session:
        rows = (session.query(PersonalBest.character_id, PersonalBest.boss_id,
                              PersonalBest.best_dps_parse,
                              PersonalBest.best_speed_parse,
                              PersonalBest.best_group_parse)
                .filter(PersonalBest.season_id == season_id,
                        PersonalBest.boss_id.in_(_raid_boss_ids(session))).all())
    if not rows:
        return {}
    total_bosses = len({bid for _cid, bid, _d, _s, _g in rows})
    if not total_bosses:
        return {}
    # peak parse per (character, boss) = best across parse types AND group sizes
    peak = {}
    for cid, bid, d, s, g in rows:
        cands = [float(v) for v in (d, s, g) if v is not None]
        if not cands:
            continue
        k = (cid, bid)
        best = max(cands)
        if k not in peak or best > peak[k]:
            peak[k] = best
    acc = {}
    for (cid, _bid), best in peak.items():
        acc[cid] = acc.get(cid, 0.0) + best        # unkilled bosses add 0
    return {cid: round(total / total_bosses, 2) for cid, total in acc.items()}


# --------------------------------------------------------------------------- #
# Race board (the leaderboard landing page)                                    #
# --------------------------------------------------------------------------- #
# Three columns per boss: who got there FIRST, who is FASTEST, who hits HARDEST.
# First clears are the point of the page -- speed and damage records change
# hands, a first never does.
#
# Deliberately NOT read from RankingCache. That cache is per (boss, group size)
# and holds the ranked lists the boss pages need; this page wants one row per
# category ACROSS group sizes plus the earliest clear ever, which the cache does
# not model. The whole board is two queries and a little Python instead.
GROUP_LABELS = {1: 'Solo', 2: 'Duo', 3: 'Trio'}

_board_cache = {}          # {season_id: {'data', 'ts'}}
_BOARD_TTL = 60


def get_race_board(force=False, season_id=None):
    """Everything the landing page draws, in two queries.

    Returns [{boss, clears, by_size, first[], fastest[], dps[]}] in boss sort
    order. `first` carries one row per group size (including sizes nobody has
    cleared, so an unclaimed first is visible rather than absent); `fastest` and
    `dps` carry the top three across all sizes.
    """
    import time
    now = time.time()
    if season_id is None:
        season_id = current_season_id()
    hit = _board_cache.get(season_id)
    if not force and hit is not None and (now - hit['ts']) < _BOARD_TTL:
        return hit['data']

    with _session() as session:
        bosses = (session.query(Boss).filter(Boss.active.is_(True),
                                             Boss.kind != 'mission')
                  .order_by(Boss.sort_order).all())

        # One pass over the encounters. Only the columns the board needs, so this
        # stays small even when the season is long.
        rows = (session.query(Encounter.id, Encounter.boss_id, Encounter.group_size,
                              Encounter.start_time, Encounter.duration_seconds,
                              Encounter.encounter_dps, Encounter.active_dps, Encounter.raw_payload)
                .filter(Encounter.validation_status == lb.STATUS_VALID,
                        Encounter.season_id == season_id)
                .all())

        # LDoN raids are timed over the whole run, so their whole-run DPS is meaningless:
        # they rank on the boss fight (active DPS), as the PoP bosses effectively do.
        timed_runs = {b.id for b in bosses if (b.boss_key or '').startswith('ldon_')}
        by_boss = {}
        for eid, bid, gs, start, dur, dps, adps, raw in rows:
            if bid in timed_runs:
                dps = adps
            peak = (raw or {}).get('peak_dps') if isinstance(raw, dict) else None
            by_boss.setdefault(bid, []).append({
                'peak_dps': int(peak) if peak else 0,
                'encounter_id': eid, 'group_size': gs,
                'group_label': GROUP_LABELS.get(gs, '%dp' % gs),
                'start_time': start,
                'duration_seconds': float(dur) if dur is not None else None,
                'encounter_dps': int(dps) if dps is not None else 0,
            })

        board, wanted = [], set()
        for b in bosses:
            mine = by_boss.get(b.id, [])
            by_size = {gs: [e for e in mine if e['group_size'] == gs] for gs in GROUP_SIZES}

            first = []
            for gs in GROUP_SIZES:
                pool = by_size[gs]
                if pool:
                    e = min(pool, key=lambda x: x['start_time'])
                    first.append(dict(e, claimed=True))
                else:
                    # An unclaimed first is the most motivating thing on the page.
                    first.append({'group_size': gs, 'group_label': GROUP_LABELS[gs],
                                  'claimed': False, 'encounter_id': None})

            # ONE ROW PER BRACKET, not a top-3 across brackets. A trio is almost
            # always faster than a solo, so ranking them together just hands all
            # three slots to trios and hides every solo and duo record on the
            # server. Solo, Duo and Trio are separate achievements and each keeps
            # its own line -- the same shape `first` already uses.
            fastest, dps = [], []
            for gs in GROUP_SIZES:
                pool = by_size[gs]
                timed = [e for e in pool if e['duration_seconds']]
                if timed:
                    fastest.append(dict(min(timed, key=lambda x: x['duration_seconds']),
                                        claimed=True))
                else:
                    fastest.append({'group_size': gs, 'group_label': GROUP_LABELS[gs],
                                    'claimed': False, 'encounter_id': None})

                hit = [e for e in pool if e['encounter_dps']]
                if hit:
                    dps.append(dict(max(hit, key=lambda x: x['encounter_dps']),
                                    claimed=True))
                else:
                    dps.append({'group_size': gs, 'group_label': GROUP_LABELS[gs],
                                'claimed': False, 'encounter_id': None,
                                'encounter_dps': 0})

            for e in first + fastest + dps:
                if e.get('encounter_id'):
                    wanted.add(e['encounter_id'])

            board.append({
                'boss_key': b.boss_key, 'name': b.name,
                'is_gauntlet': bool(b.is_gauntlet),
                'clears': len(mine),
                'by_size': {gs: len(by_size[gs]) for gs in GROUP_SIZES},
                'first': first, 'fastest': fastest, 'dps': dps,
            })

        # Second query: the rosters, only for the ~36 encounters actually shown.
        rosters = {}
        if wanted:
            prows = (session.query(Participant.encounter_id, Participant.character_id,
                                   Participant.character_name, Participant.boss_damage)
                     .filter(Participant.encounter_id.in_(wanted),
                             Participant.is_valid.is_(True))
                     .all())
            for enc_id, cid, name, dmg in prows:
                rosters.setdefault(enc_id, []).append(
                    {'id': cid, 'name': name, 'damage': int(dmg or 0)})
            for members in rosters.values():
                members.sort(key=lambda m: -m['damage'])   # top damage leads the name

    for entry in board:
        for group in ('first', 'fastest', 'dps'):
            for e in entry[group]:
                e['members'] = rosters.get(e.get('encounter_id'), [])
                e['names'] = ' + '.join(m['name'] for m in e['members'])
                e['duration_display'] = _fmt_clock(e.get('duration_seconds'))
                e['when_short'], e['when_full'] = _fmt_when(e.get('start_time'))

    # `.get` throughout: an unclaimed bracket row carries no metric at all.
    totals = {
        'clears': sum(e['clears'] for e in board),
        'firsts_claimed': sum(1 for e in board for f in e['first'] if f['claimed']),
        'firsts_open': sum(1 for e in board for f in e['first'] if not f['claimed']),
        'peak_dps': max([e.get('encounter_dps') or 0 for entry in board
                         for e in entry['dps']] or [0]),
        'fastest': min([e['duration_seconds'] for entry in board
                        for e in entry['fastest']
                        if e.get('duration_seconds')] or [0]),
    }
    totals['fastest_display'] = _fmt_clock(totals['fastest'])

    out = {'bosses': board, 'totals': totals}
    _board_cache[season_id] = {'data': out, 'ts': now}
    return out


def get_mission_ranks(character_id, season_id=None):
    """{theme_key: {'speed_rank','dps_rank','of'}} -- where this character's best
    run on each Monster Mission places this season, among every character with a
    completed run. Ranks are per character (one best each), the same way the
    mission board lists them. Missing missions are simply absent."""
    if season_id is None:
        season_id = current_season_id()
    with _session() as session:
        missions = {b.id: b.boss_key for b in session.query(Boss)
                    .filter(Boss.kind == 'mission').all()}
        if not missions:
            return {}
        pbs = (session.query(PersonalBest.character_id, PersonalBest.boss_id,
                             PersonalBest.fastest_kill_seconds, PersonalBest.best_raw_dps)
               .filter(PersonalBest.boss_id.in_(list(missions)),
                       PersonalBest.season_id == season_id,
                       PersonalBest.group_size == 1).all())
    by_boss = {}
    for cid, bid, fast, dps in pbs:
        by_boss.setdefault(bid, []).append((cid, fast, dps))
    out = {}
    for bid, rows in by_boss.items():
        mine = next((r for r in rows if r[0] == character_id), None)
        if mine is None:
            continue
        _cid, fast, dps = mine
        speed_rank = (1 + sum(1 for r in rows if r[1] is not None and fast is not None
                              and r[1] < fast)) if fast is not None else None
        dps_rank = (1 + sum(1 for r in rows if (r[2] or 0) > (dps or 0))) if dps else None
        out[missions[bid][3:]] = {'speed_rank': speed_rank, 'dps_rank': dps_rank,
                                  'of': len(rows)}
    return out


# --------------------------------------------------------------------------- #
# Monster Missions board                                                       #
# --------------------------------------------------------------------------- #
# Solo only and ranked PER CHARACTER: each character appears once per column,
# at their best run. Everyone is in the same monster form with the same stat
# block, so this is the board where gear cannot buy a place.
MISSION_TOP = 5
_mission_cache = {}


def get_mission_board(season_id=None, force=False):
    """[{boss_key, name, theme, clears, runners, first, fastest[], dps[]}] for one
    season, in camp order. One query, like the race board."""
    import time
    now = time.time()
    if season_id is None:
        season_id = current_season_id()
    hit = _mission_cache.get(season_id)
    if not force and hit is not None and (now - hit['ts']) < _BOARD_TTL:
        return hit['data']

    with _session() as session:
        missions = (session.query(Boss).filter(Boss.active.is_(True),
                                               Boss.kind == 'mission')
                    .order_by(Boss.sort_order).all())
        missions = [(m.id, m.boss_key, m.name) for m in missions]
        ids = [m[0] for m in missions]
        rows = []
        if ids:
            rows = (session.query(Encounter.id, Encounter.boss_id, Encounter.end_time,
                                  Encounter.duration_seconds,
                                  Participant.character_id, Participant.character_name,
                                  Participant.class_name, Participant.active_dps)
                    .join(Participant, Participant.encounter_id == Encounter.id)
                    .filter(Encounter.boss_id.in_(ids),
                            Encounter.season_id == season_id,
                            Encounter.validation_status == lb.STATUS_VALID,
                            Participant.is_valid.is_(True))
                    .all())

    by_boss = {}
    for eid, bid, end, dur, cid, name, cls, adps in rows:
        by_boss.setdefault(bid, []).append({
            'encounter_id': eid, 'character_id': cid, 'name': name, 'class_name': cls,
            'end_time': end,
            'duration_seconds': float(dur) if dur is not None else None,
            'dps': int(adps or 0)})

    def best_per_char(runs, key, lower_is_better):
        best = {}
        for r in runs:
            v = r[key]
            if not v:
                continue
            cur = best.get(r['character_id'])
            if cur is None or (v < cur[key] if lower_is_better else v > cur[key]):
                best[r['character_id']] = r
        out = sorted(best.values(), key=lambda r: r[key], reverse=not lower_is_better)
        return out[:MISSION_TOP]

    board = []
    for mid, key, name in missions:
        runs = by_boss.get(mid, [])
        first = min(runs, key=lambda r: r['end_time']) if runs else None
        fastest = best_per_char(runs, 'duration_seconds', True)
        dps = best_per_char(runs, 'dps', False)
        for r in ([first] if first else []) + fastest + dps:
            r['duration_display'] = _fmt_clock(r.get('duration_seconds'))
            r['when_short'], r['when_full'] = _fmt_when(r.get('end_time'))
        board.append({'boss_key': key, 'name': name,
                      'theme': name.split(': ', 1)[-1],
                      'clears': len(runs),
                      'runners': len({r['character_id'] for r in runs}),
                      'first': first, 'fastest': fastest, 'dps': dps})
    _mission_cache[season_id] = {'data': board, 'ts': now}
    return board


_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
           'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def to_pacific(dt):
    """A naive UTC time (the game database clock) as US Pacific time, for display. The US
    daylight rule by hand (2nd Sunday of March 02:00 to 1st Sunday of November 02:00 local):
    the slim image has no tz database."""
    if not dt:
        return dt
    from datetime import timedelta

    def nth_sunday(year, month, n):
        d = datetime(year, month, 1)
        d += timedelta(days=(6 - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    y = dt.year
    dst_start = nth_sunday(y, 3, 2) + timedelta(hours=10)   # 02:00 PST = 10:00 UTC
    dst_end = nth_sunday(y, 11, 1) + timedelta(hours=9)     # 02:00 PDT = 09:00 UTC
    return dt - timedelta(hours=7 if dst_start <= dt < dst_end else 8)


def _fmt_when(dt):
    """('9 Jul', '9 Jul 01:01'). Built by hand rather than with strftime because
    the no-pad day is a PLATFORM SPLIT: '%-d' is glibc-only and raises
    ValueError('Invalid format string') on Windows, where it must be spelled
    '%#d'. A template that renders on Linux and crashes in Windows dev is a trap;
    this has no such edge."""
    if not dt:
        return None, None
    dt = to_pacific(dt)
    short = '%d %s' % (dt.day, _MONTHS[dt.month - 1])
    return short, '%s %02d:%02d' % (short, dt.hour, dt.minute)


def _fmt_clock(seconds):
    """m:ss -- the form a speed record is read in. _fmt_duration's '13m 32s' is
    right for prose but too wide for a ranked column."""
    if not seconds:
        return None
    seconds = int(float(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return '%d:%02d:%02d' % (h, m, s) if h else '%d:%02d' % (m, s)


# --------------------------------------------------------------------------- #
# Bosses / navigation                                                         #
# --------------------------------------------------------------------------- #
# LDoN (raids, speedruns) and Monster Missions launch with Season 2 and exist only there: their
# boards are hidden in every other season and their runs are always filed under Season 2.
LDON_SEASON_KEY = 'season-2'


def is_ldon_board(boss_key):
    return bool(boss_key) and (boss_key.startswith('ldon_') or boss_key.startswith('mm_'))


def season_has_ldon(season):
    """True when this season shows the LDoN / Monster Missions boards."""
    return bool(season) and season.get('key') == LDON_SEASON_KEY


def list_bosses(session=None):
    own = session is None and not db.in_request()
    session = session or db.session(engine)
    try:
        rows = (session.query(Boss).filter(Boss.active.is_(True))
                .order_by(Boss.sort_order).all())
        return [{'boss_key': b.boss_key, 'name': b.name,
                 'is_gauntlet': bool(b.is_gauntlet),
                 'kind': b.kind or 'raid'} for b in rows]
    finally:
        if own:
            session.close()


def get_boss(session, boss_key):
    return session.query(Boss).filter_by(boss_key=boss_key).first()


def _active_scope_ids(session):
    season = session.query(Season).filter_by(active=True).order_by(Season.id.desc()).first()
    ruleset = session.query(Ruleset).filter_by(active=True).order_by(Ruleset.id.desc()).first()
    return (season.id if season else None), (ruleset.id if ruleset else None)


# --------------------------------------------------------------------------- #
# Boss rankings page                                                          #
# --------------------------------------------------------------------------- #
def get_boss_rankings(boss_key, group_size, season_id=None, ruleset_id=None, limit=100):
    """Return all four ranking categories for a boss + group size, read straight
    from the ranking cache, decorated with parse colors."""
    with _session() as session:
        boss = get_boss(session, boss_key)
        if not boss:
            return None
        if season_id is None or ruleset_id is None:
            # Fill ONLY the missing one. The boss route passes an explicit season_id (from ?season=)
            # but no ruleset_id; recomputing both here clobbered the requested season with the newest
            # active one (Season 2), so every board showed empty. Fill each independently.
            d_season, d_ruleset = _active_scope_ids(session)
            if season_id is None:
                season_id = d_season
            if ruleset_id is None:
                ruleset_id = d_ruleset
        bands = parse_color_bands(session)

        # A representative tier/comparison count for this scope (from any parse).
        tier_row = (session.query(Parse.tier, Parse.comparison_count)
                    .filter(Parse.boss_id == boss.id, Parse.group_size == group_size,
                            Parse.season_id == season_id, Parse.ruleset_id == ruleset_id,
                            Parse.parse_type == 'speed')
                    .first())
        tier = tier_row[0] if tier_row else lb.TIER_NOT_ENOUGH
        comparison_count = tier_row[1] if tier_row else 0

        sections = {}
        for category in CATEGORIES:
            rows = (session.query(RankingCache)
                    .filter(RankingCache.boss_id == boss.id,
                            RankingCache.group_size == group_size,
                            RankingCache.season_id == season_id,
                            RankingCache.ruleset_id == ruleset_id,
                            RankingCache.category == category)
                    .order_by(RankingCache.rank_position)
                    .limit(limit).all())
            sections[category] = [_ranking_row(bands, r) for r in rows]

        return {
            'boss': {'boss_key': boss.boss_key, 'name': boss.name,
                     'is_gauntlet': bool(boss.is_gauntlet),
                     'kind': boss.kind or 'raid'},
            'group_size': group_size,
            'group_label': lb.group_size_label(group_size),
            'tier': tier, 'comparison_count': comparison_count,
            'sections': sections,
            'category_labels': CATEGORY_LABELS,
            'parse_bands': bands,
        }


def _ranking_row(bands, rc):
    """Flatten a RankingCache row (+ its JSON payload) into a template-friendly
    dict with parse colors applied."""
    p = rc.payload or {}
    row = {'rank': rc.rank_position, 'encounter_id': rc.encounter_id,
           'participant_id': rc.participant_id,
           'metric_value': float(rc.metric_value) if rc.metric_value is not None else None,
           'members': p.get('members', []),
           'end_time': p.get('end_time'), 'raw': p}
    # Decorate any parse percentiles present in the payload.
    for key in ('speed_parse', 'group_parse', 'dps_parse'):
        if key in p:
            row[key] = _decorate_parse(bands, p[key])
    # Convenience fields per category.
    for key in ('duration_seconds', 'active_dps', 'combined_dps', 'combined_damage',
                'boss_damage', 'encounter_dps', 'character_name', 'class_name',
                'gear_score', 'deaths', 'group_size'):
        if key in p:
            row[key] = p[key]
    row['duration_display'] = _fmt_duration(p.get('duration_seconds'))
    return row


def _fmt_duration(seconds):
    if seconds is None:
        return None
    seconds = int(float(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f'{h}h {m:02d}m {s:02d}s'
    if m:
        return f'{m}m {s:02d}s'
    return f'{s}s'


# --------------------------------------------------------------------------- #
# Encounter detail page                                                       #
# --------------------------------------------------------------------------- #
def get_encounter_detail(encounter_id):
    with _session() as session:
        e = session.get(Encounter, encounter_id)
        if not e:
            return None
        bands = parse_color_bands(session)
        boss = session.get(Boss, e.boss_id)
        version = session.get(BossVersion, e.boss_version_id)
        season = session.get(Season, e.season_id)
        ruleset = session.get(Ruleset, e.ruleset_id)

        participants = (session.query(Participant)
                        .filter(Participant.encounter_id == e.id).all())
        segments = (session.query(DamageSegment)
                    .filter(DamageSegment.encounter_id == e.id,
                            DamageSegment.participant_id.is_(None))
                    .order_by(DamageSegment.sequence).all())
        parses = session.query(Parse).filter(Parse.encounter_id == e.id).all()
        flags = (session.query(ValidationFlag)
                 .filter(ValidationFlag.encounter_id == e.id).all())

        ind_by_part = {pr.participant_id: pr for pr in parses if pr.parse_type == 'individual_dps'}
        group_parse = next((pr for pr in parses if pr.parse_type == 'group_dps'), None)
        speed_parse = next((pr for pr in parses if pr.parse_type == 'speed'), None)

        return {
            'id': e.id,
            'boss': {'boss_key': boss.boss_key, 'name': boss.name,
                     'is_gauntlet': bool(boss.is_gauntlet)} if boss else None,
            'boss_version': version.label if version else None,
            'season': season.name if season else None,
            'ruleset': ruleset.name if ruleset else None,
            'group_size': e.group_size,
            'group_label': lb.group_size_label(e.group_size),
            'validation_status': e.validation_status,
            'zone_id': e.zone_id, 'instance_id': e.instance_id, 'spawn_id': e.spawn_id,
            'start_time': e.start_time.isoformat() if e.start_time else None,
            'end_time': e.end_time.isoformat() if e.end_time else None,
            'duration_seconds': float(e.duration_seconds) if e.duration_seconds is not None else None,
            'duration_display': _fmt_duration(e.duration_seconds),
            'combat_time_seconds': float(e.combat_time_seconds) if e.combat_time_seconds is not None else None,
            'total_boss_damage': int(e.total_boss_damage or 0),
            'total_pet_damage': int(e.total_pet_damage or 0),
            'encounter_dps': int(e.encounter_dps or 0),
            'active_dps': int(e.active_dps or 0),
            'deaths': e.deaths,
            'speed_parse': _decorate_parse(bands, speed_parse.percentile) if speed_parse else None,
            'group_parse': _decorate_parse(bands, group_parse.percentile) if group_parse else None,
            'participants': [{
                'character_id': p.character_id, 'character_name': p.character_name,
                'class_name': p.class_name, 'gear_score': p.gear_score,
                'boss_damage': int(p.boss_damage or 0), 'pet_damage': int(p.pet_damage or 0),
                'encounter_dps': int(p.encounter_dps or 0), 'active_dps': int(p.active_dps or 0),
                'deaths': p.deaths, 'is_valid': bool(p.is_valid),
                'dps_parse': _decorate_parse(bands, ind_by_part[p.id].percentile)
                             if p.id in ind_by_part else None,
            } for p in participants],
            'segments': [{
                'segment_name': s.segment_name, 'sequence': s.sequence,
                'duration_seconds': float(s.duration_seconds) if s.duration_seconds is not None else None,
                'damage': int(s.damage or 0), 'dps': int(s.dps or 0),
            } for s in segments],
            'flags': [{'code': f.flag_code, 'severity': f.severity,
                       'auto': bool(f.auto), 'details': f.details} for f in flags],
        }


# --------------------------------------------------------------------------- #
# Character profile                                                           #
# --------------------------------------------------------------------------- #
BUILD_MIN_SAMPLE = 10   # a build bucket needs >= this many comparable kills to show


def get_build_parses(character_id, season_id=None):
    """Per-boss 'vs your build' Individual-DPS percentile: how this character ranks
    on each boss against everyone running the SAME class composition (GestaltClasses
    bitmask), within the same group size. Only buckets with >= BUILD_MIN_SAMPLE
    comparable kills are returned, so a fragmented build never shows a fake number --
    the all-comers parse stays the primary figure the rest of the time.
    -> {boss_key: {'percentile', 'color', 'count', 'group_size', 'build'}}"""
    if season_id is None:
        season_id = current_season_id()
    with _session() as session:
        bands = parse_color_bands(session)
        mine = (session.query(Participant.active_dps, Participant.class_bitmask,
                              Participant.class_name,
                              Encounter.boss_id, Encounter.group_size)
                .join(Encounter, Encounter.id == Participant.encounter_id)
                .filter(Participant.character_id == character_id,
                        Participant.is_valid.is_(True),
                        Participant.active_dps > 0,
                        Encounter.season_id == season_id,
                        Encounter.validation_status == lb.STATUS_VALID).all())
        if not mine:
            return {}
        bitmask = next((bm for _d, bm, _c, _b, _g in mine if bm), None)
        if not bitmask:
            return {}                       # older data with no stored bitmask
        build_name = next((c for _d, _bm, c, _b, _g in mine if c), None)
        bosses = {b.id: b for b in session.query(Boss).all()}
        # best of my active_dps per (boss, group_size) -- DPS is only comparable
        # within the same group size, so that's part of the scope.
        my_best = {}
        for dps, _bm, _c, bid, gs in mine:
            k = (bid, gs)
            my_best[k] = max(my_best.get(k, 0), int(dps or 0))
        out = {}
        for (bid, gs), best in my_best.items():
            pop = [int(r[0]) for r in (session.query(Participant.active_dps)
                   .join(Encounter, Encounter.id == Participant.encounter_id)
                   .filter(Encounter.boss_id == bid, Encounter.group_size == gs,
                           Encounter.season_id == season_id,
                           Encounter.validation_status == lb.STATUS_VALID,
                           Participant.is_valid.is_(True),
                           Participant.active_dps > 0,
                           Participant.class_bitmask == bitmask).all())]
            if len(pop) < BUILD_MIN_SAMPLE:
                continue                     # not statistically real yet -> hide
            pct = float(lb.percentile_rank(Decimal(best), [Decimal(x) for x in pop],
                                           higher_is_better=True))
            b = bosses.get(bid)
            key = b.boss_key if b else str(bid)
            cur = out.get(key)
            if cur is None or pct > cur['percentile']:
                out[key] = {'percentile': round(pct, 1), 'color': color_for(bands, pct),
                            'count': len(pop), 'group_size': gs, 'build': build_name}
        return out


def get_character_profile(character_id, season_id=None):
    """One season of a character's leaderboard record (default: the current
    season). None only when the character has no record in ANY season -- a
    Season 1 player keeps their page, with an empty current season, on the day
    Season 2 opens."""
    if season_id is None:
        season_id = current_season_id()
    with _session() as session:
        all_pbs = (session.query(PersonalBest)
                   .filter(PersonalBest.character_id == character_id).all())
        if not all_pbs:
            return None
        pbs = [pb for pb in all_pbs if pb.season_id == season_id]
        bands = parse_color_bands(session)
        bosses = {b.id: b for b in session.query(Boss).all()}
        name = all_pbs[0].character_name

        # Per-boss best summary (aggregate across group sizes) + fastest kills.
        per_boss = {}
        all_parses = []
        total_clears = 0
        for pb in pbs:
            total_clears += pb.total_valid_kills or 0
            b = bosses.get(pb.boss_id)
            bkey = b.boss_key if b else str(pb.boss_id)
            slot = per_boss.setdefault(bkey, {
                'boss_key': bkey, 'name': b.name if b else bkey,
                'kind': (b.kind if b else None) or 'raid',
                'best_dps_parse': None, 'best_speed_parse': None,
                'best_group_parse': None, 'best_raw_dps': 0,
                'fastest_seconds': None, 'clears': 0})
            slot['clears'] += pb.total_valid_kills or 0
            slot['best_dps_parse'] = _max(slot['best_dps_parse'], pb.best_dps_parse)
            slot['best_speed_parse'] = _max(slot['best_speed_parse'], pb.best_speed_parse)
            slot['best_group_parse'] = _max(slot['best_group_parse'], pb.best_group_parse)
            slot['best_raw_dps'] = max(slot['best_raw_dps'], int(pb.best_raw_dps or 0))
            slot['fastest_seconds'] = _min(slot['fastest_seconds'], pb.fastest_kill_seconds)
            for v in (pb.best_dps_parse, pb.best_speed_parse, pb.best_group_parse):
                if v is not None:
                    all_parses.append(float(v))

        # Decorate per-boss slots for display. 'vs your build' is optional -- never
        # let it (e.g. a pre-migration DB missing class_bitmask) break the profile.
        try:
            build_parses = get_build_parses(character_id, season_id)
        except Exception:
            build_parses = {}
        for slot in per_boss.values():
            slot['build_parse'] = build_parses.get(slot['boss_key'])
            slot['best_dps_parse_d'] = _decorate_parse(bands, slot['best_dps_parse'])
            slot['best_speed_parse_d'] = _decorate_parse(bands, slot['best_speed_parse'])
            slot['best_group_parse_d'] = _decorate_parse(bands, slot['best_group_parse'])
            slot['fastest_display'] = _fmt_duration(slot['fastest_seconds'])
            _cand = [v for v in (slot['best_dps_parse'], slot['best_speed_parse'],
                                 slot['best_group_parse']) if v is not None]
            slot['best_any'] = max(_cand) if _cand else None
            slot['best_any_d'] = _decorate_parse(bands, slot['best_any'])

        overall_avg = get_parse_scores(season_id).get(character_id)   # coverage-adjusted (breadth-aware, same as leaderboard/score)
        best_parse = max(all_parses) if all_parses else None

        # Recent valid kills this character appears in.
        recent = _character_recent_kills(session, character_id, bands, limit=15,
                                         season_id=season_id)

        return {
            'character_id': character_id, 'character_name': name,
            'overall_parse_avg': _decorate_parse(bands, overall_avg) if overall_avg is not None else None,
            'best_parse': _decorate_parse(bands, best_parse) if best_parse is not None else None,
            'parse_score': overall_avg,
            'trophy': trophy_for(bands, overall_avg) if overall_avg is not None else None,
            'total_clears': total_clears,
            'per_boss': sorted([v for v in per_boss.values() if v['kind'] != 'mission'],
                               key=lambda x: x['name']),
            'missions': sorted([v for v in per_boss.values() if v['kind'] == 'mission'],
                               key=lambda x: x['name']),
            'seasons_played': sorted({pb.season_id for pb in all_pbs}),
            'recent_kills': recent,
        }


def _character_recent_kills(session, character_id, bands, limit=15, season_id=None):
    enc_ids = [r[0] for r in (session.query(Participant.encounter_id)
                              .filter(Participant.character_id == character_id).all())]
    if not enc_ids:
        return []
    encs = (session.query(Encounter)
            .filter(Encounter.id.in_(enc_ids),
                    Encounter.validation_status == lb.STATUS_VALID,
                    *([Encounter.season_id == season_id] if season_id else []))
            .order_by(Encounter.end_time.desc()).limit(limit).all())
    bosses = {b.id: b for b in session.query(Boss).all()}
    # Co-clearers for each shown kill, so the profile can link every group member.
    members_by_enc = {}
    show_ids = [e.id for e in encs]
    if show_ids:
        for p in (session.query(Participant)
                  .filter(Participant.encounter_id.in_(show_ids)).all()):
            members_by_enc.setdefault(p.encounter_id, []).append(
                {'character_id': p.character_id, 'character_name': p.character_name,
                 'class_name': p.class_name})
    out = []
    for e in encs:
        b = bosses.get(e.boss_id)
        out.append({'encounter_id': e.id,
                    'boss_key': b.boss_key if b else None,
                    'boss_name': b.name if b else None,
                    'group_label': lb.group_size_label(e.group_size),
                    'duration_display': _fmt_duration(e.duration_seconds),
                    'active_dps': int(e.active_dps or 0),
                    'members': members_by_enc.get(e.id, []),
                    'end_time': e.end_time.isoformat() if e.end_time else None})
    return out


# --------------------------------------------------------------------------- #
# Character rankings (leaderboard of players)                                 #
# --------------------------------------------------------------------------- #
def get_character_rankings(limit=100, season_id=None):
    """Rank characters by overall average parse -- the mean of their best DPS,
    Speed and Group parses across all bosses/group sizes. This is the SAME score
    used for the player-score parse component, the homepage Parse column, the card
    trophy, and the #1-parser crown, so every parse surface agrees on who's best."""
    if season_id is None:
        season_id = current_season_id()
    scores = get_parse_scores(season_id)   # {character_id: overall avg parse 0-100}
    with _session() as session:
        bands = parse_color_bands(session)
        raid_ids = _raid_boss_ids(session)
        rows = (session.query(
                    PersonalBest.character_id,
                    func.max(PersonalBest.character_name),
                    func.max(PersonalBest.best_dps_parse),
                    func.max(PersonalBest.best_speed_parse),
                    func.max(PersonalBest.best_group_parse),
                    func.sum(PersonalBest.total_valid_kills))
                .filter(PersonalBest.season_id == season_id,
                        PersonalBest.boss_id.in_(raid_ids))
                .group_by(PersonalBest.character_id).all())
    out = []
    for cid, name, bd, bs, bg, kills in rows:
        avg = scores.get(cid)
        best_cands = [float(v) for v in (bd, bs, bg) if v is not None]
        best = max(best_cands) if best_cands else None
        out.append({'character_id': cid, 'character_name': name,
                    'avg_parse': _decorate_parse(bands, avg) if avg is not None else None,
                    'best_parse': _decorate_parse(bands, best) if best is not None else None,
                    'trophy': trophy_for(bands, avg) if avg is not None else None,
                    'total_clears': int(kills or 0),
                    '_sort': avg if avg is not None else -1})
    out.sort(key=lambda x: x['_sort'], reverse=True)
    for i, r in enumerate(out[:limit], start=1):
        r['rank'] = i
        r.pop('_sort', None)
    return out[:limit]


def get_roster_raid_records(character_ids, season_id=None):
    """Per-boss best across a set of characters (a Discord user's roster): best
    overall parse + trophy, fastest kill, clears, and who holds each -- for the
    dashboard 'Raid Records' trophy case."""
    ids = [int(c) for c in character_ids if c]
    if not ids:
        return []
    if season_id is None:
        season_id = current_season_id()
    with _session() as session:
        bands = parse_color_bands(session)
        bosses = {b.id: b for b in session.query(Boss).all()
                  if (b.kind or 'raid') != 'mission'}
        pbs = (session.query(PersonalBest)
               .filter(PersonalBest.character_id.in_(ids),
                       PersonalBest.season_id == season_id,
                       PersonalBest.boss_id.in_(list(bosses))).all())
    per_boss = {}
    for pb in pbs:
        b = bosses.get(pb.boss_id)
        rec = per_boss.setdefault(pb.boss_id, {
            'boss_name': b.name if b else str(pb.boss_id),
            'boss_key': b.boss_key if b else None,
            'is_gauntlet': bool(b.is_gauntlet) if b else False,
            '_sort': b.sort_order if b else 999,
            'best_parse': None, 'holder_name': None, 'holder_id': None,
            'fastest_seconds': None, 'fastest_holder': None, 'clears': 0})
        rec['clears'] += pb.total_valid_kills or 0
        cand = [float(v) for v in (pb.best_dps_parse, pb.best_speed_parse,
                                   pb.best_group_parse) if v is not None]
        best = max(cand) if cand else None
        if best is not None and (rec['best_parse'] is None or best > rec['best_parse']):
            rec['best_parse'] = best
            rec['holder_name'] = pb.character_name
            rec['holder_id'] = pb.character_id
        if pb.fastest_kill_seconds is not None and (
                rec['fastest_seconds'] is None or pb.fastest_kill_seconds < rec['fastest_seconds']):
            rec['fastest_seconds'] = pb.fastest_kill_seconds
            rec['fastest_holder'] = pb.character_name
    out = []
    for rec in sorted(per_boss.values(), key=lambda r: r['_sort']):
        rec['trophy'] = trophy_for(bands, rec['best_parse']) if rec['best_parse'] is not None else None
        rec['best_parse_d'] = _decorate_parse(bands, rec['best_parse']) if rec['best_parse'] is not None else None
        rec['fastest_display'] = _fmt_duration(rec['fastest_seconds']) if rec['fastest_seconds'] is not None else None
        rec.pop('_sort', None)
        out.append(rec)
    return out


def _max(a, b):
    if b is None:
        return a
    if a is None:
        return float(b)
    return max(float(a), float(b))


def _min(a, b):
    if b is None:
        return a
    if a is None:
        return float(b)
    return min(float(a), float(b))
