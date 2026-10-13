from db import get_db
from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify
from sqlalchemy.orm import Session
from sqlalchemy import text, desc, bindparam
import effect_score
import logic
import utils
from create_leaderboard_db import engine as lb_engine, ItemScore
from gear_score import (score_many_characters, class_trio_from_bitmask,
                        get_reference_max, normalize, SCORED_SLOT_MAX)
import leaderboard_read as lbr
import pop_progress

bp = Blueprint('characters', __name__, template_folder='templates')


def gestalt_class_display(session, char_ids):
    """NMS stores each character's real (multi)class set as a class bitmask in
    data_buckets under key 'GestaltClasses' (character_data.class itself is just
    a placeholder). Return {character_id: 'War/Pal/Mnk'} for the given ids."""
    ids = [int(c) for c in char_ids if c]
    if not ids:
        return {}
    q = (text("SELECT character_id, value FROM data_buckets "
              "WHERE `key` = 'GestaltClasses' AND character_id IN :ids")
         .bindparams(bindparam('ids', expanding=True)))
    out = {}
    for character_id, value in session.execute(q, {'ids': ids}).all():
        try:
            out[character_id] = class_trio_from_bitmask(int(value))
        except (TypeError, ValueError):
            continue
    return out


def top_aa_rankings(limit=100):
    """Top characters by AA points spent (the Players page AA board)."""
    with get_db(logic.engine) as session:
        CharData = logic.Base.classes.character_data
        GuildMembers = logic.Base.classes.guild_members
        Guilds = logic.Base.classes.guilds
        rows = (session.query(CharData.id, CharData.name, CharData.level, getattr(CharData, 'class'), CharData.race,
                              CharData.aa_points_spent, CharData.time_played, CharData.pvp_kills,
                              Guilds.name.label('guild_name'), Guilds.id.label('guild_id'))
                .outerjoin(GuildMembers, GuildMembers.char_id == CharData.id)
                .outerjoin(Guilds, Guilds.id == GuildMembers.guild_id)
                .filter(CharData.gm == 0, CharData.anon != 1,        # show normal + roleplay (2); hide only true anon (1)
                        getattr(CharData, 'deleted_at').is_(None))
                .order_by(desc(CharData.aa_points_spent)).limit(limit).all())
        cmap = gestalt_class_display(session, [r[0] for r in rows])
    return [{'id': r[0], 'name': r[1], 'level': r[2], 'class_name': cmap.get(r[0], '?'),
             'race_name': RACES.get(r[4], f"Unknown ({r[4]})"), 'aa_points': r[5], 'time_played': r[6],
             'pvp_kills': r[7], 'guild_name': r[8] or "", 'guild_id': int(r[9]) if r[9] else None}
            for r in rows]


def get_gear_rankings(limit=100, season_id=None):
    """Server-wide gear-score ranking: score every character from their equipped
    inventory (worn slots 0-22) against the precomputed lb_item_score table."""
    # 1. Equipped item ids per character (from the game/content DB).
    #    Augments are scored too -- they carry real stats, so they count.
    with get_db(logic.engine) as session:
        rows = session.execute(text(
            "SELECT character_id, item_id, augment_one, augment_two, augment_three, "
            "augment_four, augment_five, augment_six FROM inventory "
            f"WHERE slot_id BETWEEN 0 AND {SCORED_SLOT_MAX} AND item_id > 0")).all()
    char_items = {}
    for char_id, item_id, *augs in rows:
        for aug in augs:
            if aug:
                char_items.setdefault(char_id, []).append(aug)
        char_items.setdefault(char_id, []).append(item_id)
    if not char_items:
        return []

    # 2. Score them all (from the leaderboard DB).
    shares = effect_score.effect_shares(logic.engine) if _season2_scoring(season_id) else None
    with get_db(lb_engine) as lb_session:
        scores = score_many_characters(lb_session, char_items, effect_shares=shares)

    # 3. Attach names/class/guild for the top N.
    top_ids = [cid for cid, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)][:limit]
    if not top_ids:
        return []
    with get_db(logic.engine) as session:
        CharData = logic.Base.classes.character_data
        GuildMembers = logic.Base.classes.guild_members
        Guilds = logic.Base.classes.guilds
        meta = {r[0]: r for r in session.query(
            CharData.id, CharData.name, CharData.level,
            Guilds.name.label('guild_name'), Guilds.id.label('guild_id'))
            .outerjoin(GuildMembers, GuildMembers.char_id == CharData.id)
            .outerjoin(Guilds, Guilds.id == GuildMembers.guild_id)
            .filter(CharData.id.in_(top_ids), CharData.gm == 0, CharData.anon != 1,        # show normal + roleplay (2); hide only true anon (1)
                    getattr(CharData, 'deleted_at').is_(None)).all()}
        classes = gestalt_class_display(session, list(meta.keys()))

    ranked = []
    for cid in top_ids:
        if cid not in meta:
            continue  # gm / anon / deleted filtered out
        _id, name, level, guild, guild_id = meta[cid]
        ranked.append({'id': cid, 'name': name, 'gear_score': scores[cid],
                       'class_name': classes.get(cid, '?'),
                       'level': level, 'guild_name': guild or '',
                       'guild_id': int(guild_id) if guild_id else None})
    return ranked


# Top Players composite weights (sum to 1000 -> a maxed character reads ~1000).
# Level and AA are normalized to their live maxima; gear score is already 0-1000;
# parse score is the character's overall parse average (0-100) across all bosses.
# Gear leads; level/AA/parse are balanced so parse matters without dominating.
TP_WEIGHT_LEVEL = 150.0
TP_WEIGHT_AA = 275.0
TP_WEIGHT_GEAR = 375.0
TP_WEIGHT_PARSE = 200.0

# Human-readable breakdown for the "How Player Score works" footer (weights above,
# ordered high -> low). Kept next to the weights so the two never drift apart.
SCORE_WEIGHTS = [
    ('Gear', TP_WEIGHT_GEAR, 'equipped-gear score vs best-in-slot for the era'),
    ('AA', TP_WEIGHT_AA, 'AA spent, relative to the highest on the server'),
    ('Parse', TP_WEIGHT_PARSE, 'overall raid parse — average of your best parse per boss (unkilled bosses count as 0)'),
    ('Level', TP_WEIGHT_LEVEL, 'character level out of 65'),
]


