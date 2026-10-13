"""
extract_spells.py  --  Triune AutoCombat data exporter (self-contained).

Reads configuration.ini directly and queries the content DB with raw SQL, so it
does NOT import EQDB's app modules (avoids their circular-import graph). Mirrors
the Build Simulator's gates:

  * Spells -- classesN learn-level 1..65, kept if in-era (a teaching scroll exists)
             or a trained discipline; Echo/post-PoP names dropped.
  * AAs    -- aa_ability/aa_ranks, ACTIVATED only (recast_time > 0), era-gated by
             rank.expansion, veteran/glyph/special categories excluded.

Run from the EQDB project dir (venv python recommended):

    python extract_spells.py            # all 16 classes
    python extract_spells.py War Rng Brd

Writes triune_data.json (web planner) and triune_data.lua (in-game tool).
"""
import configparser
import json
import os
import sys

CLASS_ABBR = {1: 'War', 2: 'Clr', 3: 'Pal', 4: 'Rng', 5: 'SK', 6: 'Dru',
              7: 'Mnk', 8: 'Brd', 9: 'Rog', 10: 'Shm', 11: 'Nec', 12: 'Wiz',
              13: 'Mag', 14: 'Enc', 15: 'Bst', 16: 'Ber'}
ABBR_TO_ID = {v.lower(): k for k, v in CLASS_ABBR.items()}
LEVEL_CAP = 65
POST_POP_SPELL_EXCLUDE = {'Hail of Arrows', 'Twin Shot'}
POST_POP_NAME_FRAGMENTS = ('Anchor', 'Guild Hall', 'Guild Lobby', 'Guild Portal')
EXCLUDED_AA_CATEGORIES = (5, 7)  # category 9 removed -- holds real AAs (Harm Touch, Lay on Hands), not fluff


def resolve_classes(argv):
    if not argv:
        return list(CLASS_ABBR.keys())
    ids = []
    for a in argv:
        k = a.strip().lower()
        if k.isdigit() and int(k) in CLASS_ABBR:
            ids.append(int(k))
        elif k in ABBR_TO_ID:
            ids.append(ABBR_TO_ID[k])
        else:
            print(f"  ! skipping unknown class '{a}'")
    return ids


def make_engine(here):
    from sqlalchemy import create_engine
    cp = configparser.RawConfigParser()
    cp.read_file(open(os.path.join(here, 'configuration.ini')))
    driver = os.environ.get('DB_DRIVER', cp.get('database', 'driver', fallback=''))
    if not driver:
        driver = 'mysql+pymysql://'
    elif '://' not in driver:
        driver = driver + '://'
    user = os.environ.get('DB_USER', cp.get('database', 'user', fallback=''))
    password = os.environ.get('DB_PASSWORD', cp.get('database', 'password', fallback=''))
    database = os.environ.get('DB_DATABASE', cp.get('database', 'database', fallback=''))
    host = os.environ.get('DB_HOST', cp.get('database', 'host', fallback=''))
    port = os.environ.get('DB_PORT', cp.get('database', 'port', fallback=''))
    try:
        max_exp = cp.getint('triune', 'expansion')
    except Exception:
        max_exp = 5
    return create_engine(f'{driver}{user}:{password}@{host}:{port}/{database}'), max_exp


