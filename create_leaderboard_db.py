"""Creates and seeds the Leaderboard V2 database (raid rankings).

This mirrors the pattern of ``create_local_db.py`` but targets the dedicated
``eqdb_leaderboard`` database defined in ``configuration.ini`` under the
``[leaderboard_database]`` section, rather than the local SQLite store.

Run once to build the schema and seed reference data:

    python create_leaderboard_db.py

The schema is the *target* that finished-encounter data is written into.  The
website only ever reads finalized rows (rankings, parses, personal bests) that
the finalization pipeline (see ``leaderboard.py``) has already computed -- it
never calculates rankings live.

All tables are namespaced with an ``lb_`` prefix.  Enum-like fields use short
strings with documented allowed values (kept as plain VARCHAR to avoid the
migration pain of real SQL ENUMs).
"""
import configparser
import os
from datetime import datetime

from sqlalchemy import (create_engine, Column, Integer, BigInteger, String, Text,
                        Boolean, DateTime, Numeric, JSON, ForeignKey, Index,
                        UniqueConstraint, func)
from sqlalchemy.orm import declarative_base, Session


here = os.path.dirname(__file__)
site_config = configparser.RawConfigParser()
site_config.read_file(open(os.path.join(here, 'configuration.ini')))
# Env var wins over configuration.ini so Docker Compose / .env can inject the real
# credentials in production without baking them into the config file in the image;
# locally the [leaderboard_database] connection is used.
LEADERBOARD_URL = (
    os.environ.get('LEADERBOARD_DB_URL')
    or site_config.get('leaderboard_database', 'connection', fallback='')
    # Nothing configured: a local SQLite file, so the site starts with empty boards.
    or 'sqlite:///leaderboard_db.db')

engine = create_engine(LEADERBOARD_URL, pool_pre_ping=True, future=True)
Base = declarative_base()


# --------------------------------------------------------------------------- #
# Reference / configuration tables                                            #
# --------------------------------------------------------------------------- #
class Boss(Base):
    """A supported encounter.  Each boss is completely independent -- rankings
    are never combined across bosses."""
    __tablename__ = 'lb_boss'
    id = Column(Integer, primary_key=True)
    boss_key = Column(String(64), unique=True, nullable=False)   # e.g. 'kerafyrm'
    name = Column(String(128), nullable=False)                   # display name
    is_gauntlet = Column(Boolean, nullable=False, default=False) # Plane of Time
    # 'raid' | 'mission'. Monster Missions are solo, form-normalized runs: they get
    # their own section and never count toward the raid parse score.
    kind = Column(String(16), nullable=False, default='raid', server_default='raid')
    sort_order = Column(Integer, nullable=False, default=0)
    active = Column(Boolean, nullable=False, default=True)


