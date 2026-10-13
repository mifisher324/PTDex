"""Leaderboard V2 engine.

This module owns everything that turns a finished, ingested encounter into
ranked, comparable leaderboard data:

  * DPS / combat-segment calculation (including the Plane of Time gauntlet rules)
  * A percentile-based parse system (Warcraft-Logs style, 0.00 - 100.00)
  * Encounter validation (auto flags + status)
  * The finalization pipeline that runs when an encounter finishes
  * Personal-bests and cached-ranking rebuilds

Design rules baked in here (from the spec):

  * Rankings are NEVER computed live.  The website reads only ``lb_ranking_cache``
    / ``lb_parse`` / ``lb_personal_best`` rows produced here.
  * Parses only ever compare within the SAME scope:
    (boss, group size, boss version, season, ruleset).
    Solo/Duo/Trio and different bosses/versions/seasons/rulesets are never mixed.
  * Invalid encounters are kept forever (status flipped, never deleted).

The schema/models live in ``create_leaderboard_db.py`` (single source of truth);
we import them here so there is no second definition to drift.
"""
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import and_
from sqlalchemy.orm import Session

from create_leaderboard_db import (engine, Boss, BossVersion, Season, Ruleset,
                                   ParseColor, Encounter, Participant,
                                   DamageSegment, Parse, PersonalBest,
                                   RankingCache, ValidationFlag, StaffActionLog)

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #
GROUP_SIZE_LABELS = {1: 'Solo', 2: 'Duo', 3: 'Trio'}

# Plane of Time gauntlet: the speed timer starts on first engage of whichever of
# these four gate bosses is pulled first, and ends when Quarm dies.
POT_GATE_BOSSES = ('Saryrn', 'Terris Thule', 'Vallon Zek', 'Tallon Zek')
POT_FINAL_BOSS = 'Quarm'

# Parse tier thresholds (by number of valid records in the comparison scope).
TIER_NOT_ENOUGH = 'not_enough_data'   # < 5
TIER_PROVISIONAL = 'provisional'      # 5 - 9
TIER_OFFICIAL = 'official'            # 10+
TIER_MIN_PROVISIONAL = 5
TIER_MIN_OFFICIAL = 10

# Validation status values.
STATUS_PENDING = 'pending'
STATUS_VALID = 'valid'
STATUS_INVALID = 'invalid'
STATUS_FLAGGED = 'flagged'

# Parse categories stored in the ranking cache.
CAT_SPEED = 'speed'
CAT_INDIVIDUAL_DPS = 'individual_dps'
CAT_GROUP_DPS = 'group_dps'
CAT_RECENT = 'recent'

# Encounter auto-validation (the checks that decide whether a submitted encounter
# is valid) is not part of the public build. Without an extension every encounter
# is accepted as submitted; an extension may replace ``leaderboard.validate_encounter``.

TWO_PLACES = Decimal('0.01')
THREE_PLACES = Decimal('0.001')


def new_session():
    return Session(bind=engine)


def group_size_label(group_size):
    return GROUP_SIZE_LABELS.get(group_size, f'{group_size}-player')


# --------------------------------------------------------------------------- #
# Parse math (pure)                                                           #
# --------------------------------------------------------------------------- #
def percentile_rank(value, population, higher_is_better=True):
    """Return a 0.00-100.00 percentile for ``value`` within ``population``.

    ``population`` is the full list of comparable metrics in scope (it includes
    ``value`` itself).  The single best performance scores 100.00; the worst
    scores 0.00.  Ties are scored generously (each tied value is credited with
    beating the others it equals).  With a population of one, the lone
    performance is 100.00 (its tier will still be 'not_enough_data').
    """
    n = len(population)
    if n <= 1:
        return Decimal('100.00')
    if higher_is_better:
        at_or_below = sum(1 for m in population if m <= value)
    else:
        at_or_below = sum(1 for m in population if m >= value)
    beaten = at_or_below - 1                      # exclude self
    if beaten < 0:
        beaten = 0
    pct = Decimal(beaten) / Decimal(n - 1) * Decimal(100)
    return pct.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def tier_for_count(count):
    if count >= TIER_MIN_OFFICIAL:
        return TIER_OFFICIAL
    if count >= TIER_MIN_PROVISIONAL:
        return TIER_PROVISIONAL
    return TIER_NOT_ENOUGH