def get_top_players(limit=100, season_id=None, weights=None, speed_scores=None, progression_scores=None):
    """Composite 'Top Players' ranking from Level + AA spent + Gear Score + Parse.
    Parse comes from ``season_id`` (default: the season the site opens on).
    Excludes GM / anonymous / deleted characters."""
    with get_db(logic.engine) as session:
        CharData = logic.Base.classes.character_data
        GuildMembers = logic.Base.classes.guild_members
        Guilds = logic.Base.classes.guilds
        # guild id as well as name -- the board links each guild to its portal page
        rows = (session.query(CharData.id, CharData.name, CharData.level,
                              CharData.aa_points_spent, Guilds.name.label('guild'),
                              Guilds.id.label('guild_id'))
                .outerjoin(GuildMembers, GuildMembers.char_id == CharData.id)
                .outerjoin(Guilds, Guilds.id == GuildMembers.guild_id)
                .filter(CharData.gm == 0, CharData.anon != 1,        # show normal + roleplay (2); hide only true anon (1)
                        getattr(CharData, 'deleted_at').is_(None)).all())
        if not rows:
            return []
        char_ids = [r[0] for r in rows]
        classes = gestalt_class_display(session, char_ids)
        inv = session.execute(text(
            "SELECT character_id, item_id, augment_one, augment_two, augment_three, "
            "augment_four, augment_five, augment_six FROM inventory "
            f"WHERE slot_id BETWEEN 0 AND {SCORED_SLOT_MAX} AND item_id > 0 AND character_id IN :ids")
            .bindparams(bindparam('ids', expanding=True)), {'ids': char_ids}).all()

    char_items = {}
    for cid, item_id, *augs in inv:
        char_items.setdefault(cid, []).append(item_id)
        for aug in augs:
            if aug:
                char_items[cid].append(aug)
    shares = effect_score.effect_shares(logic.engine) if _season2_scoring(season_id) else None
    with get_db(lb_engine) as lb_session:
        gear = score_many_characters(lb_session, char_items, effect_shares=shares)
    parse_scores = lbr.get_parse_scores(season_id)    # {char_id: overall parse avg 0-100}

    max_aa = max((r[3] or 0) for r in rows) or 1
    players = []
    for cid, name, level, aa, guild, guild_id in rows:
        aa = aa or 0
        gs = gear.get(cid, 0)
        ps = parse_scores.get(cid, 0.0)      # 0 for anyone with no ranked raid kills
        w = weights or {'level': TP_WEIGHT_LEVEL, 'aa': TP_WEIGHT_AA, 'gear': TP_WEIGHT_GEAR,
                        'parse': TP_WEIGHT_PARSE}
        sp = (speed_scores or {}).get(cid, 0.0)   # 0-100, in seasons that score speed (Season 2)
        pr = (progression_scores or {}).get(cid, 0.0)  # 0-100 LDoN/MM progression (Season 2)
        score = round((level / 65.0) * w['level']
                      + (aa / max_aa) * w['aa']
                      + (gs / 1000.0) * w['gear']
                      + (ps / 100.0) * w['parse']
                      + (sp / 100.0) * w.get('speed', 0.0)
                      + (pr / 100.0) * w.get('progression', 0.0))
        players.append({'id': cid, 'name': name, 'class_name': classes.get(cid, '?'),
                        'level': level, 'aa': aa, 'gear_score': gs, 'parse_score': ps, 'speed_score': sp, 'progression_score': pr,
                        'score': score, 'guild_name': guild or '',
                        'guild_id': int(guild_id) if guild_id else None})
    players.sort(key=lambda p: p['score'], reverse=True)
    players = players[:limit]
    bands = lbr.get_parse_bands()
    for i, p in enumerate(players, start=1):
        p['rank'] = i
        p['parse_trophy'] = lbr.trophy_for(bands, p['parse_score']) if p['parse_score'] else None
    return players


_rank_cache = {'data': None, 'ts': 0}
_RANK_TTL = 60  # seconds


def get_player_rank_map():
    """{char_id: {'rank', 'score', 'trio_rank', 'trio_size'}} for ALL players.

    'rank' is the server-wide rank by player score; 'trio_rank' is the player's
    rank *within their own exact trio* (same GestaltClasses combo), so it's
    correct regardless of how a character was searched. Cached."""
    import time
    now = time.time()
    if _rank_cache['data'] is not None and (now - _rank_cache['ts']) < _RANK_TTL:
        return _rank_cache['data']
    ranked = get_top_players(limit=10 ** 9)  # score + overall rank for everyone
    ids = [p['id'] for p in ranked]
    bits = {}
    if ids:
        with get_db(logic.engine) as session:
            rows = session.execute(text(
                "SELECT character_id, CAST(value AS UNSIGNED) FROM data_buckets "
                "WHERE `key` = 'GestaltClasses' AND character_id IN :ids")
                .bindparams(bindparam('ids', expanding=True)), {'ids': ids}).all()
        bits = {cid: b for cid, b in rows}
    # group players by their exact trio (gestalt bitmask), rank within each group
    score_by = {p['id']: p['score'] for p in ranked}
    groups = {}
    for p in ranked:  # ranked is already score-desc
        groups.setdefault(bits.get(p['id']), []).append(p['id'])
    trio_rank, trio_size = {}, {}
    for members in groups.values():
        members.sort(key=lambda cid: score_by.get(cid, 0), reverse=True)
        for i, cid in enumerate(members, start=1):
            trio_rank[cid] = i
            trio_size[cid] = len(members)
    # server-wide gear-score rank
    by_gear = sorted(ranked, key=lambda p: p.get('gear_score', 0), reverse=True)
    gear_rank = {p['id']: i for i, p in enumerate(by_gear, start=1)}
    # server-wide parse rank (only players with a ranked parse score are ranked)
    parsers = sorted((p for p in ranked if p.get('parse_score')),
                     key=lambda p: p['parse_score'], reverse=True)
    parse_rank = {p['id']: i for i, p in enumerate(parsers, start=1)}
    bands = lbr.get_parse_bands()
    m = {p['id']: {'rank': p['rank'], 'score': p['score'],
                   'trio_rank': trio_rank.get(p['id']), 'trio_size': trio_size.get(p['id']),
                   'gear_rank': gear_rank.get(p['id']), 'gear_score': p.get('gear_score', 0),
                   'parse_score': p.get('parse_score', 0), 'parse_rank': parse_rank.get(p['id']),
                   'parse_trophy': lbr.trophy_for(bands, p['parse_score']) if p.get('parse_score') else None}
         for p in ranked}
    _rank_cache['data'] = m
    _rank_cache['ts'] = now
    return m


def _tier_for(gear_score):
    """Tier name + progress toward the next tier for a normalized gear score.
    Mirrors the character-sheet ladder so the dashboard reads the same."""
    tiers = gear_tiers()
    ladder = sorted(tiers)  # ascending by cutoff
    tier = next(t for cut, t in tiers if gear_score >= cut)
    nxt = next(((c, t) for c, t in ladder if c > gear_score), None)
    cur_cut = next(c for c, t in reversed(ladder) if gear_score >= c)
    if nxt:
        span = max(1, nxt[0] - cur_cut)
        return {'tier': tier, 'next_tier': nxt[1], 'need': nxt[0] - gear_score,
                'tier_progress': round((gear_score - cur_cut) / span * 100)}
    return {'tier': tier, 'next_tier': None, 'need': 0, 'tier_progress': 100}


# Linked-character rows: (id, name, level, aa_points_spent, anon, race, last_login).


def _no_linked_rows(session, discord_id):
    """Public default: no Discord-to-game-account linking, so nobody owns any
    character. A private extension may install its own provider by assigning
    ``routes.characters.linked_character_rows`` (same signature, rows shaped as
    described above)."""
    return []


linked_character_rows = _no_linked_rows


