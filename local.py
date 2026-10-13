"""EQDB Logic File for Item Identify"""
from db import get_db
import configparser
import os
import random

from sqlalchemy import and_, create_engine, or_, text
from sqlalchemy.orm import Session
from sqlalchemy.ext.automap import automap_base


from logic import engine, Item, get_item_data, SpellsNew, LootDropEntries, NPCTypes, LootTableEntries, Zone, \
    TradeskillRecipeEntries
import utils

here = os.path.dirname(__file__)
site_config = configparser.RawConfigParser()
ini_path = os.path.join(here, 'configuration.ini')
site_config.read_file(open(ini_path))
local_database = os.environ.get('LOCAL_DATABASE', site_config.get('local_database', 'connection', fallback='sqlite:///local_db.db'))
# SQLite opens a file per connection, so it needs no pool. NullPool means a page that
# opens a second local session never waits on the first one coming back.
if local_database.startswith('sqlite'):
    from sqlalchemy.pool import NullPool
    local_engine = create_engine(local_database, poolclass=NullPool)
else:
    local_engine = create_engine(local_database)
LocalBase = automap_base()
LocalBase.prepare(autoload_with=local_engine)


IdentifiedItems = LocalBase.classes.identified_items
IDEntry = LocalBase.classes.id_entry
Contributor = LocalBase.classes.contributor
GearList = LocalBase.classes.gear_list
GearListEntry = LocalBase.classes.gear_list_entry
Restricts = LocalBase.classes.restricts
RestrictEntry = LocalBase.classes.restrict_entry
Weights = LocalBase.classes.weights
WeightEntry = LocalBase.classes.weight_entry


# --- Player dashboard: the Discord user's chosen "main" character ---------- #
def ensure_dashboard_tables():
    """Create the dashboard preference table if it doesn't exist yet. Raw SQL
    (not automap) so a fresh column/table needs no schema regeneration."""
    with local_engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS user_main ("
            "discord_id VARCHAR(64) PRIMARY KEY, character_id INTEGER NOT NULL)"))


ensure_dashboard_tables()


# --- Per-user site settings ---------------------------------------------------------------- #
ITEM_QUALITIES = ('Base', 'Enchanted', 'Legendary')


def ensure_pref_tables():
    with local_engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS user_pref ("
            "discord_id VARCHAR(64) PRIMARY KEY, item_quality VARCHAR(16) NOT NULL DEFAULT 'Base')"))


ensure_pref_tables()


def get_item_quality(discord_id):
    """Which item version (Base / Enchanted / Legendary) this user wants loot links to open."""
    if not discord_id:
        return 'Base'
    with local_engine.connect() as conn:
        row = conn.execute(text("SELECT item_quality FROM user_pref WHERE discord_id = :d"),
                           {'d': str(discord_id)}).first()
    return row[0] if row and row[0] in ITEM_QUALITIES else 'Base'


def set_item_quality(discord_id, quality):
    if quality not in ITEM_QUALITIES:
        return
    with local_engine.begin() as conn:
        conn.execute(text("DELETE FROM user_pref WHERE discord_id = :d"), {'d': str(discord_id)})
        conn.execute(text("INSERT INTO user_pref (discord_id, item_quality) VALUES (:d, :q)"),
                     {'d': str(discord_id), 'q': quality})