def parse_color(session, percentile):
    """Return the configured ``ParseColor`` row whose band contains ``percentile``
    (or None). Colors are fully configurable via the lb_parse_color table."""
    if percentile is None:
        return None
    pct = Decimal(percentile)
    return (session.query(ParseColor)
            .filter(and_(ParseColor.min_pct <= pct, ParseColor.max_pct >= pct))
            .order_by(ParseColor.sort_order)
            .first())


# --------------------------------------------------------------------------- #
# DPS / combat-segment calculation                                            #
# --------------------------------------------------------------------------- #
def _dps(damage, seconds):
    """Integer DPS = damage / seconds, guarding against zero/None."""
    if not damage or not seconds:
        return 0
    seconds = Decimal(seconds)
    if seconds <= 0:
        return 0
    return int((Decimal(damage) / seconds).to_integral_value(rounding=ROUND_HALF_UP))


def compute_combat_time(session, encounter):
    """Active combat time for an encounter = summed duration of its group-level
    (participant_id IS NULL) damage segments.  For Plane of Time this excludes
    trash / travel / idle / waiting; only real boss-combat windows count.  If no
    segments were supplied, fall back to any explicit combat_time on the row."""
    segs = (session.query(DamageSegment)
            .filter(DamageSegment.encounter_id == encounter.id,
                    DamageSegment.participant_id.is_(None))
            .all())
    total = Decimal(0)
    for s in segs:
        if s.duration_seconds:
            total += Decimal(s.duration_seconds)
    if total > 0:
        return total
    return Decimal(encounter.combat_time_seconds) if encounter.combat_time_seconds else Decimal(0)


def compute_encounter_metrics(session, encounter):
    """(Re)derive the DPS aggregates for an encounter and its participants.

    * duration_seconds is the speed metric (whole gauntlet for Plane of Time) --
      taken as-is from ingest; we only recompute if absent.
    * combat_time_seconds is the active-boss-combat denominator for DPS.
    * encounter_dps  = total boss damage / duration   (whole-encounter rate)
    * active_dps     = total boss damage / combat time (active-combat rate)
    Per-participant encounter_dps / active_dps are computed the same way.
    """
    participants = (session.query(Participant)
                    .filter(Participant.encounter_id == encounter.id).all())

    # NPC-total-only encounters (player_event_logs raid kills): the source gives
    # group DPS + combat time but no per-player damage. Fill the encounter's group
    # metrics from the reported DPS and leave per-participant DPS unset so these
    # never appear on the Individual DPS board (see the ingest extension).
    if (encounter.raw_payload or {}).get('total_only'):
        group_dps = int((encounter.raw_payload or {}).get('group_dps') or 0)
        combat_time = compute_combat_time(session, encounter)
        encounter.combat_time_seconds = combat_time
        if encounter.duration_seconds is None and encounter.start_time and encounter.end_time:
            encounter.duration_seconds = Decimal(
                (encounter.end_time - encounter.start_time).total_seconds())
        duration = Decimal(encounter.duration_seconds or 0)
        total_dmg = int(group_dps * combat_time) if combat_time else 0
        encounter.total_boss_damage = total_dmg
        encounter.total_pet_damage = 0
        encounter.encounter_dps = _dps(total_dmg, duration)
        encounter.active_dps = group_dps
        # SOLO: the single participant IS the whole group, so the encounter's
        # NPC-total DPS is unambiguously their individual DPS -> they DO get an
        # Individual DPS ranking. Duo/Trio can't be split (no per-player data),
        # so those participants stay unset and are skipped on the Individual board.
        solo = len(participants) == 1
        for p in participants:
            if solo:
                p.boss_damage = total_dmg
                p.total_damage = total_dmg
                p.encounter_dps = _dps(total_dmg, duration)
                p.active_dps = group_dps
            else:
                p.total_damage = 0
                p.encounter_dps = None
                p.active_dps = None      # -> excluded from Individual DPS parses/cache
        session.flush()
        return participants

    total_boss = sum(int(p.boss_damage or 0) for p in participants)
    total_pet = sum(int(p.pet_damage or 0) for p in participants)
    encounter.total_boss_damage = total_boss
    encounter.total_pet_damage = total_pet

    if encounter.duration_seconds is None and encounter.start_time and encounter.end_time:
        encounter.duration_seconds = Decimal(
            (encounter.end_time - encounter.start_time).total_seconds())

    combat_time = compute_combat_time(session, encounter)
    encounter.combat_time_seconds = combat_time

    duration = Decimal(encounter.duration_seconds or 0)
    encounter.encounter_dps = _dps(total_boss, duration)
    encounter.active_dps = _dps(total_boss, combat_time)

    for p in participants:
        p.total_damage = int(p.boss_damage or 0) + int(p.pet_damage or 0)
        p.encounter_dps = _dps(p.boss_damage, duration)
        # A participant's active window falls back to the encounter combat time
        # when no per-participant segments exist.
        p_segments = [s for s in session.query(DamageSegment)
                      .filter(DamageSegment.encounter_id == encounter.id,
                              DamageSegment.participant_id == p.id).all()]
        p_active = sum((Decimal(s.duration_seconds) for s in p_segments
                        if s.duration_seconds), Decimal(0))
        if p_active <= 0:
            p_active = combat_time
        p.active_dps = _dps(p.boss_damage, p_active)
    session.flush()
    return participants