def get_linked_characters(discord_id):
    """Every non-deleted character on the game accounts linked to this Discord user,
    as reported by linked_character_rows. Enriched with rank/gear/trio from the
    cached rank map. Anonymous characters are included for the OWNER's own view but
    carry no public rank (they're excluded from the rank map by design)."""
    with get_db(logic.engine) as session:
        rows = linked_character_rows(session, discord_id)
        if not rows:
            return []
        ids = [r[0] for r in rows]
        classes = gestalt_class_display(session, ids)
    rmap = get_player_rank_map()
    out = []
    for cid, name, level, aa, anon, race, last_login in rows:
        info = rmap.get(cid, {})
        gs = info.get('gear_score', 0)
        c = {'id': cid, 'name': name, 'level': level, 'aa': aa or 0,
             'anon': anon == 1, 'class_name': classes.get(cid, '?'),   # roleplay (2) is public
             'race_name': RACES.get(race, ''),
             'score': info.get('score', 0), 'rank': info.get('rank'),
             'trio_rank': info.get('trio_rank'), 'trio_size': info.get('trio_size'),
             'gear_rank': info.get('gear_rank'), 'gear_score': gs,
             'parse_score': info.get('parse_score', 0), 'parse_trophy': info.get('parse_trophy'),
             'last_login': last_login}
        c.update(_tier_for(gs))
        out.append(c)
    out.sort(key=lambda c: (c['score'], c['level']), reverse=True)
    return out


def _compute_milestones(chars, raid_records):
    """Earned achievement badges rolled up across a user's roster. Each badge is
    {name, desc, earned, color}; earned ones render lit, the rest stay locked."""
    pub = [c for c in chars if not c['anon']]
    max_level = max((c['level'] for c in pub), default=0)
    tiers = {c.get('tier') for c in pub}
    max_parse = max((c.get('parse_score') or 0 for c in pub), default=0)
    bosses_cleared = sum(1 for r in raid_records if r['clears'] > 0)
    gauntlet = any(r['is_gauntlet'] and r['clears'] > 0 for r in raid_records)
    GOLD, TEAL, ARC, VIOLET, EMB = '#ffd558', '#49d0a0', '#4db2ff', '#b79bff', '#ff8a4a'
    return [
        {'name': 'Level Cap', 'desc': 'Reached level 65.',
         'earned': max_level >= 65, 'color': GOLD},
        {'name': 'Legendary Arsenal', 'desc': 'Fielded a Legendary-tier character.',
         'earned': bool(tiers & {'Legendary', 'Mythic'}), 'color': EMB},
        {'name': 'First Blood', 'desc': 'Downed your first raid boss.',
         'earned': bosses_cleared > 0, 'color': TEAL},
        {'name': 'Boss Hunter', 'desc': 'Cleared 2+ different raid bosses.',
         'earned': bosses_cleared >= 2, 'color': ARC},
        {'name': 'Gauntlet Runner', 'desc': 'Completed the Plane of Time gauntlet.',
         'earned': gauntlet, 'color': VIOLET},
        {'name': 'Elite Parser', 'desc': 'Earned an Orange-tier parse (95+).',
         'earned': max_parse >= 95, 'color': EMB},
        {'name': 'Legend', 'desc': 'Hit a Legend parse (100).',
         'earned': max_parse >= 100, 'color': GOLD},
    ]


def get_dashboard(discord_id, main_id=None):
    """Full dashboard payload for a logged-in Discord user: their linked
    characters, chosen (or highest-score) main, roll-up stats, and trio spotlight."""
    chars = get_linked_characters(discord_id)
    if not chars:
        return {'linked': False, 'characters': [], 'main': None,
                'rollup': None, 'trio': None}
    main = next((c for c in chars if c['id'] == main_id), None) if main_id else None
    if not main:
        main = chars[0]                       # default: highest total player score
    for c in chars:
        c['is_main'] = (c['id'] == main['id'])
    ranked = [c for c in chars if not c['anon'] and c['rank']]
    rollup = {'count': len(chars),
              'best_rank': min((c['rank'] for c in ranked), default=None),
              'top_gear': max((c['gear_score'] for c in chars), default=0),
              'combined_aa': sum(c['aa'] for c in chars)}
    trio = None
    if not main['anon'] and main.get('trio_rank'):
        trio = {'classes': [x.strip() for x in main['class_name'].split('/')],
                'rank': main['trio_rank'], 'size': main['trio_size']}
    pub_ids = [c['id'] for c in chars if not c['anon']]
    raid_records = lbr.get_roster_raid_records(pub_ids)
    milestones = _compute_milestones(chars, raid_records)
    return {'linked': True, 'characters': chars, 'main': main,
            'rollup': rollup, 'trio': trio,
            'raid_records': raid_records, 'milestones': milestones}


CLASSES = {
    1: "Warrior", 2: "Cleric", 3: "Paladin", 4: "Ranger", 5: "Shadow Knight",
    6: "Druid", 7: "Monk", 8: "Bard", 9: "Rogue", 10: "Shaman",
    11: "Necromancer", 12: "Wizard", 13: "Magician", 14: "Enchanter",
    15: "Beastlord", 16: "Berserker"
}

RACES = {
    1: "Human", 2: "Barbarian", 3: "Erudite", 4: "Wood Elf", 5: "High Elf",
    6: "Dark Elf", 7: "Half Elf", 8: "Dwarf", 9: "Troll", 10: "Ogre",
    11: "Halfling", 12: "Gnome", 128: "Iksar", 130: "Vah Shir",
    330: "Froglok", 522: "Drakkin"
}

GENDERS = {0: "Male", 1: "Female", 2: "Neuter"}

DEITIES = {
    0: "Agnostic", 140: "Agnarr", 201: "Bertoxxulous", 202: "Brell Serilis", 203: "Cazic-Thule",
    204: "Erollisi Marr", 205: "Bristlebane", 206: "Innoruuk", 207: "Karana",
    208: "Mithaniel Marr", 209: "Prexus", 210: "Quellious", 211: "Rallos Zek",
    212: "Rodcet Nife", 213: "Solusek Ro", 214: "The Tribunal", 215: "Tunare",
    216: "Veeshan", 396: "The Nameless"
}

SLOTS = {
    0: "Charm", 1: "Ear 1", 2: "Head", 3: "Face", 4: "Ear 2", 5: "Neck",
    6: "Shoulders", 7: "Arms", 8: "Back", 9: "Wrist 1", 10: "Wrist 2",
    11: "Range", 12: "Hands", 13: "Primary", 14: "Secondary", 15: "Finger 1",
    16: "Finger 2", 17: "Chest", 18: "Legs", 19: "Feet", 20: "Waist",
    21: "Power Source", 22: "Ammo"
}

# Item stat columns pulled for the redesigned profile (tooltips + heroic/derived sums).
GEAR_STAT_COLS = ['ac', 'hp', 'mana', 'endur', 'astr', 'asta', 'aagi', 'adex', 'awis', 'aint', 'acha',
                  'heroic_str', 'heroic_sta', 'heroic_agi', 'heroic_dex', 'heroic_wis', 'heroic_int', 'heroic_cha',
                  'cr', 'fr', 'mr', 'pr', 'dr', 'heroic_cr', 'heroic_fr', 'heroic_mr', 'heroic_pr', 'heroic_dr',
                  'attack', 'haste', 'damage', 'delay', 'spelldmg', 'healamt', 'accuracy', 'regen', 'shielding',
                  'stunresist', 'damageshield', 'worneffect', 'focuseffect', 'proceffect', 'clickeffect']

# gear-score -> tier label (highest cutoff first)
GEAR_TIERS = [(850, 'Mythic'), (725, 'Legendary'), (575, 'Epic'), (400, 'Superior'), (0, 'Adventuring')]


