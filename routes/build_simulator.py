"""Build Simulator — pick a multiclass trio (by clicking class icons) and see
what that combination can do: the AAs it can train (PoP-era only) and the spells
it can use, with hover tooltips and AA recast timers.

Era-aware: AAs are filtered to the server's current expansion (read from the same
rule_values source as the gear score), so the list stays correct as the server
progresses. Synergies and DPS estimates are deferred (game logic not defined yet);
the shared-across-classes highlight is the one honest synergy signal.
"""
from db import get_db
from flask import Blueprint, render_template, request
from sqlalchemy.orm import Session
from sqlalchemy import and_, text

import logic
from gear_score import class_abbr
from build_gear_scores import get_server_expansion

# db_str.type that holds an AA's description text (dbstr_us AA-desc category).
AA_DESC_STR_TYPE = 4
# AA categories that are veteran-reward / glyph / special summons (not trainable
# class AAs and not in use on NMS).
EXCLUDED_AA_CATEGORIES = (5, 7, 9)

bp = Blueprint('build_simulator', __name__, template_folder='templates')

CLASSES = {
    1: "Warrior", 2: "Cleric", 3: "Paladin", 4: "Ranger", 5: "Shadow Knight",
    6: "Druid", 7: "Monk", 8: "Bard", 9: "Rogue", 10: "Shaman",
    11: "Necromancer", 12: "Wizard", 13: "Magician", 14: "Enchanter",
    15: "Beastlord", 16: "Berserker",
}
# icon filename (in static/class_icons/) per class id
CLASS_ICON = {
    1: 'warrior', 2: 'cleric', 3: 'paladin', 4: 'ranger', 5: 'shadowknight',
    6: 'druid', 7: 'monk', 8: 'bard', 9: 'rogue', 10: 'shaman', 11: 'necromancer',
    12: 'wizard', 13: 'magician', 14: 'enchanter', 15: 'beastlord', 16: 'berserker',
}
MAX_LEVEL = 65


def fmt_recast(seconds):
    """Human recast time, or None for passives / no-recast."""
    s = int(seconds or 0)
    if s <= 0:
        return None
    if s < 60:
        return f'{s}s'
    m, s = divmod(s, 60)
    if m < 60:
        return f'{m}m' if s == 0 else f'{m}m {s}s'
    h, m = divmod(m, 60)
    return f'{h}h' if m == 0 else f'{h}h {m}m'


def available_aas(session, class_ids, max_expansion):
    """AAs any chosen class can actually train on NMS, restricted to PoP-era
    (expansion <= max_expansion). Excludes veteran-reward summons (grant_only),
    expendable glyphs (charges / special categories) and other non-trainable
    special AAs (type 4). Pulls the real description text from db_str and the
    recast timer from the first rank."""
    AA = logic.Base.classes.aa_ability
    Rank = logic.Base.classes.aa_ranks
    Str = logic.Base.classes.db_str
    mask = 0
    for c in class_ids:
        mask |= (1 << (c - 1))
    rows = (session.query(AA.id, AA.name, getattr(AA, 'classes'),
                          Rank.recast_time, Str.value)
            .join(Rank, Rank.id == AA.first_rank_id)
            .outerjoin(Str, and_(Str.id == Rank.desc_sid,
                                 getattr(Str, 'type') == AA_DESC_STR_TYPE))
            .filter(getattr(AA, 'classes').op('&')(mask) != 0,
                    AA.enabled == 1,
                    Rank.expansion <= max_expansion,
                    AA.grant_only == 0,                 # not a granted veteran reward
                    AA.charges == 0,                    # not an expendable glyph
                    getattr(AA, 'type') != 4,           # not a special/expendable AA
                    getattr(AA, 'category').notin_(EXCLUDED_AA_CATEGORIES),
                    ~AA.name.like('Glyph%'))
            .all())
    aas, seen = [], set()
    for aid, name, classes, recast, desc in rows:
        if not name or name.startswith('Unknown') or name in seen:
            continue
        seen.add(name)
        who = [class_abbr(c) for c in class_ids if classes & (1 << (c - 1))]
        aas.append({'id': aid, 'name': name, 'recast': fmt_recast(recast),
                    'description': (desc or '').strip() or 'No description available.',
                    'classes': who, 'shared': len(who) > 1})
    aas.sort(key=lambda a: a['name'])
    return aas