# --------------------------------------------------------------------------- #
# Validation                                                                  #
# --------------------------------------------------------------------------- #
def validate_encounter(session, encounter):
    """Return the validation status for a finalized encounter.

    Public default: no auto-validation, so every encounter is valid. An extension
    may replace this function (same signature) with real checks that record
    ValidationFlag rows and return STATUS_INVALID / STATUS_FLAGGED / STATUS_VALID.
    """
    return STATUS_VALID


# --------------------------------------------------------------------------- #
# Scope helpers                                                               #
# --------------------------------------------------------------------------- #
def _scope_of(encounter):
    return dict(boss_id=encounter.boss_id, group_size=encounter.group_size,
                boss_version_id=encounter.boss_version_id,
                season_id=encounter.season_id, ruleset_id=encounter.ruleset_id)


def _valid_encounters_in_scope(session, scope):
    """Valid encounters in a PARSE scope (boss version included) -- used for the
    version-scoped percentile populations."""
    return (session.query(Encounter)
            .filter(Encounter.boss_id == scope['boss_id'],
                    Encounter.group_size == scope['group_size'],
                    Encounter.boss_version_id == scope['boss_version_id'],
                    Encounter.season_id == scope['season_id'],
                    Encounter.ruleset_id == scope['ruleset_id'],
                    Encounter.validation_status == STATUS_VALID)
            .all())


def _valid_encounters_in_display_scope(session, boss_id, group_size, season_id, ruleset_id):
    """Valid encounters in a DISPLAY scope (boss version NOT included) -- used for
    personal bests and ranking caches, which aggregate across boss versions while
    the parses they reference stay version-scoped."""
    return (session.query(Encounter)
            .filter(Encounter.boss_id == boss_id,
                    Encounter.group_size == group_size,
                    Encounter.season_id == season_id,
                    Encounter.ruleset_id == ruleset_id,
                    Encounter.validation_status == STATUS_VALID)
            .all())