def _season2_scoring(season_id=None):
    """Season 2 gear scoring (effects + lockers) for this season id; with no id, by today's date."""
    if season_id is None:
        return effect_score.season2_started()
    return any(r[0] == season_id and r[1] == 'season-2' for r in lbr._season_rows())


def gear_tiers():
    """Tier cut-offs in force now: Season 2's are 15% lower (gear fills 850 of the 1000)."""
    return effect_score.season2_tiers(GEAR_TIERS) if effect_score.season2_started() else GEAR_TIERS

# Equip slot id -> paper-doll grid-area (the layout the user specified). Order matters
# for rendering; slot 21 (Power Source) is intentionally omitted.
SLOT_LAYOUT = [(1, 'ear1'), (2, 'head'), (3, 'face'), (4, 'ear2'),
               (17, 'chest'), (5, 'neck'), (7, 'arms'), (8, 'back'),
               (20, 'waist'), (6, 'shld'), (9, 'wri1'), (10, 'wri2'),
               (18, 'legs'), (12, 'hands'), (0, 'charm'), (19, 'feet'),
               (15, 'fin1'), (16, 'fin2'), (13, 'prim'), (14, 'seco'),
               (11, 'rang'), (22, 'ammo')]


def _build_sigil():
    """Static arcane magic-circle SVG rendered behind the gear-score gauge."""
    import math
    cx = cy = 100

    def pt(r, a):
        return (cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))
    ticks = []
    for i in range(48):
        a = i * 7.5
        ir, orr = 85, (94 if i % 4 == 0 else 90)
        x1, y1 = pt(ir, a)
        x2, y2 = pt(orr, a)
        ticks.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>')
    # Triune Seal center (the NMS brand): triangle joining three class-nodes in
    # the brand colors -- light-blue, ember, teal -- inside the rune rings.
    tri_angles = (-90, 30, 150)
    node_cols = ('#9be1ff', '#ff8a4a', '#49d0a0')
    tri = " ".join(f"{pt(62, a)[0]:.1f},{pt(62, a)[1]:.1f}" for a in tri_angles)
    nodes = "".join(
        f'<circle cx="{pt(62, a)[0]:.1f}" cy="{pt(62, a)[1]:.1f}" r="8.5" fill="{c}" opacity="0.22"/>'
        f'<circle cx="{pt(62, a)[0]:.1f}" cy="{pt(62, a)[1]:.1f}" r="4.4" fill="{c}"/>'
        for a, c in zip(tri_angles, node_cols))
    return ('<svg class="sigil" viewBox="0 0 200 200" fill="none">'
            '<g class="sig-out" stroke="#4db2ff" stroke-width="1">'
            '<circle cx="100" cy="100" r="95"/><circle cx="100" cy="100" r="80" stroke-dasharray="2 7"/>'
            + "".join(ticks) + '</g>'
            '<g class="sig-in" stroke="#8fd0ff" stroke-width="1.2">'
            '<circle cx="100" cy="100" r="62"/>'
            f'<polygon points="{tri}" opacity="0.85"/>'
            '<g stroke="none">' + nodes + '</g></g></svg>')


def _build_ptui_emblem():
    """The PTUI in-game emblem, ported to SVG.

    A faithful port of NMSTheme::DrawEmblem from the client DLL
    (the client DLL's NMSTheme.cpp) -- the mark on the all-in-one chat bar.
    Geometry, colours and motion are taken straight from that function so the
    site and the game show the same emblem:

        halo        three stacked AccentDeep #3474be discs at 1.55 / 1.30 / 1.10 r,
                    the outermost breathing on a 4.5s cycle
        disc        rgb(8,15,28) so the mark reads ON the panel, not through it
        tick ring   12 marks, every third long -- the detail that makes it read
                    as an instrument rather than a logo. Spins clockwise.
        rings       accent #8fd0ff at 0.99r and 0.74r
        triangle    0.60r, counter-rotating, filled faintly and outlined
        nodes       the three class-slot colours -- arcane blue, ember, jade --
                    each with two glow shells, a core, and an offset white
                    specular. Each breathes on its OWN phase so the trio
                    shimmers instead of blinking in unison.

    Rotation rates match the client exactly: 0.55 rad/s out, 0.38 rad/s back,
    i.e. 11.4s and 16.5s per revolution. CSS drives the motion off the
    .pt-out / .pt-in / .pt-halo / .pt-nN classes.
    """
    import math
    C = 100.0                 # viewBox centre
    R = 62.0                  # base radius; halos reach 1.55R = 96 and still fit

    ACC = '#8fd0ff'           # Accent()      = rgb(143,208,255)
    DEEP = '#3474be'          # AccentDeep()  = rgb(52,116,190)
    SLOTS = ('#2892d8', '#f0621f', '#1fa878')   # kSlot1 blue, kSlot2 ember, kSlot3 jade

    def pt(r, a_deg):
        a = math.radians(a_deg)
        return (C + r * math.cos(a), C + r * math.sin(a))

    # --- 12 ticks, every third long (the C++ major/minor split) ---
    ticks = []
    for i in range(12):
        a = i * 30.0
        major = (i % 3) == 0
        x1, y1 = pt(R * (0.80 if major else 0.88), a)
        x2, y2 = pt(R * 0.99, a)
        ticks.append(
            '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke-opacity="%s" stroke-width="%s"/>'
            % (x1, y1, x2, y2, '.86' if major else '.43', '2.0' if major else '1.3'))

    # --- counter-rotating triangle + its three nodes ---
    tri = [pt(R * 0.60, -90 + i * 120) for i in range(3)]
    tri_pts = " ".join("%.1f,%.1f" % p for p in tri)

    # SCALE NOTE: the client draws this at roughly 20px, where node_r = 0.17r and
    # an alpha-24 halo read as a soft aura. Ported literally to an 11rem web
    # render the same ratios become a solid blue donut and three discs that
    # swallow the triangle. The RATIOS below are therefore tuned for the large
    # size -- the shape, colours, tick pattern and motion are the port; the node
    # and halo weights are what make it read the same to the eye.
    nr = R * 0.095
    # ImGui fakes a glow by stacking flat translucent discs -- fine at 20px where
    # they blur together, but in SVG at 11rem they render as hard-edged rings.
    # A radial gradient is the honest equivalent: one shell that actually fades.
    grads = []
    nodes = []
    for i, (x, y) in enumerate(tri):
        col = SLOTS[i]
        grads.append(
            '<radialGradient id="ptn%d">'
            '<stop offset="0" stop-color="%s" stop-opacity=".55"/>'
            '<stop offset=".38" stop-color="%s" stop-opacity=".26"/>'
            '<stop offset="1" stop-color="%s" stop-opacity="0"/>'
            '</radialGradient>' % (i + 1, col, col, col))
        nodes.append(
            '<g class="pt-node pt-n%d">'
            '<circle cx="%.1f" cy="%.1f" r="%.1f" fill="url(#ptn%d)" class="pt-g1"/>'
            '<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s"/>'
            '<circle cx="%.1f" cy="%.1f" r="%.1f" fill="#ffffff" fill-opacity=".8"/>'
            '</g>'
            % (i + 1,
               x, y, nr * 3.4, i + 1,
               x, y, nr, col,
               x - nr * 0.28, y - nr * 0.28, nr * 0.36))

    return (
        '<svg class="ptui-emblem" viewBox="0 0 200 200" fill="none">'
        '<defs>'
        '<radialGradient id="pthalo">'
        '<stop offset=".52" stop-color="%s" stop-opacity="0"/>'
        '<stop offset=".72" stop-color="%s" stop-opacity=".22"/>'
        '<stop offset="1" stop-color="%s" stop-opacity="0"/>'
        '</radialGradient>%s</defs>'
        # breathing halo -- one gradient shell, not stacked discs
        '<circle class="pt-h3" cx="100" cy="100" r="%.1f" fill="url(#pthalo)"/>'
        # dark disc, so the emblem reads ON the panel rather than through it
        '<circle cx="100" cy="100" r="%.1f" fill="#080f1c" fill-opacity=".82"/>'
        # spinning tick ring + the two accent rings
        '<g class="pt-out" stroke="%s" stroke-linecap="round">%s</g>'
        '<circle cx="100" cy="100" r="%.1f" fill="none" stroke="%s" stroke-opacity=".82" stroke-width="1.7"/>'
        '<circle cx="100" cy="100" r="%.1f" fill="none" stroke="%s" stroke-opacity=".27" stroke-width="1.2"/>'
        # counter-rotating triangle + nodes
        '<g class="pt-in">'
        '<polygon points="%s" fill="#5a82c8" fill-opacity=".10"/>'
        '<polygon points="%s" fill="none" stroke="#8fa5eb" stroke-opacity=".75" stroke-width="1.5"'
        ' stroke-linejoin="round"/>'
        '%s</g></svg>'
    ) % (DEEP, ACC, ACC, "".join(grads),
         R * 1.55,
         R * 0.99,
         ACC, "".join(ticks),
         R * 0.99, ACC,
         R * 0.74, ACC,
         tri_pts, tri_pts, "".join(nodes))