# Post-PoP utility spells that DO ship a teaching scroll (so the scroll gate
# below can't catch them) but are clearly later-expansion: the guild-hall /
# guild-lobby "anchor" teleport family. These name fragments do not collide with
# any PoP-era spell, so a substring match is safe. Add here if another
# scroll-shipped post-PoP family surfaces; a fully authoritative gate would need
# a per-spell expansion source, which this schema does not carry.
POST_POP_NAME_FRAGMENTS = ('Anchor', 'Guild Hall', 'Guild Lobby', 'Guild Portal')


def _is_post_pop_utility(name):
    return any(frag in name for frag in POST_POP_NAME_FRAGMENTS)


def scribable_spell_ids(session):
    """Spell ids that a scroll/tome item teaches (items.scrolleffect).

    spells_new has no expansion column, so we can't era-gate spells directly.
    But player spells are learned from scroll items, and the post-PoP utility
    spells that leak into a level<=65 class list (Echo of *, Guild Hall Anchor,
    re-id'd Bind Affinity, etc.) have no teaching scroll. Requiring a scroll is
    therefore the honest in-era signal for scribed spells. Disciplines are
    trained rather than scribed and are kept separately by the caller.

    Known limitation: a later-expansion spell that DOES ship its own scroll and
    happens to carry a class level <=65 would still pass. None of the reported
    leaks do; tighten this if a true per-spell expansion source appears on NMS.
    """
    rows = session.execute(text(
        "SELECT DISTINCT scrolleffect FROM items WHERE scrolleffect > 0")).all()
    return {r[0] for r in rows}


def available_spells(session, class_ids, level, limit=600):
    """Spells/disciplines any chosen class can use by ``level``, era-filtered to
    spells that are actually obtainable in-era (a teaching scroll exists) plus
    trained disciplines."""
    from sqlalchemy import or_
    SpellsNew = logic.SpellsNew
    is_disc_col = getattr(SpellsNew, 'IsDiscipline', None)
    class_cols = {c: getattr(SpellsNew, f'classes{c}') for c in class_ids}
    conds = [col.between(1, level) for col in class_cols.values()]
    select_cols = [SpellsNew.id, SpellsNew.name, *class_cols.values()]
    if is_disc_col is not None:
        select_cols.append(is_disc_col)
    q = session.query(*select_cols).filter(or_(*conds))

    scroll_ids = scribable_spell_ids(session)
    spells = []
    for row in q.all():
        sid, name = row[0], row[1]
        if not name:
            continue
        is_disc = bool(row[-1]) if is_disc_col is not None else False
        # In-era gate: keep trained disciplines, or spells with a teaching scroll.
        if not is_disc and sid not in scroll_ids:
            continue
        # Drop the scroll-shipped post-PoP utility that the scroll gate can't catch.
        if _is_post_pop_utility(name):
            continue
        levels = row[2:2 + len(class_cols)]
        best_lvl, best_class = None, None
        for c, lvl in zip(class_ids, levels):
            if lvl and 1 <= lvl <= level and (best_lvl is None or lvl < best_lvl):
                best_lvl, best_class = lvl, c
        if best_lvl is None:
            continue
        spells.append({'id': sid, 'name': name, 'level': best_lvl,
                       'class_abbr': class_abbr(best_class)})
    total = len(spells)
    spells.sort(key=lambda s: (-s['level'], s['name']))
    return spells[:limit], total


@bp.route('/build', methods=['GET'])
def build_simulator():
    picked = []
    for key in ('class1', 'class2', 'class3'):
        raw = request.args.get(key, '')
        if raw.isdigit() and int(raw) in CLASSES and int(raw) not in picked:
            picked.append(int(raw))
    try:
        level = max(1, min(MAX_LEVEL, int(request.args.get('level', MAX_LEVEL))))
    except (TypeError, ValueError):
        level = MAX_LEVEL

    result = None
    if picked:
        with get_db(logic.engine) as session:
            max_expansion = get_server_expansion(logic.engine, logic.site_config)[0]
            aas = available_aas(session, picked, max_expansion)
            spells, spell_total = available_spells(session, picked, level)
        result = {
            'class_ids': picked,
            'class_names': [CLASSES[c] for c in picked],
            'class_trio': '/'.join(class_abbr(c) for c in picked),
            'level': level, 'expansion': max_expansion,
            'aas': aas, 'aa_count': len(aas),
            'shared_aa_count': sum(1 for a in aas if a['shared']),
            'spells': spells, 'spell_total': spell_total,
        }

    class_list = [{'id': c, 'name': CLASSES[c], 'icon': CLASS_ICON[c]} for c in CLASSES]
    return render_template('build_simulator.html', class_list=class_list,
                           max_level=MAX_LEVEL, picked=picked, level=level, result=result)