# --------------------------------------------------------------------------- #
# Parse recomputation                                                         #
# --------------------------------------------------------------------------- #
def recompute_scope_parses(session, scope):
    """Recompute every parse (speed, group_dps, individual_dps) for a scope from
    the current set of valid encounters.  Percentiles are relative, so any new
    valid clear requires re-ranking the whole scope."""
    encounters = _valid_encounters_in_scope(session, scope)
    enc_ids = [e.id for e in encounters]

    # Null personal-best references to the parses we're about to delete (FK
    # safety); rebuild_personal_bests_for_scope repopulates them immediately
    # after.  Scoped by boss/group/season/ruleset (PB is version-agnostic).
    (session.query(PersonalBest)
     .filter(PersonalBest.boss_id == scope['boss_id'],
             PersonalBest.group_size == scope['group_size'],
             PersonalBest.season_id == scope['season_id'],
             PersonalBest.ruleset_id == scope['ruleset_id'])
     .update({PersonalBest.latest_parse_id: None}, synchronize_session=False))

    # Wipe existing parses for this scope; we rebuild them wholesale.
    if enc_ids:
        (session.query(Parse)
         .filter(Parse.encounter_id.in_(enc_ids))
         .delete(synchronize_session=False))

    if not encounters:
        session.flush()
        return

    common = dict(boss_id=scope['boss_id'], group_size=scope['group_size'],
                  boss_version_id=scope['boss_version_id'],
                  season_id=scope['season_id'], ruleset_id=scope['ruleset_id'])

    # --- Speed parse (per encounter, lower time is better) ---
    speed_pop = [Decimal(e.duration_seconds) for e in encounters]
    n_speed = len(speed_pop)
    for e in encounters:
        val = Decimal(e.duration_seconds)
        session.add(Parse(encounter_id=e.id, participant_id=None,
                          parse_type='speed', raw_metric=val,
                          percentile=percentile_rank(val, speed_pop, higher_is_better=False),
                          comparison_count=n_speed, tier=tier_for_count(n_speed), **common))

    # --- Group DPS parse (per encounter, higher combined active DPS is better) ---
    group_pop = [Decimal(e.active_dps or 0) for e in encounters]
    n_group = len(group_pop)
    for e in encounters:
        val = Decimal(e.active_dps or 0)
        session.add(Parse(encounter_id=e.id, participant_id=None,
                          parse_type='group_dps', raw_metric=val,
                          percentile=percentile_rank(val, group_pop, higher_is_better=True),
                          comparison_count=n_group, tier=tier_for_count(n_group), **common))

    # --- Individual DPS parse (per valid participant, higher active DPS better) ---
    parts = (session.query(Participant)
             .filter(Participant.encounter_id.in_(enc_ids),
                     Participant.is_valid.is_(True),
                     Participant.active_dps > 0)   # skip NPC-total-only AND 0-damage (carried) members
             .all())
    ind_pop = [Decimal(p.active_dps or 0) for p in parts]
    n_ind = len(ind_pop)
    for p in parts:
        val = Decimal(p.active_dps or 0)
        session.add(Parse(encounter_id=p.encounter_id, participant_id=p.id,
                          parse_type='individual_dps', raw_metric=val,
                          percentile=percentile_rank(val, ind_pop, higher_is_better=True),
                          comparison_count=n_ind, tier=tier_for_count(n_ind), **common))
    session.flush()


# --------------------------------------------------------------------------- #
# Ranking cache                                                               #
# --------------------------------------------------------------------------- #
def _participants_by_encounter(session, enc_ids):
    out = {}
    if not enc_ids:
        return out
    for p in (session.query(Participant)
              .filter(Participant.encounter_id.in_(enc_ids)).all()):
        out.setdefault(p.encounter_id, []).append(p)
    return out


def _members_payload(participants):
    return [{'character_id': p.character_id, 'character_name': p.character_name,
             'class_id': p.class_id, 'class_name': p.class_name,
             'gear_score': p.gear_score, 'boss_damage': int(p.boss_damage or 0),
             'active_dps': int(p.active_dps or 0), 'deaths': p.deaths}
            for p in participants]