SIGIL_SVG = _build_sigil()
PTUI_EMBLEM_SVG = _build_ptui_emblem()

@bp.route("/leaderboards", methods=['GET'])
def leaderboards():
    """Players page = the season running now (live); ?season=season-1 opens a closed season, frozen.
    Top Players, AA and Gear all belong to the season (season_top_players.season_boards)."""
    import season_top_players as stp
    try:
        season = lbr.resolve_season(request.args.get('season'))
        seasons = lbr.list_seasons()
    except Exception:
        season, seasons = None, []
    data = stp.season_boards(season)
    return render_template(
        'leaderboards.html',
        top_players=data['players'],
        top_aa=data['aa'],
        top_gear=data['gear'],
        season=season, seasons=seasons,
        bosses=lbr.list_bosses(),
    )

@bp.route("/characters", methods=['GET', 'POST'])
def character_browser():
    if request.method == 'POST':
        search_name = request.form.get('name', '').strip()
        # Trio lookup: up to three class dropdowns -> combined GestaltClasses bitmask.
        selected_bits = 0
        for _k in ('class_1', 'class_2', 'class_3'):
            _v = request.form.get(_k, 'All')
            if _v and _v != 'All':
                selected_bits |= 1 << (int(_v) - 1)
        # Advanced options
        search_race = request.form.get('race', 'All')
        min_level = request.form.get('min_level', '').strip()
        max_level = request.form.get('max_level', '').strip()

        with get_db(logic.engine) as session:
            CharData = logic.Base.classes.character_data
            GuildMembers = logic.Base.classes.guild_members
            Guilds = logic.Base.classes.guilds
            
            query = session.query(
                CharData.id, CharData.name, CharData.level, getattr(CharData, 'class'), CharData.race, Guilds.name.label('guild_name'), CharData.anon
            ).outerjoin(
                GuildMembers, GuildMembers.char_id == CharData.id
            ).outerjoin(
                Guilds, Guilds.id == GuildMembers.guild_id
            ).filter(
                CharData.gm == 0,
                CharData.anon != 1,        # show normal + roleplay (2); hide only true anon (1)
                getattr(CharData, 'deleted_at').is_(None)
            )

            if search_name:
                query = query.filter(CharData.name.like(f"%{search_name}%"))
            if selected_bits:
                # Match characters whose gestalt trio contains ALL chosen classes.
                # (A trio has exactly 3 classes, so picking 3 yields that exact trio.)
                match_rows = session.execute(text(
                    "SELECT character_id FROM data_buckets WHERE `key` = 'GestaltClasses' "
                    "AND (CAST(value AS UNSIGNED) & :bits) = :bits"), {'bits': selected_bits}).all()
                query = query.filter(CharData.id.in_([row[0] for row in match_rows] or [-1]))
            if search_race != 'All':
                query = query.filter(CharData.race == int(search_race))
            if min_level.isdigit():
                query = query.filter(CharData.level >= int(min_level))
            if max_level.isdigit():
                query = query.filter(CharData.level <= int(max_level))

            # Rank EVERY match first, then keep the top 200. Taking the first 200
            # alphabetically and sorting those meant a top-ranked character whose
            # name fell past the 200th match never appeared at all.
            rmap = get_player_rank_map()
            rows = query.all()
            rows.sort(key=lambda r: ((rmap.get(r[0]) or {}).get('score') is None,
                                     -((rmap.get(r[0]) or {}).get('score') or 0),
                                     r[1]))
            rows = rows[:200]
            cmap = gestalt_class_display(session, [r[0] for r in rows])
            results = []
            for r in rows:
                results.append({
                    'id': r[0],
                    'name': r[1],
                    'level': r[2],
                    'class_name': cmap.get(r[0], '?'),
                    'race_name': RACES.get(r[4], f"Unknown ({r[4]})"),
                    'guild_name': r[5] or "",
                    'is_rp': r[6] == 2,      # roleplay badge
                })
        # Annotate with server-wide rank + player score, then rank within this
        # result set (the "trio rank" when a trio is searched).
        for r in results:
            info = rmap.get(r['id']) or {}
            r['score'] = info.get('score')
            r['overall_rank'] = info.get('rank')
            r['trio_rank'] = info.get('trio_rank')     # rank within their own exact trio
            r['trio_size'] = info.get('trio_size')
            r['parse_score'] = info.get('parse_score')
            r['parse_trophy'] = info.get('parse_trophy')
        results.sort(key=lambda r: (r.get('score') is None, -(r.get('score') or 0)))
        return render_template('character_browser.html', results=results, search_params=request.form,
                               CLASSES=CLASSES, RACES=RACES, is_trio=bool(selected_bits))
    else:
        return render_template('character_browser.html', results=None, CLASSES=CLASSES, RACES=RACES)

