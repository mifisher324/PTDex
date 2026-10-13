"""One pooled database connection per request.

Every page borrows ONE connection from each engine's pool the first time it
runs a query, every helper it calls uses that same connection, and it goes
back to the pool when the request ends (teardown_appcontext, also on errors).
A busy pool makes new requests wait their turn; no request ever holds one
connection while waiting on a second, so the pool cannot deadlock.

Outside a request (startup jobs, background threads, scripts) each `with`
opens its own short session and closes it on exit, as before.
"""
from contextlib import contextmanager

from flask import g, has_request_context
from sqlalchemy.orm import Session


def in_request():
    return has_request_context()


def session(engine):
    """The request's shared session for this engine, or a new one outside a request
    (the caller then owns it and must close it)."""
    if not has_request_context():
        return Session(bind=engine)
    pool = g.get('_db_sessions')
    if pool is None:
        pool = g._db_sessions = {}
    s = pool.get(id(engine))
    if s is None:
        s = pool[id(engine)] = Session(bind=engine)
    return s


@contextmanager
def get_db(engine):
    """`with get_db(engine) as s:` -- the request's shared session (left open for
    the rest of the request), or a short session of its own outside a request."""
    if not has_request_context():
        with Session(bind=engine) as s:
            yield s
        return
    s = session(engine)
    try:
        yield s
    except BaseException:
        # leave the shared session usable for whatever the page does next
        try:
            s.rollback()
        except Exception:
            pass
        raise


@contextmanager
def connection(engine):
    """`with connection(engine) as c:` -- a Core connection: the request's shared
    one, or engine.connect() outside a request."""
    if not has_request_context():
        with engine.connect() as c:
            yield c
        return
    with get_db(engine) as s:
        yield s.connection()


def close_request_sessions(exc=None):
    """teardown_appcontext hook: return every connection this request borrowed."""
    sessions = g.pop('_db_sessions', None)
    if not sessions:
        return
    for s in sessions.values():
        try:
            if exc is not None:
                s.rollback()
        except Exception:
            pass
        finally:
            s.close()


def init_app(app):
    app.teardown_appcontext(close_request_sessions)