class BossVersion(Base):
    """A tuned version of a boss.  Parses only compare within the same version
    (a re-tune creates a new version so old records are never cross-compared)."""
    __tablename__ = 'lb_boss_version'
    id = Column(Integer, primary_key=True)
    boss_id = Column(Integer, ForeignKey('lb_boss.id'), nullable=False)
    version_key = Column(String(64), nullable=False)             # e.g. 'v1'
    label = Column(String(128), nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    __table_args__ = (UniqueConstraint('boss_id', 'version_key',
                                       name='uq_bossversion_boss_key'),)


class Season(Base):
    """A ranking season.  Parses never compare across seasons.

    A kill belongs to the season whose [started_at, ended_at) window holds its
    end time (see season_at) -- not whichever season happens to be active when
    the ingest runs -- so a closed season is frozen: nothing that happened after
    its end can ever land in it."""
    __tablename__ = 'lb_season'
    id = Column(Integer, primary_key=True)
    season_key = Column(String(64), unique=True, nullable=False)
    name = Column(String(128), nullable=False)
    started_at = Column(DateTime)
    ended_at = Column(DateTime)
    active = Column(Boolean, nullable=False, default=True)


class Ruleset(Base):
    """A rules version.  Parses never compare across rulesets."""
    __tablename__ = 'lb_ruleset'
    id = Column(Integer, primary_key=True)
    ruleset_key = Column(String(64), unique=True, nullable=False)
    name = Column(String(128), nullable=False)
    notes = Column(Text)
    active = Column(Boolean, nullable=False, default=True)


class ParseColor(Base):
    """Configurable Warcraft-Logs-style parse colors, keyed by percentile band."""
    __tablename__ = 'lb_parse_color'
    id = Column(Integer, primary_key=True)
    min_pct = Column(Numeric(5, 2), nullable=False)
    max_pct = Column(Numeric(5, 2), nullable=False)
    label = Column(String(64), nullable=False)
    color_hex = Column(String(9), nullable=False)               # '#RRGGBB'
    sort_order = Column(Integer, nullable=False, default=0)


class ItemScore(Base):
    """Precomputed raw gear-score contribution per wearable item, built offline
    from the content DB by build_gear_scores.py.  Ingest sums these for a
    character's equipped items instead of joining the huge content items table
    live, then normalizes against Meta['reference_max']."""
    __tablename__ = 'lb_item_score'
    item_id = Column(Integer, primary_key=True)
    raw_score = Column(Numeric(14, 2), nullable=False)


class ItemEffect(Base):
    """Season 2 effect values per item with a click buff or a proc, built by build_gear_scores.py
    (effect_score.rebuild) from the content DB. click_vec is JSON {spell effect id: weighted value}."""
    __tablename__ = 'lb_item_effect'
    item_id = Column(Integer, primary_key=True)
    click_spell = Column(Integer, nullable=False, default=0)
    click_vec = Column(Text, nullable=False, default='{}')
    proc_spell = Column(Integer, nullable=False, default=0)
    proc_value = Column(Numeric(14, 2), nullable=False, default=0)


class Meta(Base):
    """Small key/value store for leaderboard-wide derived constants, e.g.
    'reference_max' (the raw score a full best-in-slot set reaches, = 1000 on
    the normalized scale) and the calibration BiS item-id lists."""
    __tablename__ = 'lb_meta'
    meta_key = Column(String(64), primary_key=True)
    meta_value = Column(Text, nullable=False)


# --------------------------------------------------------------------------- #
# Core encounter data                                                         #
# --------------------------------------------------------------------------- #
class Encounter(Base):
    """One completed run of a boss by a Solo/Duo/Trio group.

    ``duration_seconds`` is the official speed metric (for Plane of Time this is
    the full gauntlet: travel + downtime + progression + everything, from first
    gate boss engage to Quarm's death).  ``combat_time_seconds`` is the summed
    active-boss-combat window used for DPS (never the whole gauntlet)."""
    __tablename__ = 'lb_encounter'
    id = Column(Integer, primary_key=True)

    boss_id = Column(Integer, ForeignKey('lb_boss.id'), nullable=False)
    boss_version_id = Column(Integer, ForeignKey('lb_boss_version.id'), nullable=False)
    season_id = Column(Integer, ForeignKey('lb_season.id'), nullable=False)
    ruleset_id = Column(Integer, ForeignKey('lb_ruleset.id'), nullable=False)

    group_size = Column(Integer, nullable=False)                # 1=Solo 2=Duo 3=Trio

    # Validation / provenance signals (from the game server)
    zone_id = Column(Integer)
    instance_id = Column(Integer)
    spawn_id = Column(Integer)
    # UNIQUE: prevents the same kill from being stored twice.
    # NULL is allowed and never conflicts, so hand-submitted encounters without a
    # server uid are unaffected.
    server_encounter_uid = Column(String(128), unique=True)     # server-side unique id
    source_signature = Column(String(160), index=True)          # dedupe / duplicate-completion key

    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    duration_seconds = Column(Numeric(12, 3), nullable=False)   # SPEED metric
    combat_time_seconds = Column(Numeric(12, 3))                # active boss combat (DPS denom)

    total_boss_damage = Column(BigInteger, nullable=False, default=0)
    total_pet_damage = Column(BigInteger, nullable=False, default=0)
    encounter_dps = Column(BigInteger)                          # boss dmg / duration
    active_dps = Column(BigInteger)                             # boss dmg / combat_time
    deaths = Column(Integer, nullable=False, default=0)

    # 'pending' | 'valid' | 'invalid' | 'flagged'
    validation_status = Column(String(16), nullable=False, default='pending')

    raw_payload = Column(JSON)                                  # original ingest, for staff review/recalc

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    finalized_at = Column(DateTime)

    __table_args__ = (
        Index('ix_encounter_scope', 'boss_id', 'group_size', 'season_id',
              'ruleset_id', 'boss_version_id', 'validation_status'),
        Index('ix_encounter_recent', 'boss_id', 'group_size', 'end_time'),
    )


class Participant(Base):
    """A character that took part in an encounter."""
    __tablename__ = 'lb_participant'
    id = Column(Integer, primary_key=True)
    encounter_id = Column(Integer, ForeignKey('lb_encounter.id'), nullable=False, index=True)

    character_id = Column(Integer, index=True)
    character_name = Column(String(64), nullable=False)
    class_id = Column(Integer)
    class_name = Column(String(32))
    class_bitmask = Column(Integer, index=True)   # GestaltClasses trio bitmask -- 'vs your build' parse scope

    gear_score = Column(Integer)
    boss_damage = Column(BigInteger, nullable=False, default=0)
    pet_damage = Column(BigInteger, nullable=False, default=0)
    total_damage = Column(BigInteger, nullable=False, default=0)
    encounter_dps = Column(BigInteger)
    active_dps = Column(BigInteger)
    deaths = Column(Integer, nullable=False, default=0)

    is_valid = Column(Boolean, nullable=False, default=True)
    notes = Column(Text)


class DamageSegment(Base):
    """An active-combat window within an encounter.  For the Plane of Time
    gauntlet these are the per-boss combat segments used to compute Active DPS
    (trash / travel / idle time is excluded).  ``participant_id`` NULL means the
    segment is the group-level (whole-encounter) aggregate."""
    __tablename__ = 'lb_damage_segment'
    id = Column(Integer, primary_key=True)
    encounter_id = Column(Integer, ForeignKey('lb_encounter.id'), nullable=False, index=True)
    participant_id = Column(Integer, ForeignKey('lb_participant.id'))
    segment_name = Column(String(64), nullable=False)           # e.g. 'Saryrn', 'Quarm'
    sequence = Column(Integer, nullable=False, default=0)
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    duration_seconds = Column(Numeric(12, 3))
    damage = Column(BigInteger, nullable=False, default=0)
    dps = Column(BigInteger)


class Parse(Base):
    """A computed percentile parse (0.00-100.00) for one performance, scoped so
    that comparisons only ever happen within the same boss + group size + boss
    version + season + ruleset.  ``participant_id`` NULL means a group parse."""
    __tablename__ = 'lb_parse'
    id = Column(Integer, primary_key=True)
    encounter_id = Column(Integer, ForeignKey('lb_encounter.id'), nullable=False, index=True)
    participant_id = Column(Integer, ForeignKey('lb_participant.id'))

    # 'individual_dps' | 'group_dps' | 'speed'
    parse_type = Column(String(16), nullable=False)

    # Comparison scope (denormalized from the encounter for fast, correct scoping)
    boss_id = Column(Integer, nullable=False)
    group_size = Column(Integer, nullable=False)
    boss_version_id = Column(Integer, nullable=False)
    season_id = Column(Integer, nullable=False)
    ruleset_id = Column(Integer, nullable=False)

    raw_metric = Column(Numeric(20, 3), nullable=False)         # DPS, or seconds for speed
    percentile = Column(Numeric(5, 2), nullable=False)          # 0.00 - 100.00
    comparison_count = Column(Integer, nullable=False, default=0)
    # 'not_enough_data' (<5) | 'provisional' (5-9) | 'official' (10+)
    tier = Column(String(20), nullable=False, default='not_enough_data')
    calculated_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index('ix_parse_scope', 'parse_type', 'boss_id', 'group_size',
              'boss_version_id', 'season_id', 'ruleset_id'),
    )


