"""Gear score — a universal 0-1000 "how geared is this character" metric.

Per item: a fixed weighted sum of its stats (Option A), with resists folded
together with their heroic counterpart, a weapon damage/delay efficiency term,
and a flat bonus per populated effect slot.  Per character: the SUM of equipped
item scores, normalized against a calibrated best-in-slot maximum so a fully
BiS (legendary) character reads ~1000.

Weights were calibrated against the live item database (see build_gear_scores.py):
Legendary BiS -> 1000, Enchanted BiS -> ~969, Base BiS -> ~533.

Two entry points:
  * score_item(row)              -- raw score of one content item (build job)
  * gear_score_from_ids(session, item_ids) -- normalized 0-1000 for a character
                                              (ingest; reads precomputed scores)
"""
from create_leaderboard_db import ItemScore, Meta

# --- weights (tunable; re-run build_gear_scores.py after changing) ---
STAT_WEIGHTS = {
    'hp': 0.25, 'mana': 0.20, 'endur': 0.15, 'ac': 4.0,
    'heroic_str': 8, 'heroic_sta': 8, 'heroic_agi': 8, 'heroic_dex': 8,
    'heroic_wis': 8, 'heroic_int': 8, 'heroic_cha': 8,
    'astr': 1.5, 'asta': 1.5, 'aagi': 1.5, 'adex': 1.5, 'awis': 1.5, 'aint': 1.5, 'acha': 1.5,
    'attack': 3.0, 'backstabdmg': 3.0, 'spelldmg': 2.5, 'healamt': 2.5, 'haste': 2.0,
    'accuracy': 1.5, 'avoidance': 1.5, 'combateffects': 1.5, 'damageshield': 2.0,
    'shielding': 1.5, 'dotshielding': 1.5, 'spellshield': 1.5, 'stunresist': 1.5,
    'strikethrough': 1.5, 'regen': 5.0, 'manaregen': 5.0, 'enduranceregen': 3.0,
}
RESISTS = ('cr', 'fr', 'mr', 'pr', 'dr')
RESIST_W = 2.0
W_EFF_W = 20.0                                   # weapon damage/delay efficiency
EFFECT_BONUS = {'worneffect': 30, 'focuseffect': 30, 'proceffect': 25, 'clickeffect': 15}

# Columns the build job must SELECT from the content items table.
NEEDED_COLUMNS = (list(STAT_WEIGHTS) + [f'heroic_{r}' for r in RESISTS] + list(RESISTS)
                  + ['delay', 'damage'] + list(EFFECT_BONUS))

# 22 wearable equip slots (bit index -> name) for best-in-slot calibration.
SLOTS = {0: 'Charm', 1: 'Ear1', 2: 'Head', 3: 'Face', 4: 'Ear2', 5: 'Neck', 6: 'Shoulders',
         7: 'Arms', 8: 'Back', 9: 'Wrist1', 10: 'Wrist2', 11: 'Range', 12: 'Hands',
         13: 'Primary', 14: 'Secondary', 15: 'Finger1', 16: 'Finger2', 17: 'Chest',
         18: 'Legs', 19: 'Feet', 20: 'Waist', 21: 'PowerSource', 22: 'Ammo'}

# Slots that count toward Gear Score. Ammo and power source are excluded: what sits there
# does not change the character in game, so a crafted Shuriken of Eternity in the ammo slot
# was inflating the score of anyone who made one. Both slots are still shown on the
# character sheet; they are only left out of scoring.
UNSCORED_SLOTS = (21, 22)
SCORED_SLOTS = {bit: name for bit, name in SLOTS.items() if bit not in UNSCORED_SLOTS}
# For the SQL that reads the inventory table: slot ids are contiguous from 0.
SCORED_SLOT_MAX = max(SCORED_SLOTS)


def num(v):
    """Coerce a possibly-string / possibly-None item field to a float."""
    try:
        return float(v) if v not in (None, '') else 0.0
    except (TypeError, ValueError):
        return 0.0


def score_item(row):
    """Raw gear-score contribution of one content item. ``row`` is a mapping
    (supports .get) with the NEEDED_COLUMNS present."""
    g = lambda k: num(row.get(k))
    s = sum(g(k) * w for k, w in STAT_WEIGHTS.items())
    for r in RESISTS:                                    # fold heroic resist in
        s += (g(r) + g('heroic_' + r)) * RESIST_W
    if g('delay'):                                       # weapon efficiency
        s += (g('damage') / g('delay')) * W_EFF_W
    for slot, bonus in EFFECT_BONUS.items():             # flat effect-slot bonus
        if g(slot) > 0:
            s += bonus
    return s


