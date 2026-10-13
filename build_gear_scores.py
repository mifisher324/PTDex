"""Offline job: compute the gear-score contribution of every wearable content
item and store it in lb_item_score, plus the calibration constants in lb_meta.

Run after create_leaderboard_db.py, and again whenever the content item database
changes or the weights in gear_score.py are retuned:

    python build_gear_scores.py

Reads the content DB from configuration.ini [database]; writes to the leaderboard
DB from [leaderboard_database]. The website's ingest path then reads only
lb_item_score (never joins the ~230k-row content items table live).
"""
import configparser
import json
import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import gear_score as gs
from create_leaderboard_db import engine as lb_engine, Base, Meta

here = os.path.dirname(__file__)


def _config():
    cfg = configparser.RawConfigParser()
    cfg.read_file(open(os.path.join(here, 'configuration.ini')))
    return cfg


def _content_engine(cfg=None):
    cfg = cfg or _config()
    driver = os.environ.get('DB_DRIVER', cfg.get('database', 'driver', fallback='')) or 'mysql+pymysql://'
    if '://' not in driver:
        driver += '://'
    user = os.environ.get('DB_USER', cfg.get('database', 'user', fallback=''))
    pw = os.environ.get('DB_PASSWORD', cfg.get('database', 'password', fallback=''))
    db = os.environ.get('DB_DATABASE', cfg.get('database', 'database', fallback=''))
    host = os.environ.get('DB_HOST', cfg.get('database', 'host', fallback=''))
    port = os.environ.get('DB_PORT', cfg.get('database', 'port', fallback=''))
    return create_engine(f'{driver}{user}:{pw}@{host}:{port}/{db}')


# item_files/ basenames indexed by expansion (0=Classic .. 19=RoF), matching the
# Custom:AdvLootExpansionLootLimit numbering. Each era contributes {name}.txt,
# {name}_ts.txt, {name}_special.txt where those files exist. Only eras up to the
# server's current expansion are included, so this tracks progression to GoD etc.
EXPANSION_ERA_BASENAMES = [
    'Classic', 'Kunark', 'Velious', 'Luclin', 'Planes', 'LoY', 'LDoN', 'GoD',
    'OoW', 'DoN', 'DoDH', 'PoR', 'TSS', 'TBS', 'SoF', 'SoD', 'UF', 'HoT', 'VoA', 'RoF',
]

# rule_values rows that can carry the server's current content expansion, tried in
# order. Both use the 0=Classic 1=Kunark .. 7=GoD numbering. A value < 0 means the
# rule is set to "ALL"/disabled and is skipped. Override with [triune] expansion_rule.
EXPANSION_RULES = ['Expansion:CurrentExpansion', 'Custom:AdvLootExpansionLootLimit']


def get_server_expansion(content_engine, cfg):
    """Read the server's current content expansion from rule_values so the gear
    ceiling tracks progression (PoP -> GoD -> ...) automatically when staff bump
    the rule. Returns (expansion_int, source_label). Falls back to [triune] expansion."""
    rule_names = []
    override = cfg.get('triune', 'expansion_rule', fallback='').strip()
    if override:
        rule_names.append(override)
    rule_names += EXPANSION_RULES

    with content_engine.connect() as c:
        for rn in rule_names:
            row = c.execute(text("SELECT rule_value FROM rule_values WHERE rule_name = :n"),
                            {'n': rn}).first()
            if row is None:
                continue
            try:
                value = int(row[0])
            except (TypeError, ValueError):
                continue
            if value >= 0:                 # -1 = ALL/disabled -> not a usable era index
                return value, rn
    return int(cfg.get('triune', 'expansion', fallback='5')), 'config:[triune]expansion'


def _attainable_base_ids(content_engine, max_expansion):
    """Base item ids (< 1,000,000, i.e. the item modulo its tier offset) that are
    obtainable at or below ``max_expansion``: dropped by an NPC in an in-era zone,
    or listed as a quest/tradeskill/special item in item_files/. Enchanted (+1M)
    and Legendary (+2M) copies share the same base id, so callers test
    ``item_id % 1000000 in <this set>`` to cover all three tiers."""
    ids = set()
    drop_q = text(
        "SELECT DISTINCT lde.item_id "
        "FROM zone z "
        "JOIN npc_types n ON (n.id DIV 1000) = z.zoneidnumber "
        "JOIN loottable_entries lte ON lte.loottable_id = n.loottable_id "
        "JOIN lootdrop_entries lde ON lde.lootdrop_id = lte.lootdrop_id "
        "WHERE z.expansion BETWEEN 0 AND :m AND lde.item_id > 0")
    with content_engine.connect() as c:
        for (item_id,) in c.execute(drop_q, {'m': max_expansion}):
            if item_id:
                ids.add(int(item_id) % 1000000)

    for basename in EXPANSION_ERA_BASENAMES[:max_expansion + 1]:
        for suffix in ('', '_ts', '_special'):
            p = os.path.join(here, 'item_files', f'{basename}{suffix}.txt')
            if os.path.exists(p):
                with open(p) as fh:
                    for line in fh.read().split('\n'):
                        line = line.strip()
                        if line.isdigit():
                            ids.add(int(line) % 1000000)
    # Monstrous Mayhem sets are AWARDED (Monster Mission rewards) -- never dropped or sold -- so the
    # drop/era scan above misses them. They are the top LDoN-era gear and must count toward the ceiling.
    # Base ids 600100-600291 (192); Enchanted (+1,000,000) and Legendary (+2,000,000) share the base id.
    if max_expansion >= 6:
        ids.update(range(600100, 600292))
    return ids


