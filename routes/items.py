from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify

import item
import logic
import spell

item_pages = Blueprint('items', __name__, template_folder='templates')


# ---- side-by-side item compare --------------------------------------------
def _item_gear_points(ids):
    """{item_id: gear-score points} for the given ids, scored the same way the
    character sheet scores a slot (raw score / best-in-slot reference * 1000).
    Best-effort: if the leaderboard DB is unavailable it just returns {}."""
    out = {}
    ids = [i for i in ids if i]
    if not ids:
        return out
    try:
        from create_leaderboard_db import engine as lb_engine, ItemScore
        from gear_score import get_reference_max
        from sqlalchemy.orm import Session as _LB
        with _LB(bind=lb_engine) as lb:
            ref = get_reference_max(lb) or 0
            if ref:
                for iid, raw in (lb.query(ItemScore.item_id, ItemScore.raw_score)
                                 .filter(ItemScore.item_id.in_(ids)).all()):
                    out[iid] = round(float(raw) / ref * 1000, 1)
    except Exception:
        pass
    return out


def _icmp(a, b):
    """Grouped diff rows for two item dicts (either may be None). A row appears
    only if at least one side has a value. 'better=lower' flags delay, where a
    faster weapon wins."""
    def n(d, key):
        if not d:
            return None
        try:
            return int(d.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    def attr(d, base, hero):
        if not d:
            return None
        return n(d, base) + n(d, hero)

    def row(label, av, bv, better='higher'):
        keep = (av not in (None, 0)) or (bv not in (None, 0))
        if not keep:
            return None
        win = None
        if av is not None and bv is not None:
            win = 'tie' if av == bv else ('a' if (av > bv) == (better == 'higher') else 'b')
        return {'label': label, 'a': av, 'b': bv, 'win': win}

    def ratio(d):
        if not d:
            return None
        dmg, dly = n(d, 'damage'), n(d, 'delay')
        return round(dmg / dly * 10, 1) if dly else 0

    sections = []

    def add(name, rows):
        rows = [r for r in rows if r]
        if rows:
            sections.append({'name': name, 'rows': rows})

    add('Vitals', [
        row('AC', n(a, 'ac'), n(b, 'ac')),
        row('HP', n(a, 'hp'), n(b, 'hp')),
        row('Mana', n(a, 'mana'), n(b, 'mana')),
        row('Endurance', n(a, 'endur'), n(b, 'endur')),
    ])
    if n(a, 'delay') or n(b, 'delay'):
        add('Weapon', [
            row('Base Damage', n(a, 'damage'), n(b, 'damage')),
            row('Delay', n(a, 'delay'), n(b, 'delay'), better='lower'),
            row('Ratio (dmg/dly)', ratio(a), ratio(b)),
        ])
    add('Attributes (base + heroic)', [
        row('STR', attr(a, 'astr', 'heroic_str'), attr(b, 'astr', 'heroic_str')),
        row('STA', attr(a, 'asta', 'heroic_sta'), attr(b, 'asta', 'heroic_sta')),
        row('AGI', attr(a, 'aagi', 'heroic_agi'), attr(b, 'aagi', 'heroic_agi')),
        row('DEX', attr(a, 'adex', 'heroic_dex'), attr(b, 'adex', 'heroic_dex')),
        row('WIS', attr(a, 'awis', 'heroic_wis'), attr(b, 'awis', 'heroic_wis')),
        row('INT', attr(a, 'aint', 'heroic_int'), attr(b, 'aint', 'heroic_int')),
        row('CHA', attr(a, 'acha', 'heroic_cha'), attr(b, 'acha', 'heroic_cha')),
    ])
    add('Resists (base + heroic)', [
        row('Magic', attr(a, 'mr', 'heroic_mr'), attr(b, 'mr', 'heroic_mr')),
        row('Fire', attr(a, 'fr', 'heroic_fr'), attr(b, 'fr', 'heroic_fr')),
        row('Cold', attr(a, 'cr', 'heroic_cr'), attr(b, 'cr', 'heroic_cr')),
        row('Poison', attr(a, 'pr', 'heroic_pr'), attr(b, 'pr', 'heroic_pr')),
        row('Disease', attr(a, 'dr', 'heroic_dr'), attr(b, 'dr', 'heroic_dr')),
    ])
    add('Combat & Focus', [
        row('Attack', n(a, 'attack'), n(b, 'attack')),
        row('Haste %', n(a, 'haste'), n(b, 'haste')),
        row('Spell Damage', n(a, 'spelldmg'), n(b, 'spelldmg')),
        row('Heal Amount', n(a, 'healamt'), n(b, 'healamt')),
        row('Accuracy', n(a, 'accuracy'), n(b, 'accuracy')),
        row('Avoidance', n(a, 'avoidance'), n(b, 'avoidance')),
        row('HP Regen', n(a, 'regen'), n(b, 'regen')),
        row('Mana Regen', n(a, 'manaregen'), n(b, 'manaregen')),
        row('Damage Shield', n(a, 'damageshield'), n(b, 'damageshield')),
        row('Shielding', n(a, 'shielding'), n(b, 'shielding')),
        row('Stun Resist', n(a, 'stunresist'), n(b, 'stunresist')),
        row('Strikethrough', n(a, 'strikethrough'), n(b, 'strikethrough')),
    ])
    return sections


@item_pages.route("/item/clicks", methods=['GET', 'POST'])
def click_item_search():
    if request.method == 'GET':
        return render_template('click_items.html')
    else:
        filters = {}
        if 'g_class_1' in request.form:
            if request.form['g_class_1'] != 'None':
                filters.update({'g_class_1': request.form['g_class_1']})
        if 'g_class_2' in request.form:
            if request.form['g_class_2'] != 'None':
                filters.update({'g_class_2': request.form['g_class_2']})
        if 'g_class_3' in request.form:
            if request.form['g_class_3'] != 'None':
                filters.update({'g_class_3': request.form['g_class_3']})
        base_era_list = ['Classic', 'Kunark', 'Velious', 'Luclin', 'Planes']
        remove_era_list = request.form.getlist('eras')
        era_list = []
        for era in base_era_list:
            if era in remove_era_list:
                continue
            else:
                era_list.append(era)
        filters.update({'eras': era_list})
        click_category = request.form['click_category']
        if 'click_type' in request.form:
            click_type = request.form['click_type']
        else:
            click_type = 'None'
        if 'charges' in request.form:
            filters.update({'charges': False})
        filters.update({'min_level': request.form['level']})

        if click_category != 'All' and click_category != 'Pets' and click_type == 'None':
            flash('You must select a click type')
            return redirect(url_for('items.click_item_search'))

        data = item.get_click_items(click_category, click_type, **filters)
        return render_template('click_item_results.html', data=data)


@item_pages.route("/item/search", methods=['GET', 'POST'])
def item_fast_search():
    if request.method == 'GET':
        return render_template('item_fast_search.html')
    else:
        # Do some validation
        item_name = request.form.get('item_name')
        tradeskill = request.form.get('tradeskill')
        if tradeskill == 'none':
            tradeskill = None
        equippable = request.form.get('equippable')
        if equippable == 'none':
            equippable = None
        itype = request.form.get('itype')
        no_glamour = request.form.get('no_glamour')
        only_aug = request.form.get('only_aug')
        containers = request.form.get('containers')
        min_bagslots = request.form.get('min_bagslots', '0')
        min_bagslots = int(min_bagslots) if min_bagslots.isdigit() and int(min_bagslots) <= 40 else 0
        slot = request.form.get('slot') or None
        player_class = request.form.get('player_class') or None
        item_name = (item_name or '').strip()
        # a name is optional when another filter narrows the search
        narrowed = bool(containers or slot in item.FAST_SLOTS or player_class in item.FAST_CLASSES)

        if len(item_name) > 50:
            flash('Search by name limited to 50 characters.')
            return redirect(url_for('.item_fast_search'))
        if len(item_name) < 3 and not (narrowed and len(item_name) == 0):
            flash('Search by name requires at least 3 characters (or leave it empty and pick Containers, a slot or a class)')
            return redirect(url_for('.item_fast_search'))
        if not item_name.isascii():
            flash('Only ASCII characters are allowed.')
            return redirect(url_for('.item_fast_search'))
        data = item.get_fast_item(item_name, tradeskill=tradeskill, equippable=equippable,
                                  itype=itype, no_glamours=no_glamour, only_aug=only_aug,
                                  containers=bool(containers), min_bagslots=min_bagslots,
                                  slot=slot, player_class=player_class)
        return render_template('item_fast_search_result.html', data=data)


@item_pages.route("/item/detail/<int:item_id>")
def item_detail(item_id):
    data = logic.get_item_data(item_id, full=True)
    if data:
        # Attach a readable explanation for each effect (the non-"Limit:" lines)
        # so worn/proc/click/focus effects are explained on the page.
        data['effect_desc'] = {}
        for key in ('worneffect', 'proceffect', 'clickeffect', 'focuseffect', 'bardeffect'):
            sid = data.get(key)
            if not isinstance(sid, int) or sid <= 0:
                continue
            try:
                _, slots = spell.get_spell_data(sid, basic_data=False)
            except Exception:
                slots = None
            lines = []
            for k in sorted((slots or {}), key=lambda x: int(x.split('_')[1])):
                d = (slots[k] or {}).get('desc', '')
                if d and not d.startswith('Limit:'):
                    lines.append(d)
            if lines:
                data['effect_desc'][key] = lines
    return render_template('item_detail.html', item=data, item_id=item_id)


@item_pages.route("/item/raw/<int:item_id>")
def item_raw(item_id):
    raw_data = item.get_item_raw_data(item_id)
    return render_template('raw_data.html', data=raw_data)


@item_pages.route("/item/compare")
def item_compare():
    def _id(param):
        v = (request.args.get(param) or '').strip()
        return int(v) if v.isdigit() else None
    aid, bid = _id('a'), _id('b')
    a = logic.get_item_data(aid, full=True) if aid else None
    b = logic.get_item_data(bid, full=True) if bid else None
    sections = _icmp(a, b) if (a and b) else None
    gp = _item_gear_points([a['id'] if a else None, b['id'] if b else None])
    gpts = None
    if a and b:
        ga, gb = gp.get(a['id']), gp.get(b['id'])
        win = None
        if ga is not None and gb is not None:
            win = 'tie' if ga == gb else ('a' if ga > gb else 'b')
        gpts = {'a': ga, 'b': gb, 'win': win}
    return render_template('item_compare.html', a=a, b=b, aid=aid, bid=bid,
                           sections=sections, gpts=gpts)


@item_pages.route("/item/compare/find")
def item_compare_find():
    """JSON name search for the compare-slot picker. Returns equippable items in
    ALL rarities (Base / Enchanted / Legendary) so an item's upgraded versions
    are pickable -- 'itype=All' skips get_fast_item's id-range filter."""
    q = (request.args.get('q') or '').strip()
    if len(q) < 2 or not q.isascii():
        return jsonify([])
    data = item.get_fast_item(q, equippable='1', itype='All', no_glamours=True) or []

    def rarity(iid):
        if iid >= 2000000:
            return 'Legendary'
        return 'Enchanted' if iid >= 1000000 else 'Base'
    return jsonify([{'id': e['item_id'], 'name': e['name'], 'icon': e['icon'],
                     'rarity': rarity(e['item_id'])} for e in data[:30]])
