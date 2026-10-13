"""Guild Portal routes -- directory, guild pages, and officer editing.

Permission model: officer status is re-derived from the game database on EVERY
request (guild_portal.manageable_guilds). Nothing about who may edit is stored,
so promoting or removing someone in-game takes effect on their next page load.
"""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify)

import guild_portal as gp
import guild_achievements as ga
import local
# The PTUI emblem -- the same mark the in-game all-in-one chat bar draws
# (NMSTheme::DrawEmblem). Imported rather than re-drawn so there is only ever one
# definition to correct.
from routes.characters import PTUI_EMBLEM_SVG

bp = Blueprint('guilds', __name__, template_folder='templates')

MAX_BLURB = 1200


@bp.app_template_filter('host')
def _host_filter(url):
    """Hostname of an officer-supplied link, shown beside it so nobody clicks
    blind on a URL a stranger typed into a public page."""
    return gp.host_of(url)


def _user():
    """The signed-in Discord identity, or None. Imported lazily because eqdb
    imports this module during app construction."""
    from eqdb import _session_user
    return _session_user()


def _discord_id():
    u = _user()
    return u.get('id') if u else None


@bp.route("/guilds", methods=['GET'])
def guild_portal():
    """The directory. Public -- no login needed."""
    try:
        ach = ga.for_all()             # cached; list_guilds shares the same pass
    except Exception:
        ach = {}                       # a counter problem must not blank the page
    guilds = gp.directory()
    profiles = local.get_guild_profiles()
    mine = gp.manageable_guilds(_discord_id())

    for g in guilds:
        a = ach.get(g['id'])
        g['ach_top'] = [x for x in (a['list'] if a else []) if x['level']][:4]
        p = profiles.get(g['id'], {})
        g['banner_svg'] = gp.render_banner(p.get('banner'), width=54, uid='g%d' % g['id'])
        g['recruiting'] = bool(p.get('recruiting'))
        g['blurb'] = (p.get('blurb') or '').strip()
        g['claimed'] = bool(p)
        g['can_manage'] = g['id'] in mine

    only = request.args.get('recruiting')
    if only in ('1', 'true', 'yes'):
        guilds = [g for g in guilds if g['recruiting']]

    totals = {
        'guilds': len(guilds),
        'members': sum(g['members'] for g in guilds),
        'active': sum(g['active'] for g in guilds),
        'recruiting': sum(1 for g in guilds if g['recruiting']),
    }
    return render_template('guild_portal.html', guilds=guilds, totals=totals,
                           sigil=PTUI_EMBLEM_SVG, manage_count=len(mine),
                           recruiting_only=(only in ('1', 'true', 'yes')))


@bp.route("/guild/<int:guild_id>", methods=['GET'])
def guild_detail(guild_id):
    g = gp.get_guild(guild_id)
    if not g:
        flash("That guild could not be found.")
        return redirect(url_for('.guild_portal'))

    p = local.get_guild_profile(guild_id)
    banner_cfg = gp.normalize_banner(p.get('banner'))
    try:
        ach = ga.for_guild(guild_id)
    except Exception:
        ach = None
    try:
        contrib = gp.contributors(guild_id)
    except Exception:
        contrib = None
    return render_template(
        'guild_detail.html', g=g, profile=p, ach=ach, contrib=contrib,
        banner_cfg=banner_cfg,
        banner_svg=gp.render_banner(banner_cfg, width=150, uid='hero'),
        blurb=(p.get('blurb') or '').strip(),
        recruiting=bool(p.get('recruiting')),
        can_manage=gp.can_manage(_discord_id(), guild_id),
        signed_in=bool(_discord_id()),
        shapes=list(gp.BANNER_SHAPES), fields=list(gp.BANNER_FIELDS),
        colors=gp.BANNER_COLORS, metals=gp.BANNER_METALS,
        charges=[(k, v[0]) for k, v in gp.BANNER_CHARGES.items()])


@bp.route("/guild/<int:guild_id>/save", methods=['POST'])
def guild_save(guild_id):
    """Officer-only. Re-checks rank against the game DB right here -- never trusts
    a hidden field, a session flag, or anything the form supplied."""
    # NB: return 403 rather than abort(403). eqdb registers errorhandler(Exception),
    # which catches HTTPException too and re-raises it as a 500 in Development --
    # so a raised abort would surface as a site error instead of a clean refusal.
    did = _discord_id()
    if not did or not gp.can_manage(did, guild_id):
        return ("Only a Leader or Senior Officer of this guild may edit its page.", 403)
    # Resolve WHO is editing, by character, so the change is attributable.
    editor = gp.officer_identity(did, guild_id)

    f = request.form
    banner = gp.normalize_banner({k: f.get(k) for k in
                                  ('shape', 'field', 'c1', 'c2', 'charge', 'metal')})
    blurb = (f.get('blurb') or '').strip()[:MAX_BLURB]
    recruiting = 1 if f.get('recruiting') else 0
    # No outbound website field: the guild page IS their site here, so a link off
    # it is redundant -- and every external URL an officer can publish is another
    # phishing surface to police.
    links = {
        'discord': gp.clean_discord(f.get('discord')),
        'contact': (f.get('contact') or '').strip()[:80],
    }
    # Say so when a link is rejected rather than silently blanking it -- a typo'd
    # invite would otherwise just vanish and read as a site bug.
    rejected = bool((f.get('discord') or '').strip()) and not links['discord']

    import json
    local.save_guild_profile(guild_id, did, banner=json.dumps(banner), blurb=blurb,
                             recruiting=recruiting, editor=editor, links=links)
    msg = "Guild page updated%s." % ((" — signed %s" % editor['name']) if editor else "")
    if rejected:
        msg += "  Your Discord link was not saved: it must be a discord.gg invite."
    flash(msg)
    return redirect(url_for('.guild_detail', guild_id=guild_id))


@bp.route("/guild/banner/preview", methods=['GET'])
def banner_preview():
    """Live preview for the builder. Read-only and fully sanitised, so it is safe
    to leave open -- it renders whitelisted enums and touches no storage."""
    cfg = gp.normalize_banner({k: request.args.get(k) for k in
                               ('shape', 'field', 'c1', 'c2', 'charge', 'metal')})
    return jsonify({'svg': gp.render_banner(cfg, width=150, uid='hero'), 'cfg': cfg})