# --- Guild Portal: site-owned guild page settings -------------------------- #
# NOTE: the game DB (peq) is never written to. Rosters, ranks and MOTDs stay
# authoritative in-game; this table only holds what the site itself adds --
# heraldry, a recruitment pitch, and an audit trail of who claimed the page.
# Officer permission is NOT stored here: it is re-derived from guild_members on
# every request (see guild_portal.manageable_guilds), so a demotion in-game takes
# effect immediately and no stale grant can survive here.
def ensure_guild_tables():
    with local_engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS guild_profile ("
            "guild_id INTEGER PRIMARY KEY,"
            "banner TEXT,"              # JSON: six whitelisted enum values
            "blurb TEXT,"               # recruitment pitch (free text)
            "recruiting INTEGER DEFAULT 0,"
            "wanted TEXT,"              # comma-separated class names
            "claimed_by VARCHAR(64),"   # discord id of the first claimant
            "claimed_at VARCHAR(32),"
            "updated_by VARCHAR(64),"   # discord id of the last editor
            "updated_at VARCHAR(32))"))
        # Attribution columns, added after the table shipped. CREATE TABLE IF NOT
        # EXISTS won't backfill them, so add them explicitly when missing --
        # storing the CHARACTER NAME and rank title, which is what gets shown.
        have = {r[1] for r in conn.execute(text("PRAGMA table_info(guild_profile)")).all()}
        for col in ('updated_by_name VARCHAR(80)', 'updated_by_title VARCHAR(80)',
                    'claimed_by_name VARCHAR(80)',
                    # officer-supplied contact details (validated in guild_portal)
                    'discord VARCHAR(200)', 'website VARCHAR(200)',
                    'contact VARCHAR(80)', 'raid_times VARCHAR(120)'):
            if col.split()[0] not in have:
                conn.execute(text("ALTER TABLE guild_profile ADD COLUMN %s" % col))


ensure_guild_tables()


_GP_COLS = ('guild_id', 'banner', 'blurb', 'recruiting', 'wanted', 'claimed_by',
            'claimed_at', 'updated_by', 'updated_at', 'updated_by_name',
            'updated_by_title', 'claimed_by_name',
            'discord', 'website', 'contact', 'raid_times')


def get_guild_profile(guild_id):
    """Site-side settings for one guild, or {} if the page was never claimed."""
    with local_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT %s FROM guild_profile WHERE guild_id = :g" % ", ".join(_GP_COLS)),
            {'g': int(guild_id)}).first()
    return dict(zip(_GP_COLS, row)) if row else {}


def get_guild_profiles():
    """{guild_id: profile} for the directory, so it needs one query not N."""
    with local_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT guild_id, banner, blurb, recruiting, wanted FROM guild_profile")).all()
    return {r[0]: {'guild_id': r[0], 'banner': r[1], 'blurb': r[2],
                   'recruiting': r[3], 'wanted': r[4]} for r in rows}


def save_guild_profile(guild_id, discord_id, banner=None, blurb=None,
                       recruiting=None, wanted=None, editor=None, links=None):
    """Upsert one guild's page settings. Only the fields passed are written, so a
    partial form post can't blank the rest. The caller MUST have already checked
    guild_portal.can_manage().

    `editor` is the {'name','title'} from guild_portal.officer_identity() -- the
    CHARACTER who made the change. It is shown on the guild page, so an edit that
    shouldn't have happened carries the name of whoever made it.
    """
    import datetime
    now = datetime.datetime.utcnow().isoformat(timespec='seconds')
    cur = get_guild_profile(guild_id)
    editor = editor or {}
    links = links if links is not None else {}
    vals = {
        'g': int(guild_id),
        'banner': banner if banner is not None else cur.get('banner'),
        'blurb': blurb if blurb is not None else cur.get('blurb'),
        'recruiting': int(recruiting) if recruiting is not None else int(cur.get('recruiting') or 0),
        'wanted': wanted if wanted is not None else cur.get('wanted'),
        'ub': str(discord_id), 'ua': now,
        'un': editor.get('name'), 'ut': editor.get('title'),
    }
    for k in ('discord', 'website', 'contact', 'raid_times'):
        vals[k] = links[k] if k in links else cur.get(k)

    with local_engine.begin() as conn:
        if cur:
            conn.execute(text(
                "UPDATE guild_profile SET banner=:banner, blurb=:blurb, "
                "recruiting=:recruiting, wanted=:wanted, updated_by=:ub, updated_at=:ua, "
                "updated_by_name=:un, updated_by_title=:ut, discord=:discord, "
                "website=:website, contact=:contact, raid_times=:raid_times "
                "WHERE guild_id=:g"), vals)
        else:
            vals['cb'], vals['ca'] = str(discord_id), now
            vals['cn'] = editor.get('name')
            conn.execute(text(
                "INSERT INTO guild_profile (guild_id, banner, blurb, recruiting, wanted,"
                " claimed_by, claimed_at, updated_by, updated_at,"
                " updated_by_name, updated_by_title, claimed_by_name,"
                " discord, website, contact, raid_times) "
                "VALUES (:g, :banner, :blurb, :recruiting, :wanted, :cb, :ca, :ub, :ua,"
                " :un, :ut, :cn, :discord, :website, :contact, :raid_times)"), vals)