def to_lua(o):
    if isinstance(o, bool):
        return 'true' if o else 'false'
    if isinstance(o, (int, float)):
        return repr(o)
    if isinstance(o, str):
        return '"' + o.replace('\\', '\\\\').replace('"', '\\"') + '"'
    if isinstance(o, list):
        return '{' + ','.join(to_lua(x) for x in o) + '}'
    if isinstance(o, dict):
        return '{' + ','.join('[%s]=%s' % (to_lua(k), to_lua(v)) for k, v in o.items()) + '}'
    return 'nil'


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        from sqlalchemy import text
        engine, max_exp = make_engine(here)
        conn = engine.connect()
    except Exception as e:
        print("ERROR: could not connect to the content DB.")
        print("Run from the EQDB project dir with a working configuration.ini (venv python).")
        print(f"Detail: {e}")
        sys.exit(1)

    class_ids = resolve_classes(sys.argv[1:])

    # which optional columns exist on spells_new?
    cols = {row[0].lower() for row in conn.execute(text("SHOW COLUMNS FROM spells_new"))}
    disc_col = 'IsDiscipline' if 'isdiscipline' in cols else None

    out = {'era_expansion': max_exp, 'spells': {}, 'discs': {}, 'aas': {}}
    tot_s = tot_d = tot_a = 0

    # AA era gate: NMS tags non-attainable / custom AAs with the SENTINEL expansion
    # 99999999 (Fundament lines, Glacial/Poison Arrows, Howl of the Warlord, Lyrical
    # Prankster, group/hastened variants). Real trainable AAs carry a finite expansion
    # (observed 0-12), including Vehement Rage (10). So keep finite expansions and drop
    # the sentinel -- neither a flat <=5 cap (hides Vehement Rage) nor no gate (lets the
    # sentinel junk in) is correct.
    # category 9 was in the exclusion list ("veteran/glyph/special") but on
    # this server it holds exactly two real, important class-signature AAs --
    # Harm Touch (SK) and Lay on Hands (Paladin) -- confirmed by direct query;
    # categories 5 and 7 have zero matching rows either way so dropping them
    # from the exclusion has no effect, left for documentation purposes only.
    aa_sql = text(
        "SELECT a.name, r.recast_time "
        "FROM aa_ability a JOIN aa_ranks r ON r.id = a.first_rank_id "
        "WHERE (a.`classes` & :mask) <> 0 AND a.enabled = 1 "
        "AND r.expansion BETWEEN 0 AND 99999998 "
        "AND a.grant_only = 0 AND a.charges = 0 AND a.`type` <> 4 "
        "AND a.`category` NOT IN (5,7) AND a.name NOT LIKE :glyph AND r.recast_time > 0")

    for cid in class_ids:
        abbr = CLASS_ABBR[cid]
        col = 'classes%d' % cid

        sel = ("SELECT id, name, %s AS lvl, goodEffect, buffduration, "
               "effectid1, effectid2, effectid3, effectid4%s "
               "FROM spells_new WHERE %s BETWEEN 1 AND %d") % (
            col, (', ' + disc_col) if disc_col else '', col, LEVEL_CAP)
        spells, discs, seen = [], [], set()
        for row in conn.execute(text(sel)):
            m = row._mapping
            name, lvl, good = m['name'], m['lvl'], m['goodEffect']
            sid = m['id']
            if not name or name in seen:
                continue
            if name.startswith('Echo of') or name in POST_POP_SPELL_EXCLUDE:
                continue
            if any(frag in name for frag in POST_POP_NAME_FRAGMENTS):
                continue
            is_disc = bool(m[disc_col]) if disc_col else False
            # No teaching-scroll gate: it dropped real songs (e.g. Funeral Dirge) that
            # lack a scroll item. Level<=65 + the name exclusions above are the era filter;
            # unscribed spells can't be memmed by the tool anyway.
            seen.add(name)
            if is_disc:
                discs.append([name, int(lvl)])
            else:
                is_good = int(good or 0) == 1
                has_dur = int(m['buffduration'] or 0) > 0
                # Pet-summon spells (Mag Elemental lines, Bst warders: SPA 33
                # "Summon Pet"; Nec Cavorting Bones/Leering Corpse/Convoke
                # Shadow lines: SPA 71, the necro-specific pet-summon effect)
                # are goodEffect=1, buffduration=0 -- same signature as a
                # heal -- and were showing up mislabeled as [Heal] in the
                # picker. Checked ahead of the heal/buff/dd/dot split since it
                # identifies by actual effect, not by the same duration/good
                # heuristic that can't tell them apart. Verified live against
                # this DB: Elemental: Water/Fire/Earth/Air and Companion/
                # Guardian Spirit all carry effectid1=33; Cavorting Bones,
                # Convoke Shadow, and Leering Corpse all carry effectid1=71.
                is_pet_summon = any(int(m['effectid%d' % i] or 0) in (33, 71) for i in range(1, 5))
                # Verified against known spells (Flame Bolt=dd, Immolate/Heat
                # Blood/Pyrocruor=dot, Minor/Superior Healing=heal, Skin like
                # Wood/Spirit of Wolf=buff): goodEffect + whether the spell has
                # a duration cleanly separates these four categories.
                if is_pet_summon:
                    kind = 'pet'
                elif is_good:
                    kind = 'buff' if has_dur else 'heal'
                else:
                    kind = 'dot' if has_dur else 'dd'
                spells.append([name, int(lvl), 1 if is_good else 0, kind])
        spells.sort(key=lambda x: (x[1], x[0]))
        discs.sort(key=lambda x: (x[1], x[0]))

        aas, aseen = [], set()
        for name, recast in conn.execute(aa_sql, {'mask': 1 << (cid - 1), 'exp': max_exp, 'glyph': 'Glyph%'}):
            if not name or name.startswith('Unknown') or name in aseen:
                continue
            aseen.add(name)
            aas.append([name, int(recast or 0)])
        aas.sort(key=lambda x: (x[1], x[0]))

        if spells:
            out['spells'][abbr] = spells
        if discs:
            out['discs'][abbr] = discs
        if aas:
            out['aas'][abbr] = aas
        tot_s += len(spells); tot_d += len(discs); tot_a += len(aas)
        print("  %-4s spells %4d  discs %3d  activated-AAs %3d" % (abbr, len(spells), len(discs), len(aas)))

    conn.close()

    json_dest = os.path.join(here, 'triune_data.json')
    with open(json_dest, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, separators=(',', ':'), ensure_ascii=False)
    lua_dest = os.path.join(here, 'triune_data.lua')
    with open(lua_dest, 'w', encoding='utf-8') as fh:
        fh.write('return ' + to_lua(out) + '\n')

    print("\nera expansion = %d" % max_exp)
    print("%d spells, %d discs, %d activated AAs" % (tot_s, tot_d, tot_a))
    print("  " + json_dest)
    print("  " + lua_dest + "   (copy to your MQ config dir)")


if __name__ == '__main__':
    main()