def build_profile(char_id):
    """Assemble one character's full sheet payload -- profile dict, gear map,
    tooltip data, gear-score maths, and rank info -- or None if the character is
    missing or private (GM / anonymous / deleted). Shared by the profile page and
    the side-by-side compare view so both read identical numbers."""
    with get_db(logic.engine) as session:
        CharData = logic.Base.classes.character_data
        GuildMembers = logic.Base.classes.guild_members
        Guilds = logic.Base.classes.guilds
        Inventory = logic.Base.classes.inventory
        Item = logic.Item

        char = session.query(CharData).filter(CharData.id == char_id).first()
        if not char or char.gm > 0 or char.anon == 1 or getattr(char, 'deleted_at') is not None:
            return None

        guild_info = session.query(Guilds.name, GuildMembers.rank).outerjoin(
            GuildMembers, GuildMembers.guild_id == Guilds.id
        ).filter(GuildMembers.char_id == char.id).first()
        
        guild_name = guild_info[0] if guild_info else ""
        guild_rank = guild_info[1] if guild_info else ""
        
        inv_rows = session.query(Inventory).filter(Inventory.character_id == char.id).filter(Inventory.slot_id.between(0, 22)).all()
        
        item_ids = set()
        for r in inv_rows:
            if r.item_id: item_ids.add(r.item_id)
            if r.augment_one: item_ids.add(r.augment_one)
            if r.augment_two: item_ids.add(r.augment_two)
            if r.augment_three: item_ids.add(r.augment_three)
            if r.augment_four: item_ids.add(r.augment_four)
            if r.augment_five: item_ids.add(r.augment_five)
            if r.augment_six: item_ids.add(r.augment_six)
            
        items_by_id = {}
        if item_ids:
            items = session.query(Item).filter(Item.id.in_(item_ids)).all()
            for item in items:
                items_by_id[item.id] = {
                    'id': item.id,
                    'name': item.Name,
                    'icon': item.icon
                }
                
        gear = {}
        for slot_id, slot_name in SLOTS.items():
            gear[slot_id] = {
                'slot_name': slot_name,
                'item': None,
                'augments': []
            }
            
        for r in inv_rows:
            if r.slot_id in gear:
                if r.item_id in items_by_id:
                    gear[r.slot_id]['item'] = items_by_id[r.item_id]
                augs = []
                for aug_id in [r.augment_one, r.augment_two, r.augment_three, r.augment_four, r.augment_five, r.augment_six]:
                    if aug_id and aug_id in items_by_id:
                        augs.append(items_by_id[aug_id])
                gear[r.slot_id]['augments'] = augs
                
        profile = {
            'id': char.id,
            'name': char.name,
            'is_rp': char.anon == 2,          # roleplay -> "RP" badge on the sheet
            'last_name': char.last_name,
            'level': char.level,
            'class_name': gestalt_class_display(session, [char.id]).get(char.id, '?'),
            'race_name': RACES.get(char.race, f"Unknown ({char.race})"),
            'gender': GENDERS.get(char.gender, "Unknown"),
            'deity': DEITIES.get(char.deity, f"Unknown Deity ({char.deity})"),
            'time_played_hours': round(char.time_played / 60.0, 1),
            'aa_points_spent': char.aa_points_spent,
            'pvp_kills': char.pvp_kills,
            'pvp_deaths': char.pvp_deaths,
            'pvp_career_points': char.pvp_career_points,
            'guild_name': guild_name,
            'guild_rank': guild_rank,
            'stats': {
                'str': char.str,
                'sta': char.sta,
                'dex': char.dex,
                'agi': char.agi,
                'int': char.int,
                'wis': char.wis,
                'cha': char.cha,
                'hp': char.cur_hp,
                'mana': char.mana,
                'end': char.endurance
            }
        }

        # ---- gear score, per-item stats, heroic/derived sums (redesigned profile) ----
        # Worn items drive the gear score; worn items PLUS their augments drive the
        # displayed stat sums (heroic, AC, resists, attack), to match the in-game totals.
        worn = [(sid, g['item']['id']) for sid, g in gear.items() if g['item']]
        worn_ids = list({iid for _, iid in worn})
        aug_ids = list({a['id'] for _, g in gear.items() if g['item'] for a in g['augments']})
        all_ids = list(set(worn_ids) | set(aug_ids))
        item_stats = {}
        if all_ids:
            _cols = ",".join("`%s`" % c for c in GEAR_STAT_COLS)
            _q = (text(f"SELECT id,{_cols} FROM items WHERE id IN :ids")
                  .bindparams(bindparam('ids', expanding=True)))
            for r in session.execute(_q, {'ids': all_ids}).mappings().all():
                item_stats[r['id']] = {k: int(r[k] or 0) for k in GEAR_STAT_COLS}

        # resolve item effect spell ids (worn / focus / proc / click) to names for tooltips
        effect_ids = set()
        for iid in all_ids:
            st = item_stats.get(iid, {})
            for col in ('worneffect', 'focuseffect', 'proceffect', 'clickeffect'):
                if st.get(col, 0) > 0:
                    effect_ids.add(st[col])
        spell_names = {}
        if effect_ids:
            _sq = (text("SELECT id, name FROM spells_new WHERE id IN :ids")
                   .bindparams(bindparam('ids', expanding=True)))
            for r in session.execute(_sq, {'ids': list(effect_ids)}).all():
                spell_names[r[0]] = r[1]

        with get_db(lb_engine) as _lb:
            ref = get_reference_max(_lb) or 0
            raw_map = {}
            if all_ids:  # worn items AND their augments both carry score
                for r in (_lb.query(ItemScore.item_id, ItemScore.raw_score)
                          .filter(ItemScore.item_id.in_(all_ids)).all()):
                    raw_map[r[0]] = float(r[1])

        total_raw = 0.0
        heroic = {k: 0 for k in ('str', 'sta', 'agi', 'dex', 'wis', 'int', 'cha')}
        gear_attr = {k: 0 for k in heroic}
        res = {k: 0 for k in ('cr', 'fr', 'mr', 'pr', 'dr')}
        hres = {k: 0 for k in ('cr', 'fr', 'mr', 'pr', 'dr')}
        worn_ac = attack = haste = gear_hp = gear_mana = gear_end = 0
        tip_data = {}
        for sid, iid in worn:
            st = item_stats.get(iid, {})
            # gear score: worn item plus every augment socketed in it
            raw = raw_map.get(iid, 0.0) + sum(raw_map.get(a['id'], 0.0)
                                              for a in gear[sid]['augments'])
            total_raw += raw
            pts = round(raw / ref * 1000, 1) if ref else 0.0
            gear[sid]['score'] = {'raw': round(raw, 1), 'pts': pts}
            # stat sums: the worn item plus every augment socketed in it
            contributors = [st] + [item_stats.get(a['id'], {}) for a in gear[sid]['augments']]
            for cst in contributors:
                for k in heroic:
                    heroic[k] += cst.get('heroic_' + k, 0)
                    gear_attr[k] += cst.get('a' + k, 0)
                for k in res:
                    res[k] += cst.get(k, 0)
                    hres[k] += cst.get('heroic_' + k, 0)
                worn_ac += cst.get('ac', 0)
                attack += cst.get('attack', 0)
                gear_hp += cst.get('hp', 0)
                gear_mana += cst.get('mana', 0)
                gear_end += cst.get('endur', 0)
                haste = max(haste, cst.get('haste', 0))
            effects = []
            for _lab, _col in (('Worn', 'worneffect'), ('Focus', 'focuseffect'),
                               ('Proc', 'proceffect'), ('Click', 'clickeffect')):
                _eid = st.get(_col, 0)
                if _eid > 0:
                    effects.append({'type': _lab, 'name': spell_names.get(_eid, 'Spell #%d' % _eid)})
            augs = [{'name': a['name'], 'stats': item_stats.get(a['id'], {})}
                    for a in gear[sid]['augments']]
            tip_data[gear[sid]['slot_name']] = {
                'name': gear[sid]['item']['name'], 'raw': round(raw, 1),
                'pts': pts, 'stats': st, 'effects': effects, 'augs': augs}

        focus = {}
        for sid, iid in worn:
            sources = [(gear[sid]['item']['name'], item_stats.get(iid, {}))] + \
                      [(a['name'], item_stats.get(a['id'], {})) for a in gear[sid]['augments']]
            for src_name, cst in sources:
                fid = cst.get('focuseffect', 0)
                if fid > 0:
                    f = focus.setdefault(fid, {'name': spell_names.get(fid, 'Spell #%d' % fid), 'id': fid,
                                               'sources': []})
                    f['sources'].append({'item': src_name, 'slot': gear[sid]['slot_name']})
        profile['focus_effects'] = sorted(focus.values(), key=lambda f: f['name'])

        # pet gear: what each saved pet has equipped (character_pet_inventory; pet 0 = current,
        # 1 = suspended, 2/3 = permanent pet slots, anything else = another saved pet)
        pet_labels = {0: 'Current pet', 1: 'Suspended pet', 2: 'Permanent pet 1', 3: 'Permanent pet 2'}
        pet_rows = session.execute(text(
            "SELECT p.pet, p.slot, i.id, i.Name, i.icon FROM character_pet_inventory p "
            "JOIN items i ON i.id = p.item_id WHERE p.char_id = :c ORDER BY p.pet, p.slot"),
            {'c': char.id}).all()
        pets = {}
        for pet, slot_id, iid, iname, icon in pet_rows:
            pets.setdefault(pet, {'label': pet_labels.get(pet, 'Saved pet'), 'gear': []})['gear'].append(
                {'id': iid, 'name': iname, 'icon': icon})
        profile['pet_gear'] = [pets[k] for k in sorted(pets)]

        # vault lockers (optional custom table character_vaults): slots 61-80 = click locker (their clicky buffs are kept on
        # the character), 81-83 = proc locker (primary / secondary / range weapon procs). Only the buff
        # or proc applies, so the list shows the effect, not the item's stats. Not part of gear score.
        try:
            locker_rows = session.execute(text(
                "SELECT v.vault_slot, i.id, i.Name, i.icon, i.clickeffect, i.proceffect, "
                "sc.name AS click_name, sp.name AS proc_name "
                "FROM character_vaults v JOIN items i ON i.id = v.item_id "
                "LEFT JOIN spells_new sc ON sc.id = i.clickeffect "
                "LEFT JOIN spells_new sp ON sp.id = i.proceffect "
                "WHERE v.character_id = :c AND v.bag_index = -1 AND v.vault_slot BETWEEN 61 AND 83 "
                "ORDER BY v.vault_slot"), {'c': char.id}).mappings().all()
        except Exception:  # table absent on a stock PEQ database
            session.rollback()
            locker_rows = []
        proc_slot = {81: 'Primary', 82: 'Secondary', 83: 'Range'}
        profile['click_locker'] = [{'id': r['id'], 'name': r['Name'], 'icon': r['icon'],
                                    'effect': r['click_name'] or ''}
                                   for r in locker_rows if r['vault_slot'] <= 80]
        profile['proc_locker'] = [{'id': r['id'], 'name': r['Name'], 'icon': r['icon'],
                                   'effect': r['proc_name'] or '', 'slot': proc_slot[r['vault_slot']]}
                                  for r in locker_rows if r['vault_slot'] >= 81]

        maxpts = max((gear[sid]['score']['pts'] for sid, _ in worn), default=1) or 1
        profile['gear_parts'] = None
        if ref and _season2_scoring():
            share = effect_score.effect_shares(logic.engine, [char.id]).get(char.id, 0.0)
            total, g_part, e_part = effect_score.season2_score(total_raw, ref, share)
            profile['gear_score'] = total
            profile['gear_parts'] = {'gear': g_part, 'effects': e_part}
        else:
            profile['gear_score'] = normalize(total_raw, ref) if ref else 0
        tiers = gear_tiers()
        profile['gear_tier'] = next(t for cut, t in tiers if profile['gear_score'] >= cut)
        profile['gear_pct'] = round(profile['gear_score'] / 10.0, 1)
        # tier ladder + progress toward the next tier (for the footer explainer)
        ladder = sorted(tiers)                           # ascending by cutoff
        profile['tier_ladder'] = [{'name': t, 'cut': c} for c, t in ladder]
        nxt = next(((c, t) for c, t in ladder if c > profile['gear_score']), None)
        cur_cut = next(c for c, t in reversed(ladder) if profile['gear_score'] >= c)
        if nxt:
            profile['next_tier'] = {'name': nxt[1], 'cut': nxt[0],
                                    'need': nxt[0] - profile['gear_score']}
            span = max(1, nxt[0] - cur_cut)
            profile['tier_progress'] = round((profile['gear_score'] - cur_cut) / span * 100)
        else:
            profile['next_tier'] = None
            profile['tier_progress'] = 100
        profile['heroic'] = heroic
        profile['gear_attr'] = gear_attr
        profile['resists'] = res
        profile['heroic_resists'] = hres
        profile['worn_ac'] = worn_ac
        profile['attack'] = attack
        profile['haste'] = haste
        profile['gear_hp'] = gear_hp
        profile['gear_mana'] = gear_mana
        profile['gear_end'] = gear_end

    # Trio / overall ranking (cached map; never let a ranking hiccup break the sheet)
    try:
        _rmap = get_player_rank_map()
        rank_info = _rmap.get(int(char_id))
        if rank_info:
            rank_info = dict(rank_info, total=len(_rmap))
    except Exception:
        rank_info = None

    return {'profile': profile, 'gear': gear, 'tip_data': tip_data,
            'maxpts': maxpts, 'rank_info': rank_info}