def get_main_id(discord_id):
    """The character_id this Discord user pinned as their main, or None."""
    with local_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT character_id FROM user_main WHERE discord_id = :d"),
            {'d': str(discord_id)}).first()
    return row[0] if row else None


def set_main_id(discord_id, character_id):
    """Pin a main character (portable upsert: delete-then-insert)."""
    with local_engine.begin() as conn:
        conn.execute(text("DELETE FROM user_main WHERE discord_id = :d"),
                     {'d': str(discord_id)})
        conn.execute(text(
            "INSERT INTO user_main (discord_id, character_id) VALUES (:d, :c)"),
            {'d': str(discord_id), 'c': int(character_id)})


def get_gear_lists(user):
    """NOTE: THIS FEATURE IS CURRENTLY DARK AS IT IS BEING EVALUATED WHETHER EQDB SHOULD PROVIDE THIS OR NOT"""
    return None
    if not user:
        return []

    with Session(bind=local_engine) as session:
        query = session.query(GearList).filter(GearList.uid == user.id)
        result = query.all()

    gear_lists = {}
    for entry in result:
        gear_lists.update({entry.name: entry.glid})
    return gear_lists


def get_gear_list(user, glid):
    """NOTE: THIS FEATURE IS CURRENTLY DARK AS IT IS BEING EVALUATED WHETHER EQDB SHOULD PROVIDE THIS OR NOT"""
    return None
    user_id = user.id
    with Session(bind=local_engine) as session:
        query = session.query(GearList).filter(GearList.glid == glid)
        result = query.first()

    if not result:
        return {}
    elif result.uid != user_id:
        return None

    ret_data = {'set_name': result.name}

    # Get all the items currently associated with this
    with Session(bind=local_engine) as session:
        query = session.query(GearListEntry).filter(GearListEntry.glid == glid)
        result = query.all()

    slot_data = {}
    items = {}
    if not result:
        return ret_data
    for entry in result:
        slot = entry.slot
        augslot = entry.augslot
        item_id = entry.item_id

        if augslot != 'None':
            slot_data.update({f'{slot}_{augslot}': item_id})
        else:
            slot_data.update({slot: item_id})
        if item_id in items:
            count = items[item_id]
        else:
            count = 1
        items.update({item_id: count})

    # Get all the item data
    item_ids = items.keys()
    item_list = []
    for entry in item_ids:
        item_list.append(Item.id == entry)
    item_params = or_(*item_list)

    with get_db(engine) as session:
        query = session.query(Item).filter(item_params)
        result = query.all()

    item_data = {}
    stat_change = {}
    spell_list = []
    haste = 0
    for entry in result:
        item_id = entry.id
        item_data.update({item_id: entry.Name})

        # Calculate the item stats
        for sub_entry in utils.get_stats_of_interest():
            if (sub_entry == 'clickeffect' or
                sub_entry == 'focuseffect' or
                sub_entry == 'proceffect' or
                sub_entry == 'worneffect' or
                sub_entry == 'bardeffect'):
                if getattr(entry, sub_entry) not in spell_list:
                    spell_list.append(getattr(entry, sub_entry))
            elif sub_entry == 'haste':
                if getattr(entry, 'haste') > haste:
                    haste = int(getattr(entry, 'haste'))
            else:
                multiplier = items[item_id]
                if sub_entry in stat_change:
                    amt = stat_change[sub_entry] + int(getattr(entry, sub_entry) * multiplier)
                else:
                    amt = int(getattr(entry, sub_entry) * multiplier)
                stat_change.update({sub_entry: amt})

    stat_change.update({'haste': haste})
    ret_data.update({'item_data': item_data,
                     'stats': stat_change,
                     'slot_data': slot_data})

    # Get the spell name and ID
    spell_id_list = []
    for entry in spell_list:
        spell_id_list.append(SpellsNew.id == entry)
    spell_params = or_(*spell_id_list)

    with get_db(engine) as session:
        query = session.query(SpellsNew.id, SpellsNew.name).filter(spell_params)
        result = query.all()

    spell_data = {}
    for entry in result:
        spell_data.update({entry[0]: entry[1]})

    ret_data.update({'spell_data': spell_data,
                     'stat_lookup': utils.get_stat_translations()})

    return ret_data


