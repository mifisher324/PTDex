"""AA search, AA detail pages and AA hover tooltips. Data layer: aa_info.py."""
from flask import Blueprint, render_template, request, flash, redirect, url_for

import aa_info

bp = Blueprint('aas', __name__, template_folder='templates')


@bp.route('/aa/search', methods=['GET'])
def aa_search():
    """GET form: results render on the same page so a search is a shareable URL
    (/aa/search?class=12&tab=3)."""
    a = request.args
    classes = [int(c) for c in a.getlist('class') if c.isdigit() and 1 <= int(c) <= 16][:3]
    tab = a.get('tab') if (a.get('tab') or '').isdigit() else None
    activation = a.get('activation') if a.get('activation') in ('active', 'passive') else 'any'
    name = (a.get('name') or '').strip()[:64]
    words = (a.get('text') or '').strip()[:64]
    all_eras = a.get('eras') == 'all'
    searched = any(k in a for k in ('name', 'class', 'tab', 'activation', 'text', 'eras'))
    results = None
    if searched:
        results = aa_info.search(name=name, class_ids=classes, tab=tab,
                                 activation=activation, text_query=words,
                                 all_eras=all_eras)
    return render_template('aa_search.html', results=results, classes=classes,
                           tab=tab, activation=activation, name=name, words=words,
                           all_eras=all_eras, CLASS_NAMES=aa_info.CLASS_NAMES,
                           AA_TABS=aa_info.AA_TABS)


@bp.route('/aa/<int:aa_id>', methods=['GET'])
def aa_detail(aa_id):
    data = aa_info.detail(aa_id)
    if not data:
        flash('AA not found.')
        return redirect(url_for('aas.aa_search'))
    return render_template('aa_detail.html', aa=data)


@bp.route('/aa-tooltip/<int:aa_id>', methods=['GET'])
def aa_tooltip(aa_id):
    data = aa_info.detail(aa_id)
    if not data:
        return '<div class="aa-tip">AA not found.</div>'
    return render_template('aa_tooltip.html', aa=data)