class PersonalBest(Base):
    """Per-character bests for a boss + group size (+ season + ruleset).
    Historical encounters are never overwritten -- this is a rolling summary."""
    __tablename__ = 'lb_personal_best'
    id = Column(Integer, primary_key=True)
    character_id = Column(Integer, index=True)
    character_name = Column(String(64), nullable=False)
    boss_id = Column(Integer, nullable=False)
    group_size = Column(Integer, nullable=False)
    season_id = Column(Integer, nullable=False)
    ruleset_id = Column(Integer, nullable=False)

    best_dps_parse = Column(Numeric(5, 2))
    best_speed_parse = Column(Numeric(5, 2))
    best_group_parse = Column(Numeric(5, 2))
    best_raw_dps = Column(BigInteger)
    fastest_kill_seconds = Column(Numeric(12, 3))
    total_valid_kills = Column(Integer, nullable=False, default=0)

    latest_parse_id = Column(Integer, ForeignKey('lb_parse.id'))
    latest_parse_at = Column(DateTime)
    updated_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint('character_id', 'boss_id', 'group_size', 'season_id',
                         'ruleset_id', name='uq_pb_char_scope'),
    )


class RankingCache(Base):
    """Precomputed leaderboard rows the website reads directly.  Rebuilt by the
    finalization pipeline; never computed live on page load.  ``payload`` holds
    the denormalized display row (players, classes, time, parse, gear, etc.)."""
    __tablename__ = 'lb_ranking_cache'
    id = Column(Integer, primary_key=True)
    boss_id = Column(Integer, nullable=False)
    group_size = Column(Integer, nullable=False)
    # 'speed' | 'individual_dps' | 'group_dps' | 'recent'
    category = Column(String(20), nullable=False)
    season_id = Column(Integer, nullable=False)
    ruleset_id = Column(Integer, nullable=False)

    rank_position = Column(Integer, nullable=False)             # 'rank' is reserved in MariaDB
    encounter_id = Column(Integer, ForeignKey('lb_encounter.id'))
    participant_id = Column(Integer, ForeignKey('lb_participant.id'))
    metric_value = Column(Numeric(20, 3))
    payload = Column(JSON)
    built_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index('ix_rankcache_lookup', 'boss_id', 'group_size', 'category',
              'season_id', 'ruleset_id', 'rank_position'),
    )