def update_gear_list(user, glid, item_id, slot, aug_slot):
    """NOTE: THIS FEATURE IS CURRENTLY DARK AS IT IS BEING EVALUATED WHETHER EQDB SHOULD PROVIDE THIS OR NOT"""
    return None
    user_id = user.id
    with Session(bind=local_engine) as session:
        query = session.query(GearList).filter(GearList.glid == glid)
        result = query.first()

    if result.uid != user_id:
        return None

    # Do a quick check on the item
    with get_db(engine) as session:
        query = session.query(Item.augtype).filter(Item.id == item_id)
        result = query.first()

    if result:
        if result[0] > 0 and aug_slot == 'None':
            return False

    # See if this slot / aug_slot combination is already filled.  If so, delete it.
    with Session(bind=local_engine) as session:
        query = session.query(GearListEntry).\
            filter(and_(GearListEntry.glid == glid, GearListEntry.slot == slot, GearListEntry.augslot == aug_slot))
        result = query.first()
        if result:
            session.delete(result)
            session.commit()

    # Add the new item
    with Session(bind=local_engine) as session:
        gear_entry = GearListEntry(
            glid=glid,
            slot=slot,
            augslot=aug_slot,
            item_id=item_id
        )
        session.add(gear_entry)
        session.commit()
        session.flush()

    return True


def create_gear_list(user, name, is_public):
    """NOTE: THIS FEATURE IS CURRENTLY DARK AS IT IS BEING EVALUATED WHETHER EQDB SHOULD PROVIDE THIS OR NOT"""
    return None
    user_id = user.id
    if is_public:
        private = False
    else:
        private = True
    with Session(bind=local_engine) as session:
        gear_list = GearList(
            uid=user_id,
            name=name,
            private=private
        )
        session.add(gear_list)
        session.commit()
        session.flush()
        session.refresh(gear_list)
    return gear_list.glid


def delete_weights_set(user, wid):
    user_id = user.id
    with Session(bind=local_engine) as session:
        query = session.query(Weights).filter(Weights.wid == wid)
        result = query.one()

    if result.uid != user_id:
        return False

    with Session(bind=local_engine) as session:
        entries = session.query(WeightEntry).filter(WeightEntry.wid == wid)
        for entry in entries:
            session.delete(entry)

        session.delete(result)
        session.commit()

    return True


def get_weights_sets(user):
    if not user:
        return []
    user_id = user.id
    with Session(bind=local_engine) as session:
        query = session.query(Weights).filter(Weights.uid == user_id)
        result = query.all()

    ret_data = {}
    for entry in result:
        with Session(bind=local_engine) as session:
            query = session.query(WeightEntry).filter(WeightEntry.wid == entry.wid)
            sub_result = query.all()

        stats = {}
        for sub_entry in sub_result:
            stat_name = utils.get_stat_name(sub_entry.stat)
            stats.update({stat_name: {'value': sub_entry.value,
                                      'stat': sub_entry.stat}})
        ret_data.update({entry.name: stats})
    return ret_data


