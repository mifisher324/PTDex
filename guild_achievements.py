"""Guild achievements -- tiered badges earned by a guild's whole roster.

TWO KINDS, and the difference decides what has to be built:

  PERMANENT   Read live from tables that never prune -- level, AA, flags,
              discoveries, lifetime kill counts. These work retroactively: turn
              them on and every guild's history is already there.

  ACCUMULATED Boss kills. `player_event_logs` prunes kill events after SEVEN
              DAYS, so a cumulative counter can never be rebuilt after the fact.
              accumulate() walks new events into lb_guild_boss_kill and moves a
              cursor, so counts survive the prune. Every day this does not run
              is a day of history that cannot be recovered.

WHY THE TIERS LOOK SMALL
    Checked against the real server before being written: 576 visible characters,
    74 at level 65, and the largest guild fields 21 of them. "50 max-level
    members" would be two thirds of every level-65 in the game and unreachable by
    anyone. Kill tiers are set off measured rates -- Kerafyrm ~175/week
    server-wide, Zebuxoruk ~12 -- so Zeb's ladder is deliberately an order of
    magnitude shorter than Kerafyrm's.

COUNTING A GUILD KILL
    One kill emits one event PER PARTICIPANT. Three guildmates killing together
    is ONE kill for the guild, not three, so events are collapsed on
    (created_at, npc_id, instance_id) before crediting each guild once.
"""
from db import connection, get_db
import logic
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from create_leaderboard_db import engine as lb_engine

KILL_EVENTS = ('Killed Raid NPC', 'Killed Named NPC')

# Boss npc ids -> a stable key. Kept separate from any ingest
# extension on purpose: that is about the Solo/Duo/Trio boards, this is
# about guild totals at ANY group size.
TRACKED_BOSSES = {
    'kerafyrm': (128089,),
    'seru': (159691,),
    'zebuxoruk': (223212, 223238, 459000, 460000, 461000, 462000, 463000,
                  464000, 465000, 1120001288),
    'quarm': (223201,),
}

TIER_NAMES = ('I', 'II', 'III', 'IV', 'V', 'VI')

# Points per tier are TRIANGULAR: tier I is worth 1, II is worth 2 more, III 3
# more, and so on. Holding tier IV is therefore worth 10 points, not 4 -- pushing
# one achievement deep beats collecting four shallow ones, which is the right
# incentive for a guild board (depth, not breadth).
def tier_points(level):
    return sum(range(1, level + 1))

