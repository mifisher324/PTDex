"""Alternate Advancement data for the AA search, AA pages, tooltips and the
character card.

WHERE THE DATA LIVES (peq)
    aa_ability         one row per AA: name, classes bitmask, tab (type), category,
                       first_rank_id, enabled/grant_only/charges flags.
    aa_ranks           one row per rank, chained by next_id from first_rank_id:
                       cost, level_req, recast_time, spell, expansion, and the
                       db_str ids of that rank's name (title_sid) and text (desc_sid).
    aa_rank_effects    what a rank does: (rank_id, slot, effect_id, base1, base2),
                       where effect_id is the same SPA number spells use.
    aa_rank_prereqs    (rank_id, aa_id, points): the rank needs `points` spent in
                       ability `aa_id` first.
    db_str             type 1 = AA name, type 4 = AA description (dbstr_us).
    character_alternate_abilities
                       (id = character, aa_id = a rank id of the ability, which the
                        server resolves to its ability; aa_value = the NUMBER OF
                        RANKS trained -- zone/aa_ability.cpp GetRankByPointsSpent
                        walks that many ranks down the chain despite its name).

WHICH RANKS EXIST ON THIS SERVER
    zone/aa.cpp CanUseAlternateAdvancementRank: Expansion:UseCurrentExpansionAAOnly
    is off here, so a rank is usable when its expansion is <= 0 or its bit is set
    in the player's expansions, which come from World:ExpansionSettings. That is
    NOT the gear era the rest of the site uses: players legitimately hold ranks
    from later expansions. Ranks tagged 99999999 are placeholders and never usable.

The rank table is small (~6.7k rows) and static, so it is loaded once per
process; everything else is read per request.
"""
import db
from db import get_db
import time

from sqlalchemy import text, bindparam
from sqlalchemy.orm import Session

import logic
from gear_score import class_abbr

STR_NAME, STR_DESC = 1, 4

# aa_ability.type is the AA window tab.
AA_TABS = {1: 'General', 2: 'Archetype', 3: 'Class', 4: 'Special', 5: 'Focus'}
# Categories that are not trainable class AAs: veteran rewards, glyphs, special
# summons -- and 3/4, the Monster Mission shroud abilities, which only exist
# inside a monster form and never appear on the public pages.
HIDDEN_CATEGORIES = (3, 4, 5, 7, 9)

CLASS_NAMES = {
    1: "Warrior", 2: "Cleric", 3: "Paladin", 4: "Ranger", 5: "Shadow Knight",
    6: "Druid", 7: "Monk", 8: "Bard", 9: "Rogue", 10: "Shaman",
    11: "Necromancer", 12: "Wizard", 13: "Magician", 14: "Enchanter",
    15: "Beastlord", 16: "Berserker",
}
ALL_CLASSES = 0xFFFF

_cache = {'ranks': None, 'ts': 0.0}
_CACHE_TTL = 3600


def _ranks(session=None):
    """{rank_id: row dict} for every rank, cached for an hour."""
    now = time.time()
    if _cache['ranks'] is not None and now - _cache['ts'] < _CACHE_TTL:
        return _cache['ranks']
    # inside a request the shared session is used and left open for the page
    own = session is None and not db.in_request()
    s = session or db.session(logic.engine)
    try:
        rows = s.execute(text(
            "SELECT id, cost, level_req, recast_time, spell, spell_type, expansion, "
            "next_id, title_sid, desc_sid FROM aa_ranks")).all()
    finally:
        if own:
            s.close()
    ranks = {r[0]: {'id': r[0], 'cost': int(r[1]), 'level_req': int(r[2]),
                    'recast': int(r[3]), 'spell': int(r[4]), 'spell_type': int(r[5]),
                    'expansion': int(r[6]), 'next_id': int(r[7]),
                    'title_sid': int(r[8]), 'desc_sid': int(r[9])} for r in rows}
    _cache['ranks'] = ranks
    _cache['ts'] = now
    return ranks


def rank_chain(first_rank_id, ranks=None):
    """The ranks of one ability, in order."""
    ranks = ranks or _ranks()
    out, rid, seen = [], first_rank_id, set()
    while rid and rid > 0 and rid in ranks and rid not in seen:
        seen.add(rid)
        out.append(ranks[rid])
        rid = ranks[rid]['next_id']
    return out


