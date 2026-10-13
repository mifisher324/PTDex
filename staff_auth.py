"""Staff-only access for Discord-signed-in users.

Config-driven: a user is staff when their numeric Discord id is listed in
``[leaderboard] staff_discord_ids`` (or the LEADERBOARD_STAFF_IDS env var).
In the 'Development' site_type the decorated routes are open for local testing.
"""
from functools import wraps

from flask import current_app, request


def _current_staff():
    """Return {'id', 'name'} for the signed-in Discord user, or None."""
    try:
        from eqdb import discord
        if discord.is_user_logged_in():
            u = discord.fetch_user()
            return {'id': u.id, 'name': u.name}
    except Exception:
        pass
    return None


def _staff_allowlist():
    raw = current_app.config.get('LEADERBOARD_STAFF_IDS', '') or ''
    return {s.strip() for s in raw.split(',') if s.strip()}


def staff_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if current_app.config.get('SITE_TYPE') == 'Development':
            request.staff = _current_staff() or {'id': 'dev', 'name': 'dev'}
            return fn(*args, **kwargs)
        staff = _current_staff()
        if not staff or str(staff['id']) not in _staff_allowlist():
            # Return a response directly rather than abort(403): the app-wide
            # errorhandler(Exception) would otherwise swallow the HTTPException.
            return 'Forbidden: staff access required.', 403
        request.staff = staff
        return fn(*args, **kwargs)
    return wrapper
