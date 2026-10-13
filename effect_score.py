"""Season 2 gear score: gear plus click buffs and procs, worn or in the vault lockers.

    season-2 score = 850 x min(1, gear_raw / reference_max) + 150 x effect share

Effect share = the character's effect value / the best attainable effect value (0..1).
  * Click buffs (worn items' clicks and the click locker, vault slots 61-80): valued by what the
    buff grants, with the gear-score stat weights. Buffs giving the same effect do not stack in
    game, so only the strongest value per effect counts (for the character and for the best set).
  * Procs (worn weapons and the proc locker, vault slots 81-83): damage x ~2 procs a minute on the
    weapon-efficiency scale; the best three add up.
  * The same spell counts once, worn or locker.
Season 1 keeps the plain gear score (gear_score.normalize); this module is only used in Season 2.
Tier cut-offs are lowered by the same 15% in Season 2 (see season2_tiers).
"""
from db import connection, get_db
import time
from datetime import datetime

from sqlalchemy import bindparam, text

import gear_score as gs

GEAR_SHARE = 850
EFFECT_SHARE = 150
PROC_SLOTS = 3
PROCS_PER_MIN = 2.0
PROC_DPS_W = 20.0
CACHE_SECONDS = 600         # re-read of the stored table (it changes only when calibration runs)

_W = gs.STAT_WEIGHTS
BUFF_EFFECTS = {             # spell effect id -> (weight, how to read the value)
    0: (_W['regen'], 'pos'),          # HP per tick
    1: (_W['ac'], 'pos'),
    2: (_W['attack'], 'pos'),
    4: (_W['astr'], 'pos'), 5: (_W['adex'], 'pos'), 6: (_W['aagi'], 'pos'), 7: (_W['asta'], 'pos'),
    8: (_W['aint'], 'pos'), 9: (_W['awis'], 'pos'), 10: (_W['acha'], 'pos'),
    11: (_W['haste'], 'haste'),       # 130 = 30% haste
    15: (_W['manaregen'], 'pos'),     # mana per tick
    46: (gs.RESIST_W, 'pos'), 47: (gs.RESIST_W, 'pos'), 48: (gs.RESIST_W, 'pos'),
    49: (gs.RESIST_W, 'pos'), 50: (gs.RESIST_W, 'pos'),
    59: (_W['damageshield'], 'neg'),  # damage shield is stored negative
    69: (_W['hp'], 'pos'),            # max HP
    97: (_W['mana'], 'pos'),          # max mana
    98: (_W['haste'], 'haste'),       # overhaste
    119: (_W['haste'], 'pos'),        # overhaste v3 (already a percent)
}

_cache = {'at': 0.0, 'data': None}


def season2_started(now=None):
    from create_leaderboard_db import SEASON_2_START
    return (now or datetime.utcnow()) >= SEASON_2_START


def season2_tiers(tiers):
    """Season 2 tier cut-offs: 15% lower, so each tier means the same share of the best gear."""
    return [(round(cut * GEAR_SHARE / 1000), name) for cut, name in tiers]


def _val(r, i):
    b, m = int(r[f'effect_base_value{i}'] or 0), int(r[f'max{i}'] or 0)
    return m if (m and abs(m) > abs(b)) else b


def _buff_vector(r):
    vec = {}
    if not r['goodEffect'] or not r['buffduration']:
        return vec
    for i in range(1, 13):
        eid = r[f'effectid{i}']
        if eid in BUFF_EFFECTS:
            w, how = BUFF_EFFECTS[eid]
            x = _val(r, i)
            if how == 'haste':
                x -= 100
            elif how == 'neg':
                x = -x
            if x > 0:
                vec[eid] = max(vec.get(eid, 0.0), x * w)
    return vec


def _proc_value(r):
    if r['goodEffect']:
        return 0.0
    dmg = 0.0
    for i in range(1, 13):
        if r[f'effectid{i}'] == 0:
            x = _val(r, i)
            if x < 0:
                ticks = int(r['buffduration'] or 0)
                dmg += -x * (ticks if ticks > 0 else 1)
    return dmg * PROCS_PER_MIN / 60.0 * PROC_DPS_W


def _stack(vectors):
    best = {}
    for vec in vectors:
        for eid, v in vec.items():
            best[eid] = max(best.get(eid, 0.0), v)
    return sum(best.values())