def _viewer_owns(char_id):
    """True when the signed-in Discord user's linked accounts include this
    character. Gates owner-only pages (the trader ledger)."""
    try:
        from eqdb import _session_user
        du = _session_user()
        if not du:
            return False
        return any(c['id'] == char_id for c in get_linked_characters(du['id']))
    except Exception:
        return False


@bp.route("/character/<int:char_id>", methods=['GET'])
def character_profile(char_id):
    bundle = build_profile(char_id)
    if not bundle:
        flash("Character not found or private.")
        return redirect(url_for('.character_browser'))
    # Leaderboard parse summary + #1-parser crown are OPTIONAL adornments on the
    # gear card -- never let a leaderboard-DB hiccup (down, mid-migration, missing
    # column) 500 the whole profile. Fail soft to no parse block.
    parse = None
    is_top_parser = False
    try:
        parse = lbr.get_character_profile(char_id)
        if parse:
            _top = lbr.get_character_rankings(limit=1)
            is_top_parser = bool(_top and _top[0]['character_id'] == char_id)
    except Exception:
        parse, is_top_parser = None, False
    # Trained AAs against everything this character's classes can train now.
    # Optional like the parse block -- never 500 the profile over it.
    try:
        import aa_info
        with get_db(logic.engine) as s:
            mask = s.execute(text(
                "SELECT value FROM data_buckets WHERE `key` = 'GestaltClasses' "
                "AND character_id = :c"), {'c': char_id}).scalar()
            if not mask:
                cls = s.execute(text("SELECT `class` FROM character_data WHERE id = :c"),
                                {'c': char_id}).scalar()
                mask = (1 << (int(cls) - 1)) if cls else 0
        aas = aa_info.character_aas(char_id, int(mask or 0))
    except Exception:
        aas = None
    return render_template('character_profile.html', profile=bundle['profile'],
                           gear=bundle['gear'], slot_layout=SLOT_LAYOUT,
                           tip_data=bundle['tip_data'], sigil=SIGIL_SVG,
                           maxpts=bundle['maxpts'], rank_info=bundle['rank_info'],
                           parse=parse, is_top_parser=is_top_parser, aas=aas,
                           is_owner=_viewer_owns(char_id))