# ---------------------------------------------------------------------------
# Definitions. `stat` is the key produced by _gather(); thresholds ascend.
# ---------------------------------------------------------------------------
ACHIEVEMENTS = [
    # ---- permanent, retroactive -------------------------------------------
    {'key': 'maxlevel', 'name': 'The Ascended', 'icon': '&#9650;',
     'desc': 'Members who have reached level 65.',
     'stat': 'capped', 'tiers': (5, 10, 25, 50), 'unit': 'at 65'},

    {'key': 'active', 'name': 'Ever Present', 'icon': '&#9679;',
     'desc': 'Members seen in the last 14 days.',
     'stat': 'active', 'tiers': (5, 10, 25, 50), 'unit': 'active'},

    {'key': 'discover', 'name': 'Pathfinders', 'icon': '&#10022;',
     'desc': 'Items discovered first by a member -- permanent, and unfakeable.',
     'stat': 'discoveries', 'tiers': (100, 500, 1000, 2500, 5000), 'unit': 'finds'},

    {'key': 'slayer', 'name': 'Slayers of Norrath', 'icon': '&#9876;',
     'desc': 'Lifetime creatures felled across every member account.',
     'stat': 'creature_kills', 'tiers': (100000, 500000, 1000000, 2500000, 5000000),
     'unit': 'kills'},

    {'key': 'aa', 'name': 'Enlightened', 'icon': '&#10023;',
     'desc': 'Alternate advancement earned across the roster.',
     'stat': 'total_aa', 'tiers': (10000, 25000, 50000, 100000), 'unit': 'AA'},

    {'key': 'planar', 'name': 'Planar Vanguard', 'icon': '&#9873;',
     'desc': 'Accounts that have opened the Planes of Power.',
     'stat': 'pop_unlocked', 'tiers': (1, 5, 10, 25), 'unit': 'accounts'},

    {'key': 'timeflag', 'name': 'Masters of Time', 'icon': '&#9733;',
     'desc': 'Accounts holding the complete Plane of Time flag.',
     'stat': 'time_complete', 'tiers': (1, 5, 10, 25), 'unit': 'accounts'},

    # ---- accumulated boss kills -------------------------------------------
    {'key': 'kill_kerafyrm', 'name': 'Wakers of the Sleeper', 'icon': '&#128009;',
     'desc': 'Kerafyrm slain. The server kills him about 175 times a week.',
     'stat': 'kills_kerafyrm', 'tiers': (25, 50, 100, 250, 500, 1000), 'unit': 'kills',
     'accumulated': True},

    {'key': 'kill_seru', 'name': 'Inquisition', 'icon': '&#9784;',
     'desc': 'Lord Inquisitor Seru slain.',
     'stat': 'kills_seru', 'tiers': (10, 25, 50, 100, 250), 'unit': 'kills',
     'accumulated': True},

    {'key': 'kill_quarm', 'name': 'Beyond Time', 'icon': '&#8987;',
     'desc': 'Quarm slain at the end of the Plane of Time gauntlet.',
     'stat': 'kills_quarm', 'tiers': (10, 25, 50, 100, 250), 'unit': 'kills',
     'accumulated': True},

    {'key': 'kill_zebuxoruk', 'name': 'The Caged God', 'icon': '&#9889;',
     'desc': 'Zebuxoruk slain -- roughly a dozen falls server-wide each week, '
             'so the first is worth as much as a hundred Kerafyrms.',
     'stat': 'kills_zebuxoruk', 'tiers': (1, 5, 10, 25, 50, 100), 'unit': 'kills',
     'accumulated': True},
]


# Every tier of every achievement, held at once. Used to express a guild's score
# as a share rather than a bare number, and to feed guild standing.
MAX_POINTS = sum(tier_points(len(a['tiers'])) for a in ACHIEVEMENTS)


# --------------------------------------------------------------------------- #
# Accumulator                                                                  #
# --------------------------------------------------------------------------- #
_tables_ready = False


def ensure_tables():
    """Kill counters live in the leaderboard DB (the site's own MySQL). The game
    DB is never written to.

    Runs its DDL ONCE per process. It used to fire on every _kill_counts() and
    every char_kill_counts(), which put three CREATE TABLE IF NOT EXISTS round
    trips on the front of nine separate calls per guild page view -- pure latency
    against a remote database for tables that already existed.
    """
    global _tables_ready
    if _tables_ready:
        return
    with lb_engine.begin() as c:
        c.execute(text(
            "CREATE TABLE IF NOT EXISTS lb_guild_boss_kill ("
            " guild_id INT NOT NULL,"
            " boss_key VARCHAR(32) NOT NULL,"
            " kills INT NOT NULL DEFAULT 0,"
            " first_kill DATETIME NULL,"
            " last_kill DATETIME NULL,"
            " PRIMARY KEY (guild_id, boss_key))"))
        c.execute(text(
            "CREATE TABLE IF NOT EXISTS lb_guild_kill_cursor ("
            " id INT PRIMARY KEY,"
            " last_event_id BIGINT NOT NULL DEFAULT 0,"
            " updated_at DATETIME NULL)"))
        # Per-CHARACTER counts, for the guild contributor board. Tallied in the
        # same pass as the guild totals -- the events are already in hand, and a
        # second scan could never be run against pruned data anyway.
        c.execute(text(
            "CREATE TABLE IF NOT EXISTS lb_char_boss_kill ("
            " char_id INT NOT NULL,"
            " boss_key VARCHAR(32) NOT NULL,"
            " kills INT NOT NULL DEFAULT 0,"
            " last_kill DATETIME NULL,"
            " PRIMARY KEY (char_id, boss_key))"))
        # ALL raid kills, per guild per DAY -- not just the four tracked bosses.
        # This exists purely so the guild pages stop deriving a week of history
        # from raw events on every load: that meant a COUNT(DISTINCT CONCAT(...))
        # over ~435k rows building a 181k-value distinct set, which spilled to an
        # on-disk temp table and cost ~13.5s. Reading 26 guilds x 7 days instead
        # is ~180 rows. Per-DAY rather than a running total so the displayed
        # figure can stay a rolling 7-day window.
        c.execute(text(
            "CREATE TABLE IF NOT EXISTS lb_guild_raid_day ("
            " guild_id INT NOT NULL,"
            " day DATE NOT NULL,"
            " kills INT NOT NULL DEFAULT 0,"
            " PRIMARY KEY (guild_id, day))"))
    _tables_ready = True