def rebuild_ranking_cache(session, boss_id, group_size, season_id, ruleset_id):
    """Rebuild the four ranking-cache categories for a boss + group size.

    Note the cache is scoped by boss/group/season/ruleset but aggregates across
    boss *versions* for display (each cached row still records the encounter, so
    version is recoverable); parses themselves remain version-scoped."""
    # Clear existing cache rows for this slice.
    (session.query(RankingCache)
     .filter(RankingCache.boss_id == boss_id,
             RankingCache.group_size == group_size,
             RankingCache.season_id == season_id,
             RankingCache.ruleset_id == ruleset_id)
     .delete(synchronize_session=False))

    encounters = (session.query(Encounter)
                  .filter(Encounter.boss_id == boss_id,
                          Encounter.group_size == group_size,
                          Encounter.season_id == season_id,
                          Encounter.ruleset_id == ruleset_id,
                          Encounter.validation_status == STATUS_VALID)
                  .all())
    if not encounters:
        session.flush()
        return

    enc_ids = [e.id for e in encounters]
    parts_by_enc = _participants_by_encounter(session, enc_ids)

    # Index parses for quick lookup.
    parses = session.query(Parse).filter(Parse.encounter_id.in_(enc_ids)).all()
    speed_by_enc, group_by_enc = {}, {}
    ind_by_part = {}
    for pr in parses:
        if pr.parse_type == 'speed':
            speed_by_enc[pr.encounter_id] = pr
        elif pr.parse_type == 'group_dps':
            group_by_enc[pr.encounter_id] = pr
        elif pr.parse_type == 'individual_dps':
            ind_by_part[pr.participant_id] = pr

    common = dict(boss_id=boss_id, group_size=group_size,
                  season_id=season_id, ruleset_id=ruleset_id)

    def _speed_pct(e):
        p = speed_by_enc.get(e.id)
        return float(p.percentile) if p else 0.0

    def _group_pct(e):
        p = group_by_enc.get(e.id)
        return float(p.percentile) if p else 0.0

    # --- 1. Speed rankings (fastest first; ONE row per group, at their best time:
    #        a character/group never holds multiple leaderboard spots) ---
    seen_speed, rank = set(), 0
    for e in sorted(encounters, key=lambda x: Decimal(x.duration_seconds)):
        members = parts_by_enc.get(e.id, [])
        ident = frozenset(m.character_id for m in members)
        if ident in seen_speed:
            continue
        seen_speed.add(ident)
        rank += 1
        session.add(RankingCache(category=CAT_SPEED, rank_position=rank,
                                 encounter_id=e.id, participant_id=None,
                                 metric_value=Decimal(e.duration_seconds),
                                 payload={'duration_seconds': float(e.duration_seconds),
                                          'active_dps': int(e.active_dps or 0),
                                          'speed_parse': _speed_pct(e),
                                          'group_parse': _group_pct(e),
                                          'gear_score': max((m.gear_score or 0 for m in members), default=0),
                                          'deaths': e.deaths,
                                          'end_time': e.end_time.isoformat() if e.end_time else None,
                                          'members': _members_payload(members)}, **common))

    # --- 2. Individual DPS rankings (highest active DPS first) ---
    all_parts = [p for pid_list in parts_by_enc.values() for p in pid_list
                 if p.is_valid and p.active_dps is not None]   # skip NPC-total-only participants
    seen_ind, rank = set(), 0
    for p in sorted(all_parts, key=lambda x: int(x.active_dps or 0), reverse=True):
        if p.character_id in seen_ind:          # one row per character, at their best DPS
            continue
        seen_ind.add(p.character_id)
        rank += 1
        e = next((en for en in encounters if en.id == p.encounter_id), None)
        members = parts_by_enc.get(p.encounter_id, [])
        pr = ind_by_part.get(p.id)
        session.add(RankingCache(category=CAT_INDIVIDUAL_DPS, rank_position=rank,
                                 encounter_id=p.encounter_id, participant_id=p.id,
                                 metric_value=Decimal(int(p.active_dps or 0)),
                                 payload={'character_id': p.character_id,
                                          'character_name': p.character_name,
                                          'class_name': p.class_name,
                                          'boss_damage': int(p.boss_damage or 0),
                                          'encounter_dps': int(p.encounter_dps or 0),
                                          'active_dps': int(p.active_dps or 0),
                                          'dps_parse': float(pr.percentile) if pr else 0.0,
                                          'end_time': e.end_time.isoformat() if e and e.end_time else None,
                                          'members': _members_payload(members)}, **common))

    # --- 3. Group DPS rankings (highest combined active DPS first; ONE row per
    #        group -- same lineup collapses to its best, a new lineup is its own) ---
    seen_group, rank = set(), 0
    for e in sorted(encounters, key=lambda x: int(x.active_dps or 0), reverse=True):
        members = parts_by_enc.get(e.id, [])
        ident = frozenset(m.character_id for m in members)
        if ident in seen_group:
            continue
        seen_group.add(ident)
        rank += 1
        session.add(RankingCache(category=CAT_GROUP_DPS, rank_position=rank,
                                 encounter_id=e.id, participant_id=None,
                                 metric_value=Decimal(int(e.active_dps or 0)),
                                 payload={'combined_damage': int(e.total_boss_damage or 0),
                                          'combined_dps': int(e.active_dps or 0),
                                          'group_parse': _group_pct(e),
                                          'duration_seconds': float(e.duration_seconds),
                                          'end_time': e.end_time.isoformat() if e.end_time else None,
                                          'members': _members_payload(members)}, **common))

    # --- 4. Recent valid kills (newest first) ---
    recent = sorted(encounters, key=lambda x: x.end_time or x.created_at, reverse=True)
    for rank, e in enumerate(recent, start=1):
        members = parts_by_enc.get(e.id, [])
        session.add(RankingCache(category=CAT_RECENT, rank_position=rank,
                                 encounter_id=e.id, participant_id=None,
                                 metric_value=Decimal(e.duration_seconds),
                                 payload={'end_time': e.end_time.isoformat() if e.end_time else None,
                                          'group_size': e.group_size,
                                          'duration_seconds': float(e.duration_seconds),
                                          'active_dps': int(e.active_dps or 0),
                                          'speed_parse': _speed_pct(e),
                                          'group_parse': _group_pct(e),
                                          'members': _members_payload(members)}, **common))
    session.flush()