def update_weights_set(wid, filters, user):
    user_id = user.id
    with Session(bind=local_engine) as session:
        query = session.query(Weights).filter(Weights.wid == wid)
        result = query.one()

    if result.uid != user_id:
        return None

    # Remove all the current stat weights
    with Session(bind=local_engine) as session:
        entries = session.query(WeightEntry).filter(WeightEntry.wid == wid)
        for entry in entries:
            session.delete(entry)
        session.commit()

    # Insert the stat restrictions
    with Session(bind=local_engine) as session:
        for entry in filters:
            weight_entry = WeightEntry(
                wid=wid,
                stat=entry,
                value=filters[entry]
            )
            session.add(weight_entry)
        session.commit()
        session.flush()

    return True


def get_weight_set(wid, user):
    user_id = user.id
    with Session(bind=local_engine) as session:
        query = session.query(Weights).filter(Weights.wid == wid)
        result = query.one()

    if result.uid != user_id:
        return None

    name = result.name
    with Session(bind=local_engine) as session:
        query = session.query(WeightEntry).filter(WeightEntry.wid == wid)
        result = query.all()

    stats = {}
    for entry in result:
        stat_name = utils.get_stat_name(entry.stat)
        stats.update({stat_name: {'value': entry.value,
                                  'stat': entry.stat}})

    data = {'name': name, 'stats': stats}
    return data


def add_weights_set(user, name, filters):
    user_id = user.id

    # Insert the new weight set
    with Session(bind=local_engine) as session:
        weights_set = Weights(
            uid=user_id,
            name=name,
        )
        session.add(weights_set)
        session.commit()
        session.flush()
        session.refresh(weights_set)
        set_id = weights_set.wid

    # Insert the stat weights
    with Session(bind=local_engine) as session:
        for entry in filters:
            weight_entry = WeightEntry(
                wid=set_id,
                stat=entry,
                value=filters[entry]
            )
            session.add(weight_entry)
        session.commit()
        session.flush()

    # Return the set id
    return set_id


def delete_restrict_set(user, rid):
    user_id = user.id
    with Session(bind=local_engine) as session:
        query = session.query(Restricts).filter(Restricts.rid == rid)
        result = query.one()

    if result.uid != user_id:
        return False

    with Session(bind=local_engine) as session:
        entries = session.query(RestrictEntry).filter(RestrictEntry.rid == rid)
        for entry in entries:
            session.delete(entry)

        session.delete(result)
        session.commit()

    return True


def get_restrict_sets(user):
    if not user:
        return []
    user_id = user.id
    with Session(bind=local_engine) as session:
        query = session.query(Restricts).filter(Restricts.uid == user_id)
        result = query.all()

    ret_data = {}
    for entry in result:
        with Session(bind=local_engine) as session:
            query = session.query(RestrictEntry).filter(RestrictEntry.rid == entry.rid)
            sub_result = query.all()

        stats = {}
        for sub_entry in sub_result:
            stat_name = utils.get_stat_name(sub_entry.stat)
            stats.update({stat_name: {'value': sub_entry.value,
                                      'stat': sub_entry.stat}})
        ret_data.update({entry.name: stats})
    return ret_data


def update_restrict_set(rid, filters, user):
    user_id = user.id
    with Session(bind=local_engine) as session:
        query = session.query(Restricts).filter(Restricts.rid == rid)
        result = query.one()

    if result.uid != user_id:
        return None

    # Remove all the current stat filters
    with Session(bind=local_engine) as session:
        entries = session.query(RestrictEntry).filter(RestrictEntry.rid == rid)
        for entry in entries:
            session.delete(entry)
        session.commit()

    # Insert the stat restrictions
    with Session(bind=local_engine) as session:
        for entry in filters:
            restrict_entry = RestrictEntry(
                rid=rid,
                stat=entry,
                value=filters[entry]
            )
            session.add(restrict_entry)
        session.commit()
        session.flush()

    return True