# Two independent cursors share lb_guild_kill_cursor by row id: 1 is the boss
# tally, 2 is the raid-day tally. Separate because the raid-day tally is seeded
# later than the boss tally and must not inherit its position -- everything the
# boss cursor has already passed would otherwise never be counted.
BOSS_CURSOR = 1
RAID_DAY_CURSOR = 2


def _cursor(row_id=BOSS_CURSOR):
    with connection(lb_engine) as c:
        r = c.execute(text("SELECT last_event_id FROM lb_guild_kill_cursor WHERE id = :i"),
                      {'i': int(row_id)}).first()
    return int(r[0]) if r else 0


def _set_cursor(v, row_id=BOSS_CURSOR):
    with lb_engine.begin() as c:
        c.execute(text(
            "INSERT INTO lb_guild_kill_cursor (id, last_event_id, updated_at) "
            "VALUES (:i, :v, NOW()) "
            "ON DUPLICATE KEY UPDATE last_event_id = :v, updated_at = NOW()"),
            {'i': int(row_id), 'v': int(v)})


def accumulate(verbose=True):
    """Fold new kill events into the permanent per-guild counters.

    Safe to run on any schedule and safe to run twice: the cursor only moves
    forward, so an event is never counted a second time. Run it AT LEAST daily --
    events older than seven days are gone from the source.
    """
    ensure_tables()
    # The all-raid-kill day tally rides along here so it inherits the same
    # schedule -- it is what keeps the guild pages fast, and a separate job
    # would be one more thing to forget to run.
    try:
        accumulate_raid_days(verbose)
    except Exception as e:
        print("raid-day tally failed (guild pages fall back to scanning): %s" % e)

    since_id = _cursor()

    npc_to_boss = {}
    for key, ids in TRACKED_BOSSES.items():
        for i in ids:
            npc_to_boss[i] = key

    with get_db(logic.engine) as s:
        rows = s.execute(text("""
            SELECT l.id, l.created_at, l.instance_id,
                   JSON_EXTRACT(l.event_data, '$.npc_id') AS npc_id,
                   gm.guild_id, l.character_id
            FROM player_event_logs l
            JOIN guild_members gm ON gm.char_id = l.character_id
            JOIN character_data cd ON cd.id = l.character_id
                 AND cd.gm = 0 AND cd.deleted_at IS NULL
            WHERE l.id > :since
              AND l.event_type_name IN ('Killed Raid NPC', 'Killed Named NPC')
            ORDER BY l.id
        """), {'since': since_id}).all()

    if not rows:
        if verbose:
            print("no new kill events since id %d" % since_id)
        return 0

    max_id = max(r[0] for r in rows)

    # One kill = one row per participant. Collapse to the kill, THEN credit each
    # guild once -- otherwise a trio from the same guild would count three times.
    seen = {}
    chars = {}
    for _id, ts, inst, npc, gid, cid in rows:
        try:
            npc = int(str(npc).strip('"'))
        except (TypeError, ValueError):
            continue
        boss = npc_to_boss.get(npc)
        if not boss or gid is None:
            continue
        seen.setdefault((boss, ts, inst), set()).add(int(gid))
        # Per character: each participant's own row IS their credit, so this is
        # counted per event rather than per collapsed kill.
        if cid:
            k = (int(cid), boss)
            n, last = chars.get(k, (0, ts))
            chars[k] = (n + 1, max(last, ts))

    tally = {}
    for (boss, ts, _inst), guilds in seen.items():
        for gid in guilds:
            k = (gid, boss)
            n, first, last = tally.get(k, (0, ts, ts))
            tally[k] = (n + 1, min(first, ts), max(last, ts))

    with lb_engine.begin() as c:
        for (gid, boss), (n, first, last) in tally.items():
            c.execute(text(
                "INSERT INTO lb_guild_boss_kill (guild_id, boss_key, kills, first_kill, last_kill) "
                "VALUES (:g, :b, :n, :f, :l) "
                "ON DUPLICATE KEY UPDATE kills = kills + :n, "
                " first_kill = LEAST(COALESCE(first_kill, :f), :f), "
                " last_kill = GREATEST(COALESCE(last_kill, :l), :l)"),
                {'g': gid, 'b': boss, 'n': n, 'f': first, 'l': last})
        for (cid, boss), (n, last) in chars.items():
            c.execute(text(
                "INSERT INTO lb_char_boss_kill (char_id, boss_key, kills, last_kill) "
                "VALUES (:c, :b, :n, :l) "
                "ON DUPLICATE KEY UPDATE kills = kills + :n, "
                " last_kill = GREATEST(COALESCE(last_kill, :l), :l)"),
                {'c': cid, 'b': boss, 'n': n, 'l': last})
    _set_cursor(max_id)
    _gather_cache['data'] = None    # counters just moved; don't serve the old view

    if verbose:
        print("scanned %d event rows -> %d distinct kills, %d guild counters, %d character counters"
              % (len(rows), len(seen), len(tally), len(chars)))
        print("cursor %d -> %d" % (since_id, max_id))
    return len(tally)