@bp.route("/character/<int:char_id>/progression", methods=['GET'])
def character_progression(char_id):
    """Plane of Power / Plane of Time flag progression, read from the character's
    data_buckets progress flags. Never let a bucket read 500 the page."""
    bundle = build_profile(char_id)
    if not bundle:
        flash("Character not found or private.")
        return redirect(url_for('.character_browser'))
    try:
        prog = pop_progress.compute(char_id, bundle['profile'])
    except Exception:
        prog = None
    # LDoN reads three separate stores and none of them exist until the
    # expansion ships, so it fails independently of the PoP road above.
    try:
        import ldon_progress      # optional: server-specific LDoN content (private extension)
        ldon = ldon_progress.compute(char_id, bundle['profile'])
    except Exception:
        ldon = None
    return render_template('character_progression.html', profile=bundle['profile'],
                           prog=prog, ldon=ldon, sigil=SIGIL_SVG)


def _char_compare(a, b):
    """Build the diff rows + slot-by-slot gear rows for two profile bundles.
    Either side may be None (not yet picked); winner is only computed when both
    values are present. 'better' says whether higher or lower wins (ranks are
    lower-is-better)."""
    pa = a['profile'] if a else None
    pb = b['profile'] if b else None
    ra = (a.get('rank_info') or {}) if a else {}
    rb = (b.get('rank_info') or {}) if b else {}

    def dig(p, *keys):
        cur = p
        for k in keys:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
            if cur is None:
                return None
        return cur

    def row(label, av, bv, better='higher', fmt=None):
        def disp(v):
            if v is None:
                return None
            return '{:,}'.format(v) if fmt == 'comma' else v
        win = None
        if av is not None and bv is not None:
            win = 'tie' if av == bv else ('a' if (av > bv) == (better == 'higher') else 'b')
        return {'label': label, 'a': disp(av), 'b': disp(bv), 'win': win}

    def resist(k):
        av = (dig(pa, 'resists', k) or 0) + (dig(pa, 'heroic_resists', k) or 0) if pa else None
        bv = (dig(pb, 'resists', k) or 0) + (dig(pb, 'heroic_resists', k) or 0) if pb else None
        return av, bv

    sections = [
        {'name': 'Overview', 'rows': [
            row('Level', dig(pa, 'level'), dig(pb, 'level')),
            row('Gear Score', dig(pa, 'gear_score'), dig(pb, 'gear_score')),
            row('Player Score', ra.get('score'), rb.get('score')),
            row('AA Spent', dig(pa, 'aa_points_spent'), dig(pb, 'aa_points_spent'), fmt='comma'),
        ]},
        {'name': 'Rankings (lower is better)', 'rows': [
            row('Server Rank', ra.get('rank'), rb.get('rank'), better='lower'),
            row('Trio Rank', ra.get('trio_rank'), rb.get('trio_rank'), better='lower'),
            row('Gear Rank', ra.get('gear_rank'), rb.get('gear_rank'), better='lower'),
        ]},
        {'name': 'Vitals & Combat', 'rows': [
            row('HP', dig(pa, 'stats', 'hp'), dig(pb, 'stats', 'hp'), fmt='comma'),
            row('Mana', dig(pa, 'stats', 'mana'), dig(pb, 'stats', 'mana'), fmt='comma'),
            row('Endurance', dig(pa, 'stats', 'end'), dig(pb, 'stats', 'end'), fmt='comma'),
            row('Worn AC', dig(pa, 'worn_ac'), dig(pb, 'worn_ac'), fmt='comma'),
            row('Attack', dig(pa, 'attack'), dig(pb, 'attack'), fmt='comma'),
            row('Haste %', dig(pa, 'haste'), dig(pb, 'haste')),
        ]},
        {'name': 'From Gear (incl. augs)', 'rows': [
            row('Gear HP', dig(pa, 'gear_hp'), dig(pb, 'gear_hp'), fmt='comma'),
            row('Gear Mana', dig(pa, 'gear_mana'), dig(pb, 'gear_mana'), fmt='comma'),
            row('Gear End', dig(pa, 'gear_end'), dig(pb, 'gear_end'), fmt='comma'),
        ]},
        {'name': 'Heroic Stats', 'rows': [
            row(k.upper(), dig(pa, 'heroic', k), dig(pb, 'heroic', k))
            for k in ('str', 'sta', 'agi', 'dex', 'wis', 'int', 'cha')
        ]},
        {'name': 'Resists (base + heroic)', 'rows': [
            row(lab, *resist(k)) for k, lab in
            (('mr', 'Magic'), ('fr', 'Fire'), ('cr', 'Cold'), ('pr', 'Poison'), ('dr', 'Disease'))
        ]},
    ]

    ga = a['gear'] if a else {}
    gb = b['gear'] if b else {}
    slots = []
    for sid, _area in SLOT_LAYOUT:
        sa, sb = ga.get(sid) or {}, gb.get(sid) or {}
        ia, ib = sa.get('item'), sb.get('item')
        apts = (sa.get('score') or {}).get('pts') if ia else None
        bpts = (sb.get('score') or {}).get('pts') if ib else None
        win = None
        if apts is not None and bpts is not None:
            win = 'tie' if apts == bpts else ('a' if apts > bpts else 'b')
        slots.append({'slot': SLOTS.get(sid, ''),
                      'a_name': ia['name'] if ia else None, 'a_id': ia['id'] if ia else None, 'a_pts': apts,
                      'b_name': ib['name'] if ib else None, 'b_id': ib['id'] if ib else None, 'b_pts': bpts,
                      'win': win})
    return {'sections': sections, 'slots': slots}


@bp.route("/character/compare", methods=['GET'])
def character_compare():
    def _id(param):
        v = (request.args.get(param) or '').strip()
        return int(v) if v.isdigit() else None
    aid, bid = _id('a'), _id('b')
    a = build_profile(aid) if aid else None
    b = build_profile(bid) if bid else None
    cmp_data = _char_compare(a, b) if (a and b) else None
    return render_template('character_compare.html', a=a, b=b, aid=aid, bid=bid,
                           cmp=cmp_data, sigil=SIGIL_SVG)


@bp.route("/character/compare/find", methods=['GET'])
def character_compare_find():
    """JSON name search for the compare-slot picker. Public characters only."""
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify([])
    with get_db(logic.engine) as session:
        CharData = logic.Base.classes.character_data
        rows = (session.query(CharData.id, CharData.name, CharData.level, CharData.race)
                .filter(CharData.name.like(f"%{q}%"), CharData.gm == 0, CharData.anon != 1,        # show normal + roleplay (2); hide only true anon (1)
                        getattr(CharData, 'deleted_at').is_(None))
                .order_by(CharData.name).limit(20).all())
        cmap = gestalt_class_display(session, [r[0] for r in rows])
    return jsonify([{'id': r[0], 'name': r[1], 'level': r[2],
                     'class': cmap.get(r[0], '?'), 'race': RACES.get(r[3], '')} for r in rows])