def _tier(item_id):
    return 'legendary' if item_id >= 2000000 else 'enchanted' if item_id >= 1000000 else 'base'


def _best_per_slot(scored, pred):
    """Sum of the best item score in each equip slot (subject to pred), plus the
    chosen item ids. Defines the normalization ceiling / BiS reference sets."""
    total = 0.0
    ids = []
    for bit in gs.SCORED_SLOTS:
        mask = 1 << bit
        best, best_id = 0.0, None
        for item_id, slots, raw in scored:
            if (slots & mask) and pred(item_id) and raw > best:
                best, best_id = raw, item_id
        total += best
        if best_id:
            ids.append(best_id)
    return total, ids


def main():
    Base.metadata.create_all(lb_engine)   # ensure lb_item_score + lb_meta exist
    cfg = _config()
    content_engine = _content_engine(cfg)
    max_expansion, exp_source = get_server_expansion(content_engine, cfg)
    print(f'server expansion: {max_expansion} (from {exp_source})')

    # 1. Score every wearable content item (worn gear AND augments -- augs have
    #    slots > 0 too). Track augtype + socket counts for the ceiling below.
    select_cols = ('id, slots, augtype, loregroup, '
                   'augslot1type, augslot2type, augslot3type, '
                   'augslot4type, augslot5type, augslot6type, '
                   + ', '.join(gs.NEEDED_COLUMNS))
    scored = []       # WORN items only: (item_id, slots, raw_score)
    aug_scored = []   # augments: (item_id, raw_score, augtype_bitmask, is_lore)
    sockets = {}      # item_id -> list of augment socket TYPE values on it
    with content_engine.connect() as c:
        for row in c.execute(text(f'SELECT {select_cols} FROM items WHERE slots > 0')):
            m = row._mapping
            raw = gs.score_item(m)
            if m['augtype'] and m['augtype'] > 0:
                aug_scored.append((m['id'], raw, int(m['augtype']),
                                   bool(m['loregroup'])))
            else:
                scored.append((m['id'], m['slots'], raw))
            sockets[m['id']] = [int(m[k]) for k in ('augslot1type', 'augslot2type', 'augslot3type',
                                                    'augslot4type', 'augslot5type', 'augslot6type')
                                if m[k] and m[k] > 0]
    print(f'scored {len(scored):,} worn items + {len(aug_scored):,} augments')

    # In-era attainability: the normalization ceiling (1000) must reflect the best
    # gear obtainable at the server's expansion, not out-of-era items.
    attainable = _attainable_base_ids(content_engine, max_expansion)
    # LDoN adventure-point vendor items: listed in the era files but have NO drop
    # source on this server (confirmed with the lead dev) -- not obtainable.
    UNOBTAINABLE_BASE_IDS = {
        40999,   # Wayfarers Brotherhood Emblem
        41000,   # Adventurer's Stone
        67671,   # Adventurer's Stone of Blessing
        67672,   # Adventurer's Stone Of Magic
    }
    attainable -= UNOBTAINABLE_BASE_IDS
    in_era = lambda item_id: (item_id % 1000000) in attainable
    print(f'attainable base items up to expansion {max_expansion}: {len(attainable):,}')

    # 2. Write lb_item_score (replace wholesale) -- worn items AND augments,
    #    since the runtime scores socketed augs too.
    with lb_engine.begin() as conn:
        conn.execute(text('DELETE FROM lb_item_score'))
        ins = text('INSERT INTO lb_item_score (item_id, raw_score) VALUES (:i, :s)')
        batch = []
        for item_id, raw in ([(i, r) for i, _s, r in scored] + [(i, r) for i, r, _t, _l in aug_scored]):
            batch.append({'i': item_id, 's': round(raw, 2)})
            if len(batch) >= 5000:
                conn.execute(ins, batch)
                batch = []
        if batch:
            conn.execute(ins, batch)

    # 3. Calibration: the ceiling (1000) is the best IN-ERA, best-in-slot WORN set
    #    (Legendary upgrades of in-era items) PLUS every augment socket on that
    #    set filled with the best in-era augment. Since socketed augs now count
    #    toward a character's score, they must count toward the ceiling too --
    #    otherwise well-augmented characters cap at 1000 early.
    worn_ref, bis_ids = _best_per_slot(scored, in_era)
    base_raw, base_ids = _best_per_slot(scored, lambda i: in_era(i) and _tier(i) == 'base')
    ench_raw, _ = _best_per_slot(scored, lambda i: in_era(i) and _tier(i) == 'enchanted')

    # BiS-aug allowance: fill every socket on the BiS worn set with the best
    # in-era augment that fits its socket type. NON-LORE augs may be used in
    # any number of sockets (the solo meta: e.g. Diaku Armor Fragments in every
    # armor socket); LORE augs count once. Best tier per base id.
    best_by_base = {}
    for aug_id, raw, augtype, lore in aug_scored:
        if not in_era(aug_id):
            continue
        b = aug_id % 1000000
        if b not in best_by_base or raw > best_by_base[b][1]:
            best_by_base[b] = (aug_id, raw, augtype, lore)
    aug_pool = sorted(best_by_base.values(), key=lambda t: t[1], reverse=True)
    open_sockets = []                                 # socket type values across the BiS set
    for i in bis_ids:
        open_sockets.extend(sockets.get(i, []))
    bis_sockets = len(open_sockets)
    theo_allowance, filled = 0.0, 0
    fill_counts = []                                  # (aug_id, raw, times_used)
    for aug_id, raw, augtype, lore in aug_pool:
        used = 0
        for si, stype in enumerate(open_sockets):
            if stype and (augtype & (1 << (stype - 1))):
                theo_allowance += raw
                filled += 1
                used += 1
                open_sockets[si] = 0                  # consume socket
                if lore:
                    break                             # lore: one copy only
        if used:
            fill_counts.append((aug_id, raw, used))
        if filled >= bis_sockets:
            break

    # The theoretical fill is an upper bound, not a realistic target (nobody
    # wears 22 Splinters). Anchor the allowance to the BEST aug loadout any
    # player has actually assembled, with 1.5x growth headroom, bounded by
    # [20% .. 100%] of the theoretical maximum. Rebuilds track the meta as it
    # evolves; nobody can reach 1000 because the ceiling stays ahead.
    raw_by_id = {i: r for i, _s, r in scored}
    raw_by_id.update({i: r for i, r, _t, _l in aug_scored})
    per_char = {}
    with content_engine.connect() as c:
        for row in c.execute(text(
                "SELECT character_id, augment_one, augment_two, augment_three, "
                "augment_four, augment_five, augment_six FROM inventory "
                f"WHERE slot_id BETWEEN 0 AND {gs.SCORED_SLOT_MAX} AND item_id > 0")):
            cid = row[0]
            for aug in row[1:]:
                if aug:
                    per_char[cid] = per_char.get(cid, 0.0) + raw_by_id.get(aug, 0.0)
    observed_max = max(per_char.values()) if per_char else 0.0
    aug_allowance = min(theo_allowance, max(0.2 * theo_allowance, observed_max * 1.5))
    reference_max = worn_ref + aug_allowance

    aug_names = {}
    if fill_counts:
        with content_engine.connect() as c:
            _q = text('SELECT id, Name FROM items WHERE id IN :ids').bindparams(
                __import__('sqlalchemy').bindparam('ids', expanding=True))
            for r in c.execute(_q, {'ids': [a for a, _r, _u in fill_counts]}):
                aug_names[r[0]] = r[1]

    with Session(bind=lb_engine) as s:
        def put(k, v):
            m = s.query(Meta).filter(Meta.meta_key == k).first()
            if not m:
                m = Meta(meta_key=k)
                s.add(m)
            m.meta_value = v
        put('reference_max', f'{reference_max:.2f}')
        put('reference_expansion', str(max_expansion))
        put('bis_item_ids', json.dumps(bis_ids))
        put('bis_base_item_ids', json.dumps(base_ids))
        put('aug_allowance', f'{aug_allowance:.2f}')
        put('aug_fill', json.dumps([(a, u) for a, _r, u in fill_counts]))
        s.commit()

    # 3b. Season 2 effect values (click buffs / procs per item + the best attainable set). Static
    #     content, so it is built here once, not on page loads.
    import effect_score
    n_items, bis_eff = effect_score.rebuild(content_engine, lb_engine, max_expansion)
    print(f'season 2 effects: {n_items:,} items with a click buff or proc, best set = {bis_eff:,.0f}')

    # 4. Self-check.
    print(f'worn BiS reference   = {worn_ref:,.0f}')
    print(f'theoretical aug max  = {theo_allowance:,.0f}  ({filled}/{bis_sockets} BiS sockets, upper bound only)')
    print(f'best observed augs   = {observed_max:,.0f}  (top player aug loadout)')
    print(f'aug allowance chosen = {aug_allowance:,.0f}  (observed x1.5, clamped to [20%..100%] of theoretical)')
    print(f'reference_max = {reference_max:,.0f}  (BiS worn + aug allowance = 1000)')
    print(f'  Legendary BiS, no augs : {gs.normalize(worn_ref, reference_max):>4}')
    print(f'  In-era Enchanted BiS   : {gs.normalize(ench_raw, reference_max):>4}')
    print(f'  In-era Base BiS        : {gs.normalize(base_raw, reference_max):>4}')
    print('done.')


if __name__ == '__main__':
    main()