# --------------------------------------------------------------------------- #
# Raid-kill day tally -- what makes the guild pages fast                        #
# --------------------------------------------------------------------------- #
# Counting rules match _kill_stats, with two deliberate differences:
#   * GM and deleted characters are excluded (the live scan counted them).
#   * instance_id joins the distinct key, so two instances killing the same NPC
#     in the same second count as two kills rather than one.
# Both are corrections, and both move counts by a hair at most.
_RAID_JOIN = """
        FROM player_event_logs l
        JOIN guild_members gm ON gm.char_id = l.character_id
        JOIN character_data cd ON cd.id = l.character_id
             AND cd.gm = 0 AND cd.deleted_at IS NULL
        WHERE l.event_type_name = 'Killed Raid NPC'
"""


def _seed_raid_days(verbose=True):
    """One-off backfill of everything currently in the event log.

    Runs the expensive aggregate ONCE, and grouped by day -- which also splits
    the killer distinct set (140k values for the largest guild) into seven
    day-sized ones, so even this pass is cheaper than the query it retires.
    Returns the event id the incremental tally should start from.
    """
    with get_db(logic.engine) as s:
        rows = s.execute(text("""
            SELECT gm.guild_id, DATE(l.created_at) AS d,
                   COUNT(DISTINCT CONCAT(l.created_at, '-',
                         JSON_EXTRACT(l.event_data, '$.npc_id'), '-',
                         COALESCE(l.instance_id, 0)))
        """ + _RAID_JOIN + """
            GROUP BY gm.guild_id, DATE(l.created_at)
        """)).all()
        top = s.execute(text("SELECT COALESCE(MAX(l.id), 0) FROM player_event_logs l "
                             "WHERE l.event_type_name = 'Killed Raid NPC'")).scalar() or 0

    with lb_engine.begin() as c:
        for gid, day, n in rows:
            c.execute(text(
                "INSERT INTO lb_guild_raid_day (guild_id, day, kills) VALUES (:g, :d, :n) "
                "ON DUPLICATE KEY UPDATE kills = GREATEST(kills, :n)"),
                {'g': int(gid), 'd': day, 'n': int(n)})
    if verbose:
        print("seeded %d guild-days from the event log (cursor -> %d)" % (len(rows), top))
    return top