def _rank_owner(ranks):
    """{rank_id: first_rank_id of its ability}, so any stored rank id resolves."""
    owner = {}
    nexts = {r['next_id'] for r in ranks.values() if r['next_id'] > 0}
    for rid in ranks:
        if rid in nexts:
            continue                          # not the head of a chain
        for r in rank_chain(rid, ranks):
            owner[r['id']] = rid
    return owner


_mask_cache = {'mask': None, 'ts': 0.0}


def expansion_mask():
    """World:ExpansionSettings -- the expansions a player's AA ranks may come from."""
    now = time.time()
    if _mask_cache['mask'] is not None and now - _mask_cache['ts'] < _CACHE_TTL:
        return _mask_cache['mask']
    mask = None
    try:
        with get_db(logic.engine) as s:
            v = s.execute(text(
                "SELECT rule_value FROM rule_values WHERE rule_name = 'World:ExpansionSettings' "
                "ORDER BY ruleset_id LIMIT 1")).scalar()
        mask = int(v)
    except Exception:
        mask = None
    if mask is None:
        mask = 0x7FFFF                     # EQEmu default: every expansion through RoF
    _mask_cache['mask'] = mask
    _mask_cache['ts'] = now
    return mask


def rank_usable(rank, mask=None):
    """Same test as the server's CanUseAlternateAdvancementRank expansion check."""
    exp = rank['expansion']
    if exp <= 0:
        return True
    if exp > 31:
        return False
    return bool((mask if mask is not None else expansion_mask()) & (1 << (exp - 1)))


def fmt_recast(seconds):
    s = int(seconds or 0)
    if s <= 0:
        return None
    if s < 60:
        return '%ds' % s
    m, s = divmod(s, 60)
    if m < 60:
        return '%dm' % m if s == 0 else '%dm %ds' % (m, s)
    h, m = divmod(m, 60)
    return '%dh' % h if m == 0 else '%dh %dm' % (h, m)


def class_list(mask):
    if (mask & ALL_CLASSES) == ALL_CLASSES:
        return ['All classes']
    return [CLASS_NAMES[c] for c in range(1, 17) if mask & (1 << (c - 1))]


def _public_filter():
    return ("a.enabled = 1 AND a.grant_only = 0 AND a.charges = 0 "
            "AND a.category NOT IN (3, 4, 5, 7, 9) AND a.name NOT LIKE 'Glyph%' "
            "AND a.name NOT LIKE 'Unknown%'")


# --------------------------------------------------------------------------- #
# Effects                                                                      #
# --------------------------------------------------------------------------- #
class _NoSpell(object):
    """Stand-in spell record for translate_spa: an AA effect has no duration,
    target or formula, so every spell field reads as 0."""
    def __getattr__(self, _name):
        return 0


def describe_effect(effect_id, base1, base2):
    """Plain words for one rank effect, via the spell pages' SPA translator.
    Falls back to the effect's name and raw values when a spell-only branch of
    the translator cannot handle an AA's numbers."""
    try:
        import spell
        d = spell.translate_spa(effect_id, base1, base2, 100, base1, 255, _NoSpell(),
                                no_item_links=True)
        if d:
            return str(d)
    except Exception:
        pass
    try:
        import spell
        name = spell.fast_spa_lookup(effect_id)
    except Exception:
        name = None
    label = name or 'Effect %d' % effect_id
    return '%s: %s%s' % (label, base1, (' (limit %s)' % base2) if base2 else '')