class ValidationFlag(Base):
    """A reason an encounter was invalidated or flagged for staff review.
    Invalid encounters are kept forever, never deleted."""
    __tablename__ = 'lb_validation_flag'
    id = Column(Integer, primary_key=True)
    encounter_id = Column(Integer, ForeignKey('lb_encounter.id'), nullable=False, index=True)
    flag_code = Column(String(64), nullable=False)              # a validation flag code
    severity = Column(String(16), nullable=False, default='warning')  # info|warning|critical
    auto = Column(Boolean, nullable=False, default=True)        # auto-detected vs staff-added
    details = Column(Text)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class StaffActionLog(Base):
    """Immutable audit log of every staff action on the leaderboard."""
    __tablename__ = 'lb_staff_action_log'
    id = Column(Integer, primary_key=True)
    staff_user_id = Column(String(64))
    staff_user_name = Column(String(128))
    action = Column(String(64), nullable=False)                 # e.g. 'invalidate_encounter'
    encounter_id = Column(Integer)
    details = Column(JSON)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


# --------------------------------------------------------------------------- #
# Seed data                                                                   #
# --------------------------------------------------------------------------- #
SEED_BOSSES = [
    # (boss_key, name, is_gauntlet, sort_order)
    ('kerafyrm', 'Kerafyrm the Sleeper', False, 10),
    ('seru', 'Lord Inquisitor Seru', False, 20),
    ('plane_of_time', 'Plane of Time (Full Gauntlet)', True, 30),
    ('zebuxoruk', 'Zebuxoruk', False, 40),
]

# Monster Missions -- one per LDoN camp, solo only. Keys match
# the LDoN theme keys and monster_mission_runs.mission_key ('mm_' + theme key).
SEED_MISSIONS = [
    # (boss_key, name, sort_order)
    ('mm_guk', 'Monster Mission: The Drowned Choir', 110),          # Deepest Guk, South Ro camp
    ('mm_mir', "Monster Mission: The Keeper's Last Beast", 120),    # Miragul's Menagerie, Everfrost
    ('mm_mmc', 'Monster Mission: Blood Debt', 130),                 # Mistmoore Catacombs, Butcherblock
    ('mm_ruj', 'Monster Mission: The Last Warband', 140),           # Rujarkian Hills, East Commonlands
    ('mm_tak', 'Monster Mission: What Wears the Shape', 150),       # Takish-Hiz, North Ro
]