def accumulate_raid_days(verbose=True):
    """Fold new raid kills into per-guild, per-day counts.

    Forward-only like the boss tally, and for the same reason: the event log
    prunes at seven days, so anything missed cannot be recovered. Seeds itself on
    first run so a fresh deploy never shows zeros.
    """
    ensure_tables()
    since = _cursor(RAID_DAY_CURSOR)
    if not since:
        _set_cursor(_seed_raid_days(verbose), RAID_DAY_CURSOR)
        return 0

    with get_db(logic.engine) as s:
        rows = s.execute(text("""
            SELECT l.id, l.created_at, l.instance_id,
                   JSON_EXTRACT(l.event_data, '$.npc_id') AS npc, gm.guild_id
        """ + _RAID_JOIN + """
              AND l.id > :since
            ORDER BY l.id
        """), {'since': since}).all()

    if not rows:
        if verbose:
            print("no new raid kills since id %d" % since)
        return 0

    # One kill = one row per participant: collapse to the kill first, then credit
    # each guild that was there exactly once.
    seen = {}
    for _id, ts, inst, npc, gid in rows:
        if gid is None:
            continue
        seen.setdefault((ts, str(npc), inst), set()).add(int(gid))

    tally = {}
    for (ts, _npc, _inst), guilds in seen.items():
        for gid in guilds:
            k = (gid, ts.date())
            tally[k] = tally.get(k, 0) + 1

    with lb_engine.begin() as c:
        for (gid, day), n in tally.items():
            c.execute(text(
                "INSERT INTO lb_guild_raid_day (guild_id, day, kills) VALUES (:g, :d, :n) "
                "ON DUPLICATE KEY UPDATE kills = kills + :n"),
                {'g': gid, 'd': day, 'n': n})
    _set_cursor(max(r[0] for r in rows), RAID_DAY_CURSOR)

    if verbose:
        print("raid days: %d event rows -> %d kills -> %d guild-days"
              % (len(rows), len(seen), len(tally)))
    return len(tally)


def raid_kill_window(days=7):
    """{guild_id: kills} over the last `days`, from the accumulated table.

    Anchored to the newest day PRESENT rather than to today, so a restored
    snapshot reports its own last week instead of an empty one -- the same
    reasoning as guild_portal._activity_anchor. Returns {} if never seeded, which
    is the caller's signal to fall back to scanning the event log.
    """
    import datetime
    ensure_tables()
    with connection(lb_engine) as c:
        anchor = c.execute(text("SELECT MAX(day) FROM lb_guild_raid_day")).scalar()
        if not anchor:
            return {}
        if isinstance(anchor, datetime.datetime):
            anchor = anchor.date()
        cutoff = anchor - datetime.timedelta(days=max(days, 1) - 1)
        rows = c.execute(text(
            "SELECT guild_id, SUM(kills) FROM lb_guild_raid_day "
            "WHERE day >= :cut GROUP BY guild_id"), {'cut': cutoff}).all()
    return {int(g): int(n or 0) for g, n in rows}