# --------------------------------------------------------------------------- #
# Search                                                                       #
# --------------------------------------------------------------------------- #
def search(name='', class_ids=(), tab=None, activation='any', text_query='',
           all_eras=False, limit=400):
    """AAs matching the filters, sorted by name. Each row: id, name, classes,
    tab, ranks, total cost, first-rank level, recast, activated, description."""
    where = [_public_filter()]
    params = {}
    if name:
        where.append("a.name LIKE :nm")
        params['nm'] = '%' + name + '%'
    mask = 0
    for c in class_ids:
        mask |= 1 << (int(c) - 1)
    if mask:
        where.append("(a.classes & :mask) != 0")
        params['mask'] = mask
    if tab:
        where.append("a.type = :tab")
        params['tab'] = int(tab)
    if text_query:
        where.append("d.value LIKE :tq")
        params['tq'] = '%' + text_query + '%'
    mask_x = expansion_mask()
    sql = ("SELECT a.id, a.name, a.classes, a.type, a.first_rank_id, d.value "
           "FROM aa_ability a JOIN aa_ranks r ON r.id = a.first_rank_id "
           "LEFT JOIN db_str d ON d.id = r.desc_sid AND d.type = %d "
           "WHERE %s ORDER BY a.name LIMIT %d" % (STR_DESC, ' AND '.join(where), int(limit)))
    with get_db(logic.engine) as s:
        rows = s.execute(text(sql), params).all()
        ranks = _ranks(s)
    out, seen = [], set()
    for aid, nm, classes, typ, first, desc in rows:
        chain = rank_chain(first, ranks)
        if not chain:
            continue
        if not all_eras:
            chain = [r for r in chain if rank_usable(r, mask_x)]
            if not chain:
                continue
        key = (nm, classes)
        if key in seen:
            continue
        seen.add(key)
        activated = chain[0]['spell'] > 0
        if activation == 'active' and not activated:
            continue
        if activation == 'passive' and activated:
            continue
        out.append({
            'id': aid, 'name': nm, 'classes': class_list(int(classes)),
            'class_abbrs': [class_abbr(c) for c in range(1, 17)
                            if int(classes) & (1 << (c - 1))],
            'tab': AA_TABS.get(int(typ), '—'),
            'ranks': len(chain), 'cost': sum(r['cost'] for r in chain),
            'level': chain[0]['level_req'], 'recast': fmt_recast(chain[0]['recast']),
            'activated': activated,
            'description': (desc or '').strip(),
        })
    return out


# --------------------------------------------------------------------------- #
# Detail                                                                       #
# --------------------------------------------------------------------------- #
def detail(aa_id):
    """Everything about one AA, rank by rank, or None."""
    with get_db(logic.engine) as s:
        row = s.execute(text(
            "SELECT a.id, a.name, a.classes, a.type, a.category, a.first_rank_id, "
            "       a.enabled, a.grant_only, a.charges "
            "FROM aa_ability a WHERE a.id = :i"), {'i': int(aa_id)}).first()
        if not row or int(row[4]) in (3, 4):
            return None
        aid, nm, classes, typ, cat, first, enabled, grant_only, charges = row
        ranks = _ranks(s)
        chain = rank_chain(first, ranks)
        rids = [r['id'] for r in chain]
        effects, prereqs, strings, spells = {}, {}, {}, {}
        if rids:
            for rid, slot, eid, b1, b2 in s.execute(text(
                    "SELECT rank_id, slot, effect_id, base1, base2 FROM aa_rank_effects "
                    "WHERE rank_id IN :ids ORDER BY rank_id, slot")
                    .bindparams(bindparam('ids', expanding=True)), {'ids': rids}).all():
                effects.setdefault(rid, []).append(
                    {'slot': slot, 'effect_id': eid, 'base1': b1, 'base2': b2,
                     'text': describe_effect(int(eid), int(b1), int(b2))})
            for rid, paid, pts, pname in s.execute(text(
                    "SELECT p.rank_id, p.aa_id, p.points, a.name FROM aa_rank_prereqs p "
                    "LEFT JOIN aa_ability a ON a.id = p.aa_id WHERE p.rank_id IN :ids")
                    .bindparams(bindparam('ids', expanding=True)), {'ids': rids}).all():
                prereqs.setdefault(rid, []).append(
                    {'aa_id': paid, 'points': pts, 'name': pname or 'AA %s' % paid})
            sids = {r['desc_sid'] for r in chain if r['desc_sid'] > 0} | \
                   {r['title_sid'] for r in chain if r['title_sid'] > 0}
            if sids:
                for sid, styp, val in s.execute(text(
                        "SELECT id, type, value FROM db_str WHERE id IN :ids AND type IN (1, 4)")
                        .bindparams(bindparam('ids', expanding=True)),
                        {'ids': list(sids)}).all():
                    strings[(sid, styp)] = (val or '').strip()
            spell_ids = [r['spell'] for r in chain if r['spell'] > 0]
            if spell_ids:
                for sid, sname in s.execute(text(
                        "SELECT id, name FROM spells_new WHERE id IN :ids")
                        .bindparams(bindparam('ids', expanding=True)),
                        {'ids': spell_ids}).all():
                    spells[sid] = sname
    mask_x = expansion_mask()
    out_ranks, running = [], 0
    last_desc = None
    for n, r in enumerate(chain, start=1):
        running += r['cost']
        desc = strings.get((r['desc_sid'], STR_DESC))
        out_ranks.append({
            'rank': n, 'cost': r['cost'], 'total_cost': running,
            'level': r['level_req'], 'recast': fmt_recast(r['recast']),
            'expansion': r['expansion'], 'future': not rank_usable(r, mask_x),
            'spell_id': r['spell'] if r['spell'] > 0 else None,
            'spell_name': spells.get(r['spell']),
            'effects': effects.get(r['id'], []),
            'prereqs': prereqs.get(r['id'], []),
            # only show a rank's text when it differs from the rank before
            'description': desc if desc and desc != last_desc else None,
        })
        if desc:
            last_desc = desc
    first_desc = strings.get((chain[0]['desc_sid'], STR_DESC)) if chain else None
    return {
        'id': aid, 'name': nm, 'classes': class_list(int(classes)),
        'tab': AA_TABS.get(int(typ), '—'),
        'activated': bool(chain and chain[0]['spell'] > 0),
        'ranks': out_ranks, 'max_rank': len(out_ranks),
        'available_ranks': sum(1 for r in out_ranks if not r['future']),
        'total_cost': running,
        'description': first_desc or '',
        'public': bool(enabled) and not grant_only and not charges,
    }