def rebuild(content_engine, lb_engine, max_expansion):
    """Calibration step (build_gear_scores.py): value every item's click buff and proc once and store
    them in lb_item_effect, plus the best attainable set in lb_meta 'bis_effect_raw'. Spell and item
    content is static, so pages never recompute this. Returns (items stored, best set value)."""
    import json
    import build_gear_scores as bgs
    from sqlalchemy.orm import Session
    from create_leaderboard_db import ItemEffect, Meta
    cols = ['id', 'goodEffect', 'buffduration'] + [f'effectid{i}' for i in range(1, 13)] + \
           [f'effect_base_value{i}' for i in range(1, 13)] + [f'max{i}' for i in range(1, 13)]
    with connection(content_engine) as c:
        items = {r[0]: (int(r[1] or 0), int(r[2] or 0)) for r in c.execute(text(
            "SELECT id, clickeffect, proceffect FROM items WHERE clickeffect > 0 OR proceffect > 0"))}
        spell_ids = sorted({x for cp in items.values() for x in cp if x > 0})
        bvec, pval = {}, {}
        q = text(f"SELECT {','.join(cols)} FROM spells_new WHERE id IN :ids").bindparams(
            bindparam('ids', expanding=True))
        for k in range(0, len(spell_ids), 2000):
            for r in c.execute(q, {'ids': spell_ids[k:k + 2000]}).mappings():
                v = _buff_vector(r)
                if v:
                    bvec[r['id']] = v
                p = _proc_value(r)
                if p > 0:
                    pval[r['id']] = p
    attainable = bgs._attainable_base_ids(content_engine, max_expansion)
    best_clicks = {cl for iid, (cl, _) in items.items() if iid % 1000000 in attainable and cl in bvec}
    best_procs = sorted({pval[pr] for iid, (_, pr) in items.items() if iid % 1000000 in attainable and pr in pval},
                        reverse=True)
    bis = _stack(bvec[s] for s in best_clicks) + sum(best_procs[:PROC_SLOTS])

    rows = []
    for iid, (cl, pr) in items.items():
        if cl in bvec or pr in pval:
            rows.append({'item_id': iid, 'click_spell': cl if cl in bvec else 0,
                         'click_vec': json.dumps({str(k): round(v, 2) for k, v in bvec.get(cl, {}).items()}),
                         'proc_spell': pr if pr in pval else 0, 'proc_value': round(pval.get(pr, 0.0), 2)})
    ItemEffect.__table__.create(lb_engine, checkfirst=True)
    with get_db(lb_engine) as s:
        s.query(ItemEffect).delete()
        s.bulk_insert_mappings(ItemEffect, rows)
        m = s.query(Meta).filter(Meta.meta_key == 'bis_effect_raw').first()
        if not m:
            m = Meta(meta_key='bis_effect_raw')
            s.add(m)
        m.meta_value = f'{bis:.2f}'
        s.commit()
    _cache['data'] = None
    return len(rows), bis


def _tables():
    """The stored per-item effect values (lb_item_effect) and best set value. Read from the
    leaderboard DB and kept for a few minutes; built only by the calibration job."""
    import json
    from sqlalchemy.orm import Session
    from create_leaderboard_db import engine as lb_engine, ItemEffect, Meta
    now = time.time()
    if _cache['data'] and now - _cache['at'] < CACHE_SECONDS:
        return _cache['data']
    items, bvec, pval = {}, {}, {}
    with get_db(lb_engine) as s:
        for r in s.query(ItemEffect.item_id, ItemEffect.click_spell, ItemEffect.click_vec,
                         ItemEffect.proc_spell, ItemEffect.proc_value).all():
            items[r[0]] = (r[1], r[3])
            if r[1]:
                bvec[r[1]] = {int(k): float(v) for k, v in json.loads(r[2] or '{}').items()}
            if r[3]:
                pval[r[3]] = float(r[4])
        m = s.query(Meta).filter(Meta.meta_key == 'bis_effect_raw').first()
    _cache['data'] = {'items': items, 'bvec': bvec, 'pval': pval, 'bis': float(m.meta_value) if m else 0.0}
    _cache['at'] = now
    return _cache['data']


def effect_shares(content_engine, char_ids=None):
    """{character_id: effect share 0..1} from worn items and the click / proc lockers."""
    t = _tables()
    items, bvec, pval, bis = t['items'], t['bvec'], t['pval'], t['bis']
    if not bis:
        return {}
    only = set(char_ids) if char_ids is not None else None
    clicks, procs = {}, {}

    def add(cid, iid, as_click, as_proc):
        cl, pr = items.get(iid, (0, 0))
        if as_click and cl in bvec:
            clicks.setdefault(cid, set()).add(cl)
        if as_proc and pr in pval:
            procs.setdefault(cid, set()).add(pr)

    # one character (the sheet) filters in SQL; the boards read every row once
    where = " AND character_id IN :ids" if only is not None and len(only) <= 1000 else ""
    params = {'ids': list(only)} if where else {}

    def q(sql):
        t = text(sql + where)
        return t.bindparams(bindparam('ids', expanding=True)) if where else t

    with connection(content_engine) as c:
        for cid, iid in c.execute(q(
                f"SELECT character_id, item_id FROM inventory WHERE slot_id BETWEEN 0 AND {gs.SCORED_SLOT_MAX} "
                "AND item_id > 0"), params):
            if only is None or cid in only:
                add(cid, iid, True, True)
        try:  # character_vaults is an optional custom table, absent on stock PEQ
            vault_rows = c.execute(q(
                "SELECT character_id, vault_slot, item_id FROM character_vaults "
                "WHERE bag_index = -1 AND vault_slot BETWEEN 61 AND 83"), params).all()
        except Exception:
            c.rollback()
            vault_rows = []
        for cid, slot, iid in vault_rows:
            if only is None or cid in only:
                add(cid, iid, slot <= 80, slot >= 81)
    out = {}
    for cid in set(clicks) | set(procs):
        v = _stack(bvec[s] for s in clicks.get(cid, ())) + \
            sum(sorted((pval[p] for p in procs.get(cid, ())), reverse=True)[:PROC_SLOTS])
        out[cid] = min(1.0, v / bis)
    return out


def season2_score(gear_raw, reference_max, effect_share):
    """(total 0-1000, gear part, effects part)."""
    if not reference_max:
        return 0, 0, 0
    g = GEAR_SHARE * min(1.0, max(0.0, gear_raw) / float(reference_max))
    e = EFFECT_SHARE * max(0.0, min(1.0, effect_share or 0.0))
    return round(g + e), round(g), round(e)