# LDoN raids -- one board per raid, ranked on clear time (no per-player damage is recorded).
# Keys are 'ldon_' + the raid's zone, matching ldon_raid_runs.raid_key (a server-side
# table written by the raid quest scripts).
SEED_LDON_RAIDS = [
    # (boss_key, name, sort_order)
    ('ldon_guke', 'Deepest Guk: The Curse Reborn', 210),
    ('ldon_gukg', 'Deepest Guk: Ritualist of Hate', 211),
    ('ldon_gukc', 'Deepest Guk: The Rescue', 212),
    ('ldon_mirb', "Miragul's Menagerie: Frozen Nightmare", 220),
    ('ldon_mirc', "Miragul's Menagerie: Spider Den", 221),
    ('ldon_mirg', "Miragul's Menagerie: Folly of Miragul's Ambition", 222),
    ('ldon_mmcc', "Mistmoore's Catacombs: Struggles within the Progeny", 230),
    ('ldon_mmcf', "Mistmoore's Catacombs: Scion Lair of Fury", 231),
    ('ldon_rujg', 'The Rujarkian Hills: Hidden Vale of Deceit', 240),
    ('ldon_rujd', 'The Rujarkian Hills: Prison Break', 241),
    ('ldon_rujj', 'The Rujarkian Hills: War March of Imal Ojun', 242),
    ('ldon_takc', 'Takish-Hiz: Within the Compact', 250),
    ('ldon_taka', 'Takish-Hiz: Sunken Library', 251),
    ('ldon_take', 'Takish-Hiz: The Palace Grounds', 252),
]

# Season boundaries. The database clock is UTC (player_event_logs.created_at).
# Season 2 starts at the LDoN release. Override with LEADERBOARD_SEASON2_START
# (ISO 8601, UTC) if the launch moves. Season 1 starts at the leaderboard's
# original clean-slate cutoff.
SEASON_1_START = datetime(2026, 7, 27, 0, 0, 0)
SEASON_2_START = datetime.fromisoformat(
    os.environ.get('LEADERBOARD_SEASON2_START') or '2026-10-02T04:15:00')
SEASONS = [
    # (season_key, name, started_at, ended_at)
    ('season-1', 'Season 1', SEASON_1_START, SEASON_2_START),
    ('season-2', 'Season 2', SEASON_2_START, None),
]

# Warcraft-Logs-style bands.  Colors are configurable -- edit rows in lb_parse_color.
SEED_PARSE_COLORS = [
    # (min_pct, max_pct, label, color_hex, sort_order)
    (100, 100, 'Legend', '#E5CC80', 10),
    (99,  99,  'Pink',   '#E268A8', 20),
    (95,  98,  'Orange', '#FF8000', 30),
    (75,  94,  'Purple', '#A335EE', 40),
    (50,  74,  'Blue',   '#0070DD', 50),
    (25,  49,  'Green',  '#1EFF00', 60),
    (0,   24,  'Gray',   '#666666', 70),
]


def season_at(session, when):
    """The season a moment belongs to: the one whose [started_at, ended_at) holds
    ``when``. Open-ended bounds (NULL) count as unbounded. Falls back to the
    newest active season when no window matches (e.g. a season row with no dates
    on an install that predates dated seasons)."""
    if when is not None and getattr(when, 'tzinfo', None) is not None:
        when = when.replace(tzinfo=None)      # stored datetimes are naive UTC
    if when is not None:
        for s in (session.query(Season)
                  .order_by(Season.started_at.desc(), Season.id.desc()).all()):
            if s.started_at is not None and when < s.started_at:
                continue
            if s.ended_at is not None and when >= s.ended_at:
                continue
            return s
    return (session.query(Season).filter_by(active=True)
            .order_by(Season.id.desc()).first())


def _seed_seasons(session):
    """Create or correct the dated season rows. Only the dates, name and active
    flag are managed; ids never change, so existing encounters stay attached."""
    for key, name, start, end in SEASONS:
        s = session.query(Season).filter_by(season_key=key).first()
        if not s:
            s = Season(season_key=key, name=name)
            session.add(s)
        s.name = name
        s.started_at = start
        s.ended_at = end
        s.active = True