def normalize(total_raw, reference_max):
    """Map a summed raw score to the 0-1000 scale, clamped."""
    if not reference_max or float(reference_max) <= 0:
        return 0
    return round(min(1000.0, float(total_raw) / float(reference_max) * 1000.0))


def get_reference_max(session):
    m = session.query(Meta).filter(Meta.meta_key == 'reference_max').first()
    return float(m.meta_value) if m else None


def gear_score_from_ids(session, item_ids, reference_max=None):
    """Normalized 0-1000 gear score for a character's equipped item IDs, summing
    the precomputed lb_item_score rows. Returns None if scores aren't built yet
    or no item ids were provided."""
    ids = [int(i) for i in (item_ids or []) if i]
    if not ids:
        return None
    if reference_max is None:
        reference_max = get_reference_max(session)
    if not reference_max:
        return None
    # Look up distinct scores, then sum per-occurrence so a character wearing the
    # same item in two slots (e.g. matching earrings) is counted twice, matching
    # how reference_max is built per-slot.
    rows = session.query(ItemScore.item_id, ItemScore.raw_score).filter(
        ItemScore.item_id.in_(set(ids))).all()
    score_map = {r[0]: float(r[1]) for r in rows}
    total = sum(score_map.get(i, 0.0) for i in ids)
    return normalize(total, reference_max)


def score_many_characters(session, char_item_ids, reference_max=None, effect_shares=None):
    """Batch gear score for many characters. ``char_item_ids`` maps
    character_id -> list of equipped item ids. Returns {character_id: 0-1000}.
    Two queries total (reference_max + one IN over all distinct items), then
    summed per character in Python (duplicates respected)."""
    if not char_item_ids:
        return {}
    if reference_max is None:
        reference_max = get_reference_max(session)
    if not reference_max:
        return {}
    all_ids = {int(i) for ids in char_item_ids.values() for i in (ids or []) if i}
    if not all_ids:
        return {cid: 0 for cid in char_item_ids}
    rows = session.query(ItemScore.item_id, ItemScore.raw_score).filter(
        ItemScore.item_id.in_(all_ids)).all()
    score_map = {r[0]: float(r[1]) for r in rows}
    out = {}
    for cid, ids in char_item_ids.items():
        total = sum(score_map.get(int(i), 0.0) for i in (ids or []) if i)
        if effect_shares is not None:            # Season 2: gear 850 + effects 150 (effect_score.py)
            import effect_score
            out[cid] = effect_score.season2_score(total, reference_max, effect_shares.get(cid, 0.0))[0]
        else:
            out[cid] = normalize(total, reference_max)
    return out


# EverQuest class id -> 3-letter abbreviation (for multiclass trio display, e.g.
# "War/Pal/Mnk"). Shared so the raid leaderboard and character pages agree.
CLASS_ABBR = {
    1: 'War', 2: 'Clr', 3: 'Pal', 4: 'Rng', 5: 'SK', 6: 'Dru', 7: 'Mnk', 8: 'Brd',
    9: 'Rog', 10: 'Shm', 11: 'Nec', 12: 'Wiz', 13: 'Mag', 14: 'Enc', 15: 'Bst', 16: 'Ber',
}


def class_abbr(class_id):
    return CLASS_ABBR.get(int(class_id), '?') if class_id is not None else '?'


def class_trio(class_ids):
    """Render a list of class ids as 'War/Pal/Mnk'. Accepts 1-3 ids."""
    return '/'.join(class_abbr(c) for c in class_ids if c) or '?'


def decode_class_bitmask(value):
    """NMS packs a character's class set as a bitmask in character_data.class:
    bit (class_id - 1) set means the character has that class. Returns the list
    of class ids present, low bit first (e.g. 69 -> [1, 3, 7] = War/Pal/Mnk)."""
    if not value:
        return []
    v = int(value)
    return [cid for cid in range(1, 17) if v & (1 << (cid - 1))]


def class_trio_from_bitmask(value):
    """character_data.class bitmask -> 'War/Pal/Mnk'."""
    return class_trio(decode_class_bitmask(value))