def get_restrict_set(rid, user):
    user_id = user.id
    with Session(bind=local_engine) as session:
        query = session.query(Restricts).filter(Restricts.rid == rid)
        result = query.one()

    if result.uid != user_id:
        return None

    name = result.name
    with Session(bind=local_engine) as session:
        query = session.query(RestrictEntry).filter(RestrictEntry.rid == rid)
        result = query.all()

    stats = {}
    for entry in result:
        stat_name = utils.get_stat_name(entry.stat)
        stats.update({stat_name: {'value': entry.value,
                                  'stat': entry.stat}})

    data = {'name': name, 'stats': stats}
    return data


def add_restrict_set(user, name, filters):
    user_id = user.id

    # Insert the new restrict set
    with Session(bind=local_engine) as session:
        restrict_set = Restricts(
            uid=user_id,
            name=name,
        )
        session.add(restrict_set)
        session.commit()
        session.flush()
        session.refresh(restrict_set)
        set_id = restrict_set.rid

    # Insert the stat restrictions
    with Session(bind=local_engine) as session:
        for entry in filters:
            restrict_entry = RestrictEntry(
                rid=set_id,
                stat=entry,
                value=filters[entry]
            )
            session.add(restrict_entry)
        session.commit()
        session.flush()

    # Return the set id
    return set_id


def get_user_data(user):
    user_id = user.id
    ret_data = {}
    with Session(bind=local_engine) as session:
        query = session.query(GearList).filter(GearList.uid == user_id)
        result = query.all()
        ret_data.update({'gearlists': result})

        query = session.query(Restricts).filter(Restricts.uid == user_id)
        result = query.all()
        ret_data.update({'restricts': result})

        query = session.query(Weights).filter(Weights.uid == user_id)
        result = query.all()
        ret_data.update({'weights': result})

    return ret_data


def add_item_identification(data, user=None):
    """Adds an item identification to the database, and does the necessary followup."""
    item_id = data.get('item_id')
    expansion = data.get('expansion')
    source = data.get('source')
    zone_id = data.get('zone_id')

    if user:
        user_id = user.id
        user_name = user.name
    else:
        user_id = -1
        user_name = 'Anonymous'

    # Does this user already exist in the local db
    with Session(bind=local_engine) as session:
        query = session.query(Contributor).filter(Contributor.id == user_id)
        result = query.all()

    if not result:
        # Add the contributor
        with Session(bind=local_engine) as session:
            new_contrib = Contributor(name=user_name,
                                      id=user_id,
                                      contributed=1)
            session.add(new_contrib)
            session.commit()
            contrib = {'name': user_name, 'id': user_id, 'contributed': 1}
    else:
        contrib = result[0].__dict__

    # Does this item have existing identifications
    with Session(bind=local_engine) as session:
        args = [IDEntry.expansion, IDEntry.source, IDEntry.item_id, Contributor.contributed, Contributor.id]
        query = session.query(*args).filter(IDEntry.item_id == item_id).filter(IDEntry.cid == Contributor.id)
        result = query.all()

    existing_entries = result

    # Create the data matrix
    idents = [{'expansion': expansion, 'source': source, 'zone': zone_id}]
    ident_amt = [contrib.get('contributed')]
    for entry in result:
        entry = entry._mapping
        test_key = {'expansion': entry.get('expansion'), 'source': entry.get('source'), 'zone': entry.get('zone_id')}
        if test_key in idents:
            idx = idents.index(test_key)
            new_val = ident_amt[idx] + entry.get('contributed')
            ident_amt[idx] = new_val
        else:
            idents.append(test_key)
            ident_amt.append(entry.get('contributed'))

    # Check if we now have an identification:
    identification = None
    for entry in ident_amt:
        if entry >= 100:
            idx = ident_amt.index(entry)
            identification = idents[idx]
            break

    if identification is not None:
        # Update all the contributors contributions
        with Session(bind=local_engine) as session:
            for entry in existing_entries:
                if entry.id == -1:
                    continue
                obj = session.query(Contributor).filter(Contributor.id == entry.get('id'))
                obj.update({'contributed': entry.get('contributed') + 1})
                session.commit()
            if user_id != -1:
                obj = session.query(Contributor).filter(Contributor.id == user_id)
                obj.update({'contributed': contrib.get('contributed') + 1})
                session.commit()
                contrib['contributed'] += 1

        # Add this item to identified items.
        with Session(bind=local_engine) as session:
            new_item = IdentifiedItems(item_id=item_id,
                                       expansion=identification.get('expansion'),
                                       source=identification.get('source'),
                                       zone_id=identification.get('zone_id'))
            session.add(new_item)
            session.commit()

        # Remove the item from existing identifications
        with Session(bind=local_engine) as session:
            for entry in existing_entries:
                obj = IDEntry.query.filter_by(item_id == entry.get('item_id'))
                session.delete(obj)
                session.commit()

        # Report back
        ret_dict = {'item_id': item_id,
                    'result': True,
                    'expansion': expansion,
                    'source': source}
    else:
        # Add this to Idents
        with Session(bind=local_engine) as session:
            new_entry = IDEntry(item_id=item_id,
                                cid=contrib.get('id'),
                                expansion=expansion,
                                source=source,
                                zone_id=zone_id)
            session.add(new_entry)
            session.commit()
        ret_dict = {'item_id': item_id,
                    'result': False,
                    'expansion': None,
                    'source': None}
    if user:
        # Get the number of identifications the user has submitted
        with Session(bind=local_engine) as session:
            query = session.query(IDEntry.item_id).filter(IDEntry.cid == user.id)
            result = query.all()

        ret_dict.update({'user_contrib': contrib.get('contributed'),
                         'user_identify': len(result)})
    return ret_dict