# --------------------------------------------------------------------------- #
# Character card                                                               #
# --------------------------------------------------------------------------- #
def character_aas(char_id, class_mask):
    """A character's trained AAs grouped by tab, plus totals against everything
    their classes can train in the current era.

    -> {'groups': [{'tab', 'aas': [{id, name, rank, max_rank, spent}]}],
        'trained_ranks', 'available_ranks', 'trained_aas', 'available_aas',
        'spent', 'max_spend'}"""
    mask_x = expansion_mask()
    with get_db(logic.engine) as s:
        ranks = _ranks(s)
        owner = _rank_owner(ranks)
        mine = {}
        for rid, n in s.execute(text(
                "SELECT aa_id, aa_value FROM character_alternate_abilities WHERE id = :c"),
                {'c': int(char_id)}).all():
            head = owner.get(int(rid))
            if head:
                mine[head] = max(mine.get(head, 0), int(n))
        pool = s.execute(text(
            "SELECT a.id, a.name, a.type, a.first_rank_id FROM aa_ability a "
            "WHERE " + _public_filter() + " AND (a.classes & :mask) != 0"),
            {'mask': int(class_mask) or ALL_CLASSES}).all()
    groups = {}
    tot = {'trained_ranks': 0, 'available_ranks': 0, 'trained_aas': 0,
           'available_aas': 0, 'spent': 0, 'max_spend': 0}
    # No de-duplication by name: on a multiclass character two classes' copies of
    # a same-named AA are separate abilities in the AA window, trained separately.
    for aid, nm, typ, first in pool:
        full = rank_chain(first, ranks)
        chain = [r for r in full if rank_usable(r, mask_x)]
        if not chain:
            continue
        have = min(mine.get(int(first), 0), len(chain))
        tot['available_ranks'] += len(chain)
        tot['available_aas'] += 1
        tot['max_spend'] += sum(r['cost'] for r in chain)
        if have:
            tot['trained_ranks'] += have
            tot['trained_aas'] += 1
            tot['spent'] += sum(r['cost'] for r in full[:have])
            groups.setdefault(AA_TABS.get(int(typ), 'Other'), []).append(
                {'id': aid, 'name': nm, 'rank': have, 'max_rank': len(chain),
                 'maxed': have >= len(chain)})
    order = ['General', 'Archetype', 'Class', 'Special', 'Focus', 'Other']
    out = [{'tab': t, 'aas': sorted(groups[t], key=lambda a: a['name'])}
           for t in order if t in groups]
    tot['pct'] = round(100.0 * tot['trained_ranks'] / tot['available_ranks']) \
        if tot['available_ranks'] else 0
    return dict(tot, groups=out)