# --------------------------------------------------------------------------- #
# Personal bests                                                              #
# --------------------------------------------------------------------------- #
def rebuild_personal_bests_for_scope(session, scope):
    """Recompute personal-best summary rows for every character with valid data
    in a display scope (boss + group size + season + ruleset, across boss
    versions).  Historical encounters are untouched; this is a rolling summary."""
    encounters = _valid_encounters_in_display_scope(
        session, scope['boss_id'], scope['group_size'],
        scope['season_id'], scope['ruleset_id'])
    enc_ids = [e.id for e in encounters]
    if not enc_ids:
        return
    enc_by_id = {e.id: e for e in encounters}

    parts = (session.query(Participant)
             .filter(Participant.encounter_id.in_(enc_ids),
                     Participant.is_valid.is_(True)).all())
    parses = session.query(Parse).filter(Parse.encounter_id.in_(enc_ids)).all()

    # Parse lookups.
    ind_by_part, group_by_enc, speed_by_enc = {}, {}, {}
    for pr in parses:
        if pr.parse_type == 'individual_dps':
            ind_by_part[pr.participant_id] = pr
        elif pr.parse_type == 'group_dps':
            group_by_enc[pr.encounter_id] = pr
        elif pr.parse_type == 'speed':
            speed_by_enc[pr.encounter_id] = pr

    # Aggregate per character.
    agg = {}
    for p in parts:
        e = enc_by_id[p.encounter_id]
        cid = p.character_id
        a = agg.setdefault(cid, {
            'character_name': p.character_name, 'total_valid_kills': 0,
            'best_dps_parse': None, 'best_speed_parse': None, 'best_group_parse': None,
            'best_raw_dps': 0, 'fastest_kill_seconds': None,
            'latest_parse_id': None, 'latest_parse_at': None})
        a['character_name'] = p.character_name
        a['total_valid_kills'] += 1
        a['best_raw_dps'] = max(a['best_raw_dps'], int(p.active_dps or 0))

        # A "passenger" is a member who dealt no boss damage on a kill that HAS
        # per-player damage (i.e. not an NPC-total-only kill). They earn NO parse
        # credit at all -- speed, group and individual all belong to whoever
        # actually did the damage; their bests stay null. (NPC-total kills can't
        # be split, so every member still shares the speed/group parse there.)
        passenger = (not (e.raw_payload or {}).get('total_only')) \
            and int(p.boss_damage or 0) <= 0

        if not passenger:
            # Only credit parses from scopes with enough kills to be meaningful
            # (>= provisional). A 1-kill scope scores 100 but is tagged
            # 'not_enough_data'; crediting it would stamp a phantom 100 on the card,
            # trophy, crown and rankings. The raw boss boards still show every kill
            # with its tier note -- the kill just doesn't become a competitive best.
            ind = ind_by_part.get(p.id)
            if ind and ind.comparison_count >= TIER_MIN_PROVISIONAL:
                a['best_dps_parse'] = _max_dec(a['best_dps_parse'], ind.percentile)
                if a['latest_parse_at'] is None or (ind.calculated_at and ind.calculated_at >= a['latest_parse_at']):
                    a['latest_parse_at'] = ind.calculated_at
                    a['latest_parse_id'] = ind.id
            grp = group_by_enc.get(e.id)
            if grp and grp.comparison_count >= TIER_MIN_PROVISIONAL:
                a['best_group_parse'] = _max_dec(a['best_group_parse'], grp.percentile)
            spd = speed_by_enc.get(e.id)
            if spd and spd.comparison_count >= TIER_MIN_PROVISIONAL:
                a['best_speed_parse'] = _max_dec(a['best_speed_parse'], spd.percentile)
        dur = Decimal(e.duration_seconds)
        a['fastest_kill_seconds'] = dur if a['fastest_kill_seconds'] is None \
            else min(a['fastest_kill_seconds'], dur)

    # Upsert PB rows.
    for cid, a in agg.items():
        pb = (session.query(PersonalBest)
              .filter(PersonalBest.character_id == cid,
                      PersonalBest.boss_id == scope['boss_id'],
                      PersonalBest.group_size == scope['group_size'],
                      PersonalBest.season_id == scope['season_id'],
                      PersonalBest.ruleset_id == scope['ruleset_id'])
              .first())
        if not pb:
            pb = PersonalBest(character_id=cid, boss_id=scope['boss_id'],
                              group_size=scope['group_size'],
                              season_id=scope['season_id'], ruleset_id=scope['ruleset_id'])
            session.add(pb)
        pb.character_name = a['character_name']
        pb.total_valid_kills = a['total_valid_kills']
        pb.best_dps_parse = a['best_dps_parse']
        pb.best_speed_parse = a['best_speed_parse']
        pb.best_group_parse = a['best_group_parse']
        pb.best_raw_dps = a['best_raw_dps']
        pb.fastest_kill_seconds = a['fastest_kill_seconds']
        pb.latest_parse_id = a['latest_parse_id']
        pb.latest_parse_at = a['latest_parse_at']
    session.flush()