def char_kill_counts(char_ids):
    """{char_id: {'total': n, 'by_boss': {key: n}}} for the contributor board."""
    ids = [int(c) for c in char_ids if c]
    if not ids:
        return {}
    ensure_tables()
    out = {}
    with connection(lb_engine) as c:
        q = (text("SELECT char_id, boss_key, kills FROM lb_char_boss_kill "
                  "WHERE char_id IN :ids")
             .bindparams(bindparam('ids', expanding=True)))
        for cid, boss, n in c.execute(q, {'ids': ids}).all():
            e = out.setdefault(int(cid), {'total': 0, 'by_boss': {}})
            e['by_boss'][boss] = int(n)
            e['total'] += int(n)
    return out


def _kill_counts():
    """{guild_id: {boss_key: (kills, first, last)}}"""
    ensure_tables()
    out = {}
    with connection(lb_engine) as c:
        for gid, boss, n, first, last in c.execute(text(
                "SELECT guild_id, boss_key, kills, first_kill, last_kill "
                "FROM lb_guild_boss_kill")).all():
            out.setdefault(int(gid), {})[boss] = (int(n), first, last)
    return out


# --------------------------------------------------------------------------- #
# Permanent stats                                                              #
# --------------------------------------------------------------------------- #
_VISIBLE = "cd.gm = 0 AND cd.deleted_at IS NULL AND cd.anon <> 1"


def _gather():
    """{guild_id: {stat: value}} for every guild, in a handful of queries."""
    stats = {}

    def put(gid, key, val):
        stats.setdefault(int(gid), {})[key] = int(val or 0)

    with get_db(logic.engine) as s:
        anchor = int(s.execute(text(
            "SELECT COALESCE(MAX(last_login), 0) FROM character_data")).scalar() or 0)
        cutoff = anchor - 14 * 86400

        for gid, capped, active, aa in s.execute(text(f"""
            SELECT gm.guild_id, SUM(cd.level >= 65), SUM(cd.last_login >= :cut),
                   SUM(cd.aa_points_spent)
            FROM guild_members gm
            JOIN character_data cd ON cd.id = gm.char_id AND {_VISIBLE}
            GROUP BY gm.guild_id"""), {'cut': cutoff}).all():
            put(gid, 'capped', capped)
            put(gid, 'active', active)
            put(gid, 'total_aa', aa)

        for gid, n in s.execute(text(f"""
            SELECT gm.guild_id, COUNT(*)
            FROM discovered_items di
            JOIN character_data cd ON cd.name = di.char_name AND {_VISIBLE}
            JOIN guild_members gm ON gm.char_id = cd.id
            GROUP BY gm.guild_id""")).all():
            put(gid, 'discoveries', n)

        for gid, n in s.execute(text(f"""
            SELECT gm.guild_id, SUM(akc.count)
            FROM account_kill_counts akc
            JOIN character_data cd ON cd.account_id = akc.account_id AND {_VISIBLE}
            JOIN guild_members gm ON gm.char_id = cd.id
            GROUP BY gm.guild_id""")).all():
            put(gid, 'creature_kills', n)

        for gid, n in s.execute(text(f"""
            SELECT gm.guild_id, COUNT(DISTINCT cd.account_id)
            FROM guild_members gm
            JOIN character_data cd ON cd.id = gm.char_id AND {_VISIBLE}
            JOIN data_buckets db ON db.`key` = CONCAT(cd.account_id, '-progress-flag-PoP')
            GROUP BY gm.guild_id""")).all():
            put(gid, 'pop_unlocked', n)

        # The Time flag is complete when the final step (Loreseeker Maelin) is set.
        for gid, n in s.execute(text(f"""
            SELECT gm.guild_id, COUNT(DISTINCT cd.account_id)
            FROM guild_members gm
            JOIN character_data cd ON cd.id = gm.char_id AND {_VISIBLE}
            JOIN data_buckets db ON db.`key` = 'pop' AND db.account_id = cd.account_id
            WHERE db.value LIKE '%%"maelin"%%'
            GROUP BY gm.guild_id""")).all():
            put(gid, 'time_complete', n)

    for gid, bosses in _kill_counts().items():
        for boss, (n, _f, _l) in bosses.items():
            put(gid, 'kills_%s' % boss, n)

    return stats