def _seed_ldon_raids(session):
    for boss_key, name, order in SEED_LDON_RAIDS:
        boss = session.query(Boss).filter_by(boss_key=boss_key).first()
        if not boss:
            boss = Boss(boss_key=boss_key, name=name, is_gauntlet=False,
                        kind='raid', sort_order=order, active=True)
            session.add(boss)
            session.flush()
        boss.name = name
        if not session.query(BossVersion).filter_by(boss_id=boss.id, version_key='v1').first():
            session.add(BossVersion(boss_id=boss.id, version_key='v1',
                                    label='Version 1', active=True))


def _seed_missions(session):
    for boss_key, name, order in SEED_MISSIONS:
        boss = session.query(Boss).filter_by(boss_key=boss_key).first()
        if not boss:
            boss = Boss(boss_key=boss_key, name=name, is_gauntlet=False,
                        kind='mission', sort_order=order, active=True)
            session.add(boss)
            session.flush()
        boss.kind = 'mission'
        boss.name = name          # the real mission names replaced the per-camp placeholders
        if not session.query(BossVersion).filter_by(boss_id=boss.id, version_key='v1').first():
            session.add(BossVersion(boss_id=boss.id, version_key='v1',
                                    label='Version 1', active=True))


def seed(session):
    """Idempotently insert reference data (safe to run repeatedly)."""
    # Seasons + ruleset
    _seed_seasons(session)
    if not session.query(Ruleset).filter_by(ruleset_key='ruleset-1').first():
        session.add(Ruleset(ruleset_key='ruleset-1', name='Ruleset 1',
                            notes='Initial Solo/Duo/Trio ruleset.', active=True))
    session.flush()

    # Bosses + a default 'v1' version each
    for boss_key, name, is_gauntlet, order in SEED_BOSSES:
        boss = session.query(Boss).filter_by(boss_key=boss_key).first()
        if not boss:
            boss = Boss(boss_key=boss_key, name=name, is_gauntlet=is_gauntlet,
                        sort_order=order, active=True)
            session.add(boss)
            session.flush()
        if not session.query(BossVersion).filter_by(boss_id=boss.id, version_key='v1').first():
            session.add(BossVersion(boss_id=boss.id, version_key='v1',
                                    label='Version 1', active=True))
    _seed_missions(session)
    _seed_ldon_raids(session)

    # Parse colors
    for min_pct, max_pct, label, color_hex, order in SEED_PARSE_COLORS:
        if not session.query(ParseColor).filter_by(label=label).first():
            session.add(ParseColor(min_pct=min_pct, max_pct=max_pct, label=label,
                                   color_hex=color_hex, sort_order=order))
    session.commit()


def _ensure_columns():
    """Idempotent column top-ups for EXISTING installs. ``create_all`` creates
    missing tables but never ALTERs existing ones, so a newly-added column (e.g.
    lb_participant.class_bitmask) must be added explicitly. Runs on import so a
    deploy self-heals on restart -- no manual migration required. Fully guarded:
    a missing table, missing privilege, or unreachable DB is a silent no-op."""
    from sqlalchemy import text
    ddl = ("ALTER TABLE lb_participant ADD COLUMN IF NOT EXISTS class_bitmask INT",
           "ALTER TABLE lb_boss ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'raid'")
    with engine.begin() as conn:
        for stmt in ddl:
            try:
                conn.execute(text(stmt))
            except Exception:
                pass


# Self-heal the schema on import so a deploy never needs a hand-run ALTER for a
# new column (create_all only creates missing tables, it never alters existing).
try:
    _ensure_columns()
except Exception:
    pass


def _ensure_reference():
    """Seasons and Monster Mission boards, self-healed on import for the same
    reason as _ensure_columns: the full seed() is a hand-run script, and a deploy
    must not depend on someone remembering to run it before Season 2 opens.
    Guarded -- a failure here leaves the site running on what it already has."""
    with Session(bind=engine) as session:
        _seed_seasons(session)
        _seed_missions(session)
        _seed_ldon_raids(session)
        session.commit()


try:
    _ensure_reference()
except Exception:
    pass


def main():
    Base.metadata.create_all(engine)
    _ensure_columns()
    _ensure_reference()
    with Session(bind=engine) as session:
        seed(session)
    print('Leaderboard schema created and seeded.')


if __name__ == '__main__':
    main()
