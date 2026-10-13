from db import get_db
import os
import re

from flask import Blueprint, render_template, request, flash, redirect, url_for, abort, jsonify, session
from sqlalchemy.orm import Session

import local
import npc
import utils

npc_pages = Blueprint('npcs', __name__, template_folder='templates')


@npc_pages.route("/search/npc", methods=['GET', 'POST'])
def npc_search():
    if request.method == 'GET':
        return render_template('npc_search.html')
    else:
        npc_name = request.form['npc_name']
        if len(npc_name) > 50:
            flash('Search by name limited to 50 characters.')
            return redirect(url_for('.npc_search'))
        elif len(npc_name) < 3:
            flash('Search by name requires at least 3 characters')
            return redirect(url_for('.npc_search'))
        if not npc_name.isascii():
            flash('Only ASCII characters are allowed.')
            return redirect(url_for('.npc_search'))
        data = npc.get_npcs(npc_name)
        return render_template('npc_search_result.html', data=data)


@npc_pages.route("/npc/raw/<int:npc_id>")
def npc_raw(npc_id):
    raw_data = npc.get_npc_raw_data(npc_id)
    return render_template('raw_data.html', data=raw_data)


@npc_pages.route("/npc/map/<short_name>.json")
def npc_map(short_name):
    # zone short names only: no path separators or dots can reach the file open
    if not re.fullmatch(r'[a-z0-9_]{1,32}', short_name) or \
            not os.path.isfile(os.path.join(utils.here, 'maps', f'{short_name}.txt')):
        abort(404)
    lines = [[m['x1'], m['y1'], m['x2'], m['y2'], m['rgb']] for m in utils.get_map_data(short_name)]
    resp = jsonify(lines)
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


QUALITY_OFFSET = {'Enchanted': 1000000, 'Legendary': 2000000}


def _quality_map(data):
    """Base loot item id -> the id of the version the signed-in user prefers, where that version exists."""
    du = session.get('duser')
    offset = QUALITY_OFFSET.get(local.get_item_quality(du['id']) if du else 'Base')
    if not offset or not data:
        return {}
    base_ids = {i['item_id'] for ll in (data.get('loot_lists') or {}).values() for i in ll['items']
                if i['item_id'] < 1000000}
    if not base_ids:
        return {}
    with get_db(npc.engine) as s:
        have = {r[0] for r in s.query(npc.Item.id).filter(npc.Item.id.in_([b + offset for b in base_ids])).all()}
    return {b: b + offset for b in base_ids if b + offset in have}


@npc_pages.route("/npc/detail/<int:npc_id>")
def npc_detail(npc_id):
    data = npc.get_npc_detail(npc_id)
    return render_template('npc_detail.html', data=data, quality_map=_quality_map(data))