def _max_dec(current, candidate):
    if candidate is None:
        return current
    if current is None:
        return Decimal(candidate)
    return max(Decimal(current), Decimal(candidate))


# --------------------------------------------------------------------------- #
# Finalization pipeline                                                       #
# --------------------------------------------------------------------------- #
def finalize_encounter(encounter_id, session=None, rebuild_scope=True):
    """Run the full finalize pipeline for one encounter:

        finalize -> validate -> calc DPS -> calc parses -> update personal bests
        -> update cached rankings

    Safe to re-run (idempotent): recomputes the whole scope each time.  Returns
    the encounter's resulting validation status.

    rebuild_scope=False stops after validation. A batch ingest (the timed sync)
    uses it and rebuilds each touched scope ONCE at the end: steps 4-6 rewrite
    every parse in the scope, so doing them per kill made one sync rewrite the
    same ~10k-encounter board once per new kill and hold the sync lock for many
    minutes.
    """
    from datetime import datetime, timezone
    own = session is None
    session = session or new_session()
    try:
        encounter = session.get(Encounter, encounter_id)
        if encounter is None:
            raise ValueError(f'Encounter {encounter_id} not found')

        # 1-3. Metrics then validation (validation reads the computed DPS).
        compute_encounter_metrics(session, encounter)
        encounter.validation_status = validate_encounter(session, encounter)
        encounter.finalized_at = datetime.now(timezone.utc)
        session.flush()

        # 4-6. Everything downstream is scope-wide (percentiles are relative).
        if not rebuild_scope:
            if own:
                session.commit()
            return encounter.validation_status
        scope = _scope_of(encounter)
        recompute_scope_parses(session, scope)
        rebuild_personal_bests_for_scope(session, scope)
        rebuild_ranking_cache(session, scope['boss_id'], scope['group_size'],
                              scope['season_id'], scope['ruleset_id'])

        if own:
            session.commit()
        return encounter.validation_status
    except Exception:
        if own:
            session.rollback()
        raise
    finally:
        if own:
            session.close()


def recalculate_scope(boss_id, group_size, season_id, ruleset_id, boss_version_id):
    """Staff action: recompute parses + rankings + personal bests for a whole
    scope without changing any encounter's validation status or raw data."""
    scope = dict(boss_id=boss_id, group_size=group_size, season_id=season_id,
                 ruleset_id=ruleset_id, boss_version_id=boss_version_id)
    with new_session() as session:
        recompute_scope_parses(session, scope)
        rebuild_personal_bests_for_scope(session, scope)
        rebuild_ranking_cache(session, boss_id, group_size, season_id, ruleset_id)
        session.commit()