def get_unidentified_item(user=None):
    """Returns an unidentified item."""
    if user:
        pick_new = False
        chooser = random.randint(1, 10)
        if chooser == 10:
            pick_new = True
        if not pick_new:
            # Get a partially identified item that the user hasn't weighed in on
            with Session(bind=local_engine) as session:
                query = session.query(IDEntry.item_id).filter(IDEntry.cid == user.id)
                result = query.all()
            exclude_ids = []
            for entry in result:
                exclude_ids.append(IDEntry.item_id != entry[0])
            exclude_params = and_(*exclude_ids)

            with Session(bind=local_engine) as session:
                query = session.query(IDEntry.item_id).filter(exclude_params)
                result = query.all()
            if result:
                choice = random.choice(result)
                return get_item_data(choice[0])

    # Get all the identified items IDs from the local db.
    with Session(bind=local_engine) as session:
        query = session.query(IdentifiedItems.item_id)
        result = query.all()
    ided_items = [item for t in result for item in t]

    # Anything from 1000 to 1000000 is a potentially valid item ID.
    pos_items = list(range(1000, 1000000))

    unid = list(set(pos_items) - set(ided_items))

    while True:
        check_id = random.choice(unid)
        # See if this item ID exists on NMS
        with get_db(engine) as session:
            query = session.query(Item.id).filter(Item.id == check_id)
            result = query.all()
        if not result:
            continue
        # We have a valid ID!  Return it!
        return get_item_data(result[0][0])


def get_leaderboard():
    """Gets the data identification leaderboard"""
    # Get all contributors
    with Session(bind=local_engine) as session:
        query = session.query(Contributor)
        result = query.all()

    contributors = []
    for entry in result:
        entry = entry.__dict__
        contributor = {'contributor': entry['name'], 'contributed': entry['contributed']}
        with Session(bind=local_engine) as session:
            query = session.query(IDEntry).filter(IDEntry.cid == entry['id'])
            result = query.all()
        contributor.update({'identifications': len(result)})
        contributors.append(contributor)

    return contributors