# --------------------------------------------------------------------------- #
# Evaluation                                                                   #
# --------------------------------------------------------------------------- #
def _evaluate(stat_map):
    """One guild's stats -> its achievement view-model."""
    out, earned, points = [], 0, 0
    for a in ACHIEVEMENTS:
        val = int(stat_map.get(a['stat'], 0) or 0)
        tiers = a['tiers']
        level = sum(1 for t in tiers if val >= t)          # 0 = not started
        nxt = tiers[level] if level < len(tiers) else None
        prev = tiers[level - 1] if level else 0
        if nxt:
            span = max(nxt - prev, 1)
            pct = max(0, min(100, round(100.0 * (val - prev) / span)))
        else:
            pct = 100
        if level:
            earned += 1
            points += tier_points(level)
        out.append({
            'key': a['key'], 'name': a['name'], 'icon': a['icon'], 'desc': a['desc'],
            'unit': a['unit'], 'accumulated': a.get('accumulated', False),
            'value': val, 'tiers': list(tiers),
            'level': level, 'max_level': len(tiers),
            'roman': TIER_NAMES[level - 1] if level else None,
            # the tier being WORKED TOWARD, which is one past the one held
            'next_roman': TIER_NAMES[level] if level < len(tiers) else None,
            'complete': level == len(tiers),
            'next': nxt, 'remaining': (nxt - val) if nxt else 0, 'pct': pct,
        })
    out.sort(key=lambda x: (-x['level'], -x['pct']))
    return {'list': out, 'earned': earned, 'total': len(ACHIEVEMENTS),
            'points': points, 'max_points': MAX_POINTS,
            'pct_points': round(100.0 * points / MAX_POINTS) if MAX_POINTS else 0}


# _gather() is six server-wide aggregates over the game DB. A guild page used to
# run the whole thing TWICE (once inside list_guilds, once for the page's own
# badges) and the directory once more, none of which can change faster than the
# accumulator writes. 60s matches the site's other read caches (_TP_TTL,
# _RANK_TTL) -- long enough to collapse a page load to a single pass, short
# enough that a fresh kill shows up while someone is still looking at the page.
_gather_cache = {'data': None, 'ts': 0.0}
_GATHER_TTL = 60


def gather(force=False):
    import time
    now = time.time()
    if not force and _gather_cache['data'] is not None \
            and (now - _gather_cache['ts']) < _GATHER_TTL:
        return _gather_cache['data']
    data = _gather()
    _gather_cache['data'] = data
    _gather_cache['ts'] = now
    return data


def for_guild(guild_id):
    return _evaluate(gather().get(int(guild_id), {}))


def for_all():
    """{guild_id: view-model} -- one pass, for the directory."""
    return {gid: _evaluate(sm) for gid, sm in gather().items()}


if __name__ == '__main__':
    import sys
    if 'accumulate' in sys.argv:
        accumulate()
    elif 'raid-days' in sys.argv:
        # Seed or advance the raid-kill day tally on its own. Use this right
        # after deploying to make the guild pages fast immediately, rather than
        # waiting for the next sync.
        accumulate_raid_days()
        w = raid_kill_window()
        print("7-day window now covers %d guilds, %s kills"
              % (len(w), "{:,}".format(sum(w.values()))))
    else:
        res = for_all()
        for gid, r in sorted(res.items(), key=lambda kv: -kv[1]['points']):
            print("guild %-4s %2d/%d earned, %3d pts  %s"
                  % (gid, r['earned'], r['total'], r['points'],
                     ", ".join("%s %s" % (a['name'], a['roman'])
                               for a in r['list'] if a['level'])))
