"""Expansion gates + Plane of Time flag progression -- the whole road, from
Nagafen to Zebuxoruk.

Two separate flag systems feed this page. Both are owned per ACCOUNT, but they
are stored differently, so each has its own reader:

  1. EXPANSION GATES -- see ERA_GATES below. 16 boss kills open Kunark,
     Velious, Luclin and the Planes of Power.
  2. TIME FLAGS -- the 42-step ladder Seer Mal Nae'Shi reads, below.

DATA MODEL (verified against the game DB)
    data_buckets.key = 'pop', owned by ACCOUNT (the account_id column; character_id
    is 0 on these rows). The value is JSON:

        {"flags": {"hedge":"1","construct":"1","askr":"4", ...}, "aa": {...}}

    Because the bucket hangs off the account, the Time flag is shared by every
    character on it -- the page says so. (Contrast 'GestaltClasses', keyed on the
    character_id COLUMN, and zone_flags(charID, zoneID), keyed per character.)

WHAT IS TRACKABLE
    Only the 42 flags below, because only they exist in the bucket. Everything
    else in a walkthrough -- Gryme's Crypt Key, the twelve Adan kings, the Torden
    gems, the Rathe Council -- sets no flag, so it lives in a step's `desc` as
    *how* you earn the flag, never as its own checkbox.

    'story'      -- Gram Dunnar's [stories] counter (1-7), not a progression step.
    'aidgrimel'  -- optional Aid Grimel step, sits at "0"; excluded from the 42.

    A handful of "You receive a character flag!" lines (zoning into Temple of Marr
    or the Halls of Honor) have NO 'pop' key -- those are zone_flags rows, so they
    are deliberately not steps here.
"""
from db import get_db
import json

import logic
from sqlalchemy import text
from sqlalchemy.orm import Session

# The six Tribunal trials: the Tribunal flags you once, for whichever you did.
# "After completeing any of the trial return and tell The Tribunal..." -- so this
# whole group counts as ONE requirement, not six.
TRIALS = [
    ('execution', 'Trial of Execution'), ('flame', 'Trial of Flame'),
    ('hanging', 'Trial of Hanging'), ('lashing', 'Trial of Lashing'),
    ('stoning', 'Trial of Stoning'), ('torture', 'Trial of Torture'),
]

# Keys that exist in the bucket but are not progression steps.
NON_STEP_KEYS = {'story', 'aidgrimel'}

# ---------------------------------------------------------------------------
# EXPANSION GATES -- the 16 kills that open Kunark, Velious, Luclin and PoP.
#
# DATA MODEL (verified against the game DB)
#   progression_flags   7 eras. We use 1-4 only: this server stops at PoP, so
#                       stage 17 ('saryrn' -> GoD) is deliberately excluded.
#   progression_stages  the gate bosses. NOTE stage.flag_id is the era the kill
#                       OPENS, not the era the boss lives in -- Nagafen and Vox
#                       are Classic mobs carrying flag_id 1 (RoK), and Dozekar
#                       sits in the Velious Temple of Veeshan under flag_id 3
#                       (SoL). That is why the gate is named for its door.
#   progression_atlas   zone_id -> flag_id, i.e. how many zones each gate opens.
#
#   Per-player state is data_buckets.key = '{account_id}-progress-flag-{era}'.
#   The id lives in the KEY -- BOTH id columns are 0 on these rows -- so this
#   cannot reuse _read_bucket(). The value is a flat list, not JSON:
#       'lord nagafen=1;lady vox=1'
#   Boss names match progression_stages.name verbatim, lower-case.
#
#   npc ids drive the "where is it" link, so the row is chosen to make the NPC
#   page useful -- it draws a map pin from spawn2 and a loot list from the
#   loottable. Selection rule, in order:
#       1. era-correct zone   (the Kunark wyrms were re-used in Field of Scale,
#                              zone 452, for a later expansion -- never those)
#       2. has a spawn point  (many '#'-prefixed raid rows are script-spawned
#                              with spawn2=0, so they render no pin)
#       3. otherwise the row players actually kill, per the kill events
#   Only Emperor Ssraeshza has no spawnable row at all (both candidates are
#   script-spawned), so he falls through to rule 3: 162227 is the row in the
#   kill log and the one carrying the 40-item loot table.
# ---------------------------------------------------------------------------
ERA_GATES = [
    {'n': 1, 'code': 'RoK', 'mark': 'K', 'era': 'Ruins of Kunark',
     'title': 'The Elder Dragons', 'zones': 27,
     'blurb': "Norrath's two great wyrms still bar the road east.",
     'opens': "Firiona Vie &middot; The Dreadlands &middot; The Burning Wood &middot; The Ruins of Sebilis "
              "&middot; Chardok &middot; The Howling Stones &middot; Veeshan's Peak",
     'bosses': [
       {'key': 'lord nagafen', 'name': 'Lord Nagafen', 'npc': 32040,
        'zone': "Nagafen's Lair", 'short': 'soldungb', 'zid': 32, 'level': 55},
       {'key': 'lady vox', 'name': 'Lady Vox', 'npc': 73057,
        'zone': 'The Permafrost Caverns', 'short': 'permafrost', 'zid': 73, 'level': 55},
     ]},

    {'n': 2, 'code': 'SoV', 'mark': 'V', 'era': 'Scars of Velious',
     'title': 'The Kunark Wyrms', 'zones': 19,
     'blurb': 'Four dragons scattered the length of Kunark hold the road north.',
     'opens': 'The Iceclad Ocean &middot; Kael Drakkel &middot; Skyshrine &middot; The Western Wastes '
              "&middot; The Temple of Veeshan &middot; The Sleeper's Tomb &middot; The Plane of Growth",
     'bosses': [
       {'key': 'trakanon', 'name': 'Trakanon', 'npc': 89154,
        'zone': 'The Ruins of Sebilis', 'short': 'sebilis', 'zid': 89, 'level': 65},
       {'key': 'gorenaire', 'name': 'Gorenaire', 'npc': 86014,
        'zone': 'The Dreadlands', 'short': 'dreadlands', 'zid': 86, 'level': 60},
       {'key': 'severilous', 'name': 'Severilous', 'npc': 94009,
        'zone': 'The Emerald Jungle', 'short': 'emeraldjungle', 'zid': 94, 'level': 60},
       {'key': 'talendor', 'name': 'Talendor', 'npc': 91093,
        'zone': 'The Skyfire Mountains', 'short': 'skyfire', 'zid': 91, 'level': 60},
     ]},

    {'n': 3, 'code': 'SoL', 'mark': 'L', 'era': 'Shadows of Luclin',
     'title': 'The Velious Wyrms', 'zones': 21,
     'blurb': 'Five more dragons across Velious &mdash; the last of them deep inside the Temple of Veeshan.',
     'opens': 'Shadow Haven &middot; The Nexus &middot; Ssraeshza Temple &middot; Vex Thal '
              "&middot; Grieg's End &middot; The Umbral Plains &middot; The Akheva Ruins",
     'bosses': [
       {'key': 'klandicar', 'name': 'Klandicar', 'npc': 120084,
        'zone': 'The Western Wastes', 'short': 'westwastes', 'zid': 120, 'level': 70},
       {'key': 'zlandicar', 'name': 'Zlandicar', 'npc': 123115,
        'zone': 'Dragon Necropolis', 'short': 'necropolis', 'zid': 123, 'level': 70},
       {'key': 'wuoshi', 'name': 'Wuoshi', 'npc': 119112,
        'zone': 'The Wakening Land', 'short': 'wakening', 'zid': 119, 'level': 64},
       {'key': 'dozekar the cursed', 'name': 'Dozekar the Cursed', 'npc': 124037,
        'zone': 'The Temple of Veeshan', 'short': 'templeveeshan', 'zid': 124, 'level': 66},
       {'key': 'kelorek`dar', 'name': 'Kelorek`Dar', 'npc': 117073,
        'zone': 'Cobaltscar', 'short': 'cobaltscar', 'zid': 117, 'level': 65},
     ]},

    {'n': 4, 'code': 'PoP', 'mark': 'P', 'era': 'Planes of Power',
     'title': 'The Lords of Luclin', 'zones': 24,
     'blurb': 'The last door. Five lords of Luclin stand between you and the Planes.',
     'opens': 'The Plane of Knowledge &middot; The Plane of Justice &middot; The Plane of Innovation '
              '&middot; Bastion of Thunder &middot; The Halls of Honor &middot; Drunder '
              '&middot; The Plane of Time',
     'bosses': [
       {'key': 'thought horror overfiend', 'name': 'Thought Horror Overfiend', 'npc': 164078,
        'zone': 'The Deep', 'short': 'thedeep', 'zid': 164, 'level': 63},
       {'key': 'the insanity crawler', 'name': 'The Insanity Crawler', 'npc': 179180,
        'zone': 'The Akheva Ruins', 'short': 'akheva', 'zid': 179, 'level': 63},
       {'key': 'grieg veneficus', 'name': 'Grieg Veneficus', 'npc': 163057,
        'zone': "Grieg's End", 'short': 'griegsend', 'zid': 163, 'level': 65},
       {'key': 'xerkizh the creator', 'name': 'Xerkizh the Creator', 'npc': 162190,
        'zone': 'Ssraeshza Temple', 'short': 'ssratemple', 'zid': 162, 'level': 66},
       {'key': 'emperor ssraeshza', 'name': 'Emperor Ssraeshza', 'npc': 162227,
        'zone': 'Ssraeshza Temple', 'short': 'ssratemple', 'zid': 162, 'level': 66},
     ]},
]

GATE_TOTAL = sum(len(g['bosses']) for g in ERA_GATES)   # 16

TIERS = [
 {'n': 1, 'roman': 'I', 'name': 'The First Planes',
  'blurb': 'Disease, Justice, Innovation and Nightmares — the four gates that open the climb.',
  'groups': [
    {'name': 'Plane of Disease', 'zone': 'podisease', 'steps': [
      {'key': 'adler', 'name': 'Adler Fuirstel',
       'desc': 'Hail him in the Plane of Tranquility, near the Plane of Disease portal (the faucet).'},
      {'key': 'grummus', 'name': 'Grummus',
       'desc': "Kill <b>Gryme the Crypt Guardian</b> atop the castle for <em>Gryme's Crypt Key</em>, open the "
               'castle door, kill <b>Aramin the Spider Guardian</b>, then clear your way to Grummus.'},
      {'key': 'elder', 'name': 'Elder Fuirstel',
       'desc': 'Zone into the pit to reach <b>Ruins of Lxanvom</b>, return to Adler Fuirstel, then hail Elder '
               'Fuirstel in the Sick Bay <em>(-286, -1407, -906)</em>.'},
    ]},
    {'name': 'Plane of Justice', 'zone': 'pojustice', 'trials': True,
     'note': 'Post-flag: hail <b>Gram Dunnar</b> in the Plane of Knowledge — this opens '
             '<b>Plane of Storms</b> and <b>Plane of Valor</b>.',
     'steps': [
      {'key': 'mavuin', 'name': 'Mavuin',
       'desc': 'Locate him in the basement of the Plane of Justice and hail him.'},
      {'key': 'tribunal', 'name': "Plead Mavuin's case",
       'desc': 'After your trial, tell The Tribunal <em>"I have come to plead the case of Mavuin."</em> '
               'then return and hail Mavuin.'},
    ]},
    {'name': 'Plane of Innovation', 'zone': 'poinnovation', 'steps': [
      {'key': 'xanamech', 'name': 'Xanamech Nezmirthafen',
       'desc': 'Loot <em>Copper Node</em>, <em>Bundle of Super Conductive Wires</em> and <em>Intact Power '
               'Cell</em>, give all three to <b>Nitram Anizok</b> and escort him. Stand <b>behind</b> Xanamech '
               "before he activates so his cone doesn't kill Nitram — then hail Nitram."},
      {'key': 'behemoth', 'name': 'Manaetic Behemoth', 'stages': 2,
       'desc': 'Hail <b>Giwin Mirakon</b> inside the factory, then kill clockwork devices for ~5 minutes until '
               'the Behemoth activates. Kill it and hail Giwin again.'},
    ]},
    {'name': 'Plane of Nightmares', 'zone': 'ponightmare', 'steps': [
      {'key': 'hedge', 'name': 'Adroha Jezith',
       'desc': 'Hail her in the Sick Bay <em>(-253, -1415, -906)</em> for the Hedge preflag.'},
      {'key': 'construct', 'name': 'A Construct of Nightmares',
       'desc': 'Far northeast across the water, tell <b>Thelin Poxbourne</b> <em>ready</em> to enter the hedge '
               'maze. Kill the Construct, loot the <em>Dagger Blade Shard</em> and hail Thelin.'},
      {'key': 'terris', 'name': 'Terris Thule',
       'desc': 'Click the gate to her lair, fight to the building at the end of the path, and kill her.'},
      {'key': 'poxbourne', 'name': 'Elder Poxbourne',
       'desc': 'Return to the Sick Bay <em>(-253, -1415, -906)</em> and hail him.'},
    ]},
  ]},

 {'n': 2, 'roman': 'II', 'name': 'Decay, Torment, Storms &amp; Valor',
  'blurb': "The Carprin cycle, the Plaguebringer, Saryrn's tower, the storm giants and the prismatic dragon.",
  'groups': [
    {'name': 'Ruins of Lxanvom — Crypt of Decay', 'zone': 'codecay', 'steps': [
      {'key': 'codecay', 'name': 'The Carprin Cycle', 'stages': 2,
       'desc': "Pull Carprin Deatharn's three guards off him and kill them first, then Carprin. Work through "
               '<b>Avhi Escron</b>, <b>Bishop Toluwon</b>, <b>Raex Pwodill &amp; Vindor Mawnil</b>, then '
               "<b>High Priest Uttor Szanvon</b>. <em>Don't kill</em> Tarkil Adan — hail him."},
      {'key': 'bertox', 'name': 'Bertoxxulous the Plaguebringer',
       'desc': 'Click the chair at the zone entrance, kill the <b>Spectre of Corruption</b> to start (2-hour '
               'timer), clear all twelve <b>Adan</b> kings, then kill Bertox. Hail Elder Fuirstel afterwards.'},
    ]},
    {'name': 'Plane of Torment', 'zone': 'potorment', 'steps': [
      {'key': 'shadyglade', 'name': 'Fahlia Shadyglade',
       'desc': 'Hail her in the Sick Bay <em>(-292, -1362, -906)</em> for the preflag.'},
      {'key': 'newleaf', 'name': 'Tylis Newleaf',
       'desc': 'Build <em>A Screaming Sphere</em> from the four Orbs and the Mouths of Baraguj Szuul, reach '
               'Maareq the Prophet on the 5th floor, hail Tylis at the cages, then kill <b>The Keeper of '
               'Sorrows</b> and hail him again.'},
      {'key': 'saryrn', 'name': 'Saryrn', 'stages': 2,
       'desc': 'Kill Saryrn along with her pet <b>Sorrowsong</b> and adds, then return and hail Tylis Newleaf '
               'followed by Fahlia Shadyglade.'},
    ]},
    {'name': 'Plane of Storms', 'zone': 'postorms', 'steps': [
      {'key': 'askr', 'name': 'Askr the Lost', 'stages': 4,
       'desc': 'Loot the <em>Storm Giant Head</em>, <em>Volaas Beard</em>, <em>Taarid Bone</em> and <em>Satuur '
               'Sash</em> from the Hleyta, Jolna and Irmin camps, plus two <em>Esoteric Medallions</em> from '
               "different giants. Build <em>Askr's Sealed Bag of Verity</em> and the <em>Esoteric Meld</em>, "
               'and hand them to Askr.'},
    ]},
    {'name': 'Plane of Valor', 'zone': 'povalor', 'steps': [
      {'key': 'aerin', 'name': 'Aerin`Dar',
       'desc': 'Collect the three hearts and three <em>Pieces of the Crystalline Globe</em>, run the '
               '<b>Paralin Notion</b> → <b>Aaramis Darylin</b> → <b>Ryglot Cupperhide</b> chain for the '
               '<em>Crystalline Globe</em>, then port in and kill Aerin`Dar with his Mindless Minions and Rahlgon.'},
      {'key': 'valor', 'name': 'Passage to the Halls of Honor',
       'desc': 'Click the trap door after Aerin`Dar falls and follow the path to zone into the Halls of Honor.'},
    ], 'note': 'Post-flag: hail <b>Gram Dunnar</b> and select <em>stories</em>.'},
  ]},

 {'n': 3, 'roman': 'III', 'name': 'Thunder, Honor, Zek &amp; the Tower',
  'blurb': 'Torden and the Halls of Honor, the Temple of Marr, the warlords of Drunder, then the Burning Prince.',
  'groups': [
    {'name': 'Torden, Bastion of Thunder', 'zone': 'bothunder', 'steps': [
      {'key': 'agnarr', 'name': 'Agnarr, the Storm Lord',
       'desc': 'Build the <em>Enchanted Ring of Torden</em> from the four wing gems, clear the four courtyard '
               'towers for their Spheres to make the <em>Symbol of Torden</em>, then ride the Lightning Sphere '
               'up. Kill <b>Envyd Firestorm</b> and <b>Emmerick Skyfury</b> — hailing Askr the Lost between '
               'floors — then Agnarr.'},
      {'key': 'karana', 'name': 'Karana',
       'desc': 'He spawns in the room at the top of the stairs once Agnarr is dead. <b>Hail him</b> and follow '
               'the dialog.'},
    ]},
    {'name': 'Halls of Honor', 'zone': 'hohonora', 'steps': [
      {'key': 'faye', 'name': 'Trial of Trydan Faye',
       'desc': 'NE basement — hail Faye, kill the <b>Custodian of Marr</b>, then <b>Rydda`Dar</b>.'},
      {'key': 'trell', 'name': 'Trial of Rhaliq Trell',
       'desc': 'NW basement — protect the villagers in all three rooms from the named attackers, then hail Trell.'},
      {'key': 'garn', 'name': 'Trial of Alekson Garn',
       'desc': 'SE basement — protect the norrathian maidens from the crazed norrathians. Four mobs then a '
               "harder one per room; <b>don't let any maiden die</b>. Hail the Planar Projection."},
    ]},
    {'name': 'Temple of Marr', 'zone': 'hohonorb', 'steps': [
      {'key': 'marr', 'name': 'Lord Mithaniel Marr',
       'desc': 'Kill <b>Ralthazor, Champion of Marr</b> to make Lord Marr targetable, clear his adds, then kill him.'},
    ]},
    {'name': 'Drunder, Fortress of Zek', 'zone': 'potactics', 'steps': [
      {'key': 'tallon', 'name': 'Tallon Zek',
       'desc': 'Locate and kill him; click the wall where he stood to zone out.'},
      {'key': 'vallon', 'name': 'Vallon Zek',
       'desc': 'Opposite side of the zone from Tallon. Same exit trick — the panel on the wall.'},
      {'key': 'rallos', 'name': 'Rallos Zek the Warlord',
       'desc': 'Kill <b>Decorin Berik</b> and <b>Decorin Grunhork</b> close together, refight Tallon and Vallon, '
               'then engage Rallos. He ports away at <b>50%</b> — follow him down to the arena and finish him '
               'inside 30 minutes.'},
    ], 'note': 'Post-flag: hail <b>Gram Dunnar</b> and follow <em>stories</em>.'},
    {'name': 'Tower of Solusek Ro', 'zone': 'solrotower', 'steps': [
      {'key': 'xuzl', 'name': 'Xuzl',
       'desc': 'Wing mini-boss — a firepot returns you to the base of each wing.'},
      {'key': 'arlyxir', 'name': 'Arlyxir', 'desc': 'Wing mini-boss.'},
      {'key': 'jiva', 'name': 'Jiva', 'desc': 'Wing mini-boss.'},
      {'key': 'rizlona', 'name': 'Rizlona', 'desc': 'Wing mini-boss.'},
      {'key': 'dresolik', 'name': 'The Protector of Dresolik', 'desc': 'Wing mini-boss.'},
      {'key': 'solusek', 'name': 'Solusek Ro',
       'desc': 'Click the port in the middle of the main room and kill him. Then open the floor door and swim '
               'down the <b>Lava Pit</b> into the Plane of Fire — this grants access to the Elemental Planes.'},
      {'key': 'librarian', 'name': 'Grand Librarian Maelin',
       'desc': "A fallback — if elemental access didn't take, hail him in the Plane of Knowledge."},
    ]},
  ]},

 {'n': 4, 'roman': 'IV', 'name': 'The Elemental Planes',
  'blurb': 'Fire, Air, Water and both Earths. Each one also yields a component of the Time key.',
  'groups': [
    {'name': 'Doomfire, the Burning Land', 'zone': 'pofire', 'steps': [
      {'key': 'fennin', 'name': 'Fennin Ro, the Tyrant of Fire',
       'desc': 'Four phases — the <b>Guardian of Doomfire</b>, then two named waves of four, then Fennin. '
               'Drops the <em>Globe of Dancing Flame</em>.'},
    ]},
    {'name': 'Eryslai, the Kingdom of Wind', 'zone': 'poair', 'steps': [
      {'key': 'xegony', 'name': 'Xegony, the Queen of Air',
       'desc': 'Hail <b>Sarhya the Dawnbreeze</b> for the <em>Pouch of Swirling Winds</em>, run the Stormrider, '
               'Elemental, Phoenix and Spider rings for the four Essences, and combine them into the <em>Wind '
               'Etched Key</em>. Xegony spawns adds at 98, 82, 66, 50, 34 and 18%. Drops the <em>Amorphous '
               'Cloud of Air</em>.'},
    ]},
    {'name': 'Reef of Coirnav', 'zone': 'powater', 'steps': [
      {'key': 'coirnav', 'name': 'Coirnav, the Avatar of Water',
       'desc': 'Four waves — focus the small adds and <b>ignore the named</b>. Pwelon, Nrinda and Vamuil return '
               'in a much weaker form before Coirnav spawns. Drops the <em>Sphere of Coalesced Water</em>.'},
    ]},
    {'name': 'Vegarlson, the Earthen Badlands', 'zone': 'poeartha', 'steps': [
      {'key': 'arbitor', 'name': 'A Mystical Arbitor of Earth',
       'desc': 'Kill the crocodile <b>Tantisala Jaggedtooth</b> for the boss key, clear all four ring events '
               '(Rock Golem, Mud Golem, Vine, Elementals), then take the Arbitor at the bottom of the zone. '
               'Grants the <em>Passkey of the Twelve</em>.'},
    ]},
    {'name': 'Ragrax, Stronghold of the Twelve', 'zone': 'poearthb', 'steps': [
      {'key': 'rathe', 'name': 'The Avatar of Earth',
       'desc': 'Clear both rooms, kill the three <b>War Chieftains</b> and <b>Warlord Gintolaken</b>, then the '
               'twelve <b>Rathe Council</b> members — six mezzable, six rootable. The Avatar spawns last and '
               'drops the <em>Mound of Living Stone</em>.'},
    ]},
  ]},

 {'n': 5, 'roman': 'V', 'name': 'The Plane of Time',
  'blurb': 'Craft the Quintessence of Elements, then take the Clocks into Time itself.',
  'summit': True,
  'groups': [
    {'name': 'Plane of Time', 'zone': 'potimea', 'steps': [
      {'key': 'maelin', 'name': 'Loreseeker Maelin',
       'desc': 'Find <b>Chronographer Muan</b> in the Plane of Innovation to port to the Plane of Time Clocks, '
               'then follow Loreseeker Maelin&#39;s dialog and click the machine to zone into the Plane of Time.'},
    ]},
  ]},
]

# The Time key, assembled from Tier 4 drops (reference only -- no flags).
KEY_PARTS = [
    ('fire',  'Plane of Fire',        ['Solidified Magma', 'Fiery Granite', 'Globe of Dancing Flame']),
    ('air',   'Plane of Air',         ['Alabaster Beak', 'Ivory Stormrider Canine', 'Amorphous Cloud of Air']),
    ('water', 'Plane of Water',       ['Emerald Colored Mephit Scales', 'Oil of a Frog', 'Sphere of Coalesced Water']),
    ('earth', 'Plane of Earth A / B', ['Liquefied Earth', 'Shattered Crystal', 'Mound of Living Stone']),
]


def _read_bucket(char_id):
    """(flags dict, account_id). Flags are per-ACCOUNT, so resolve the character's
    account first. Values of '0' mean not earned."""
    with get_db(logic.engine) as s:
        acct = s.execute(text("SELECT account_id FROM character_data WHERE id = :i"),
                         {'i': int(char_id)}).scalar()
        if not acct:
            return {}, None
        rows = s.execute(text("SELECT value FROM data_buckets WHERE `key` = 'pop' "
                              "AND account_id = :a"), {'a': int(acct)}).all()
    flags = {}
    for (v,) in rows:
        try:
            d = json.loads(v) or {}
        except (TypeError, ValueError):
            continue
        for k, val in (d.get('flags') or {}).items():
            if str(val).strip() not in ('', '0'):
                flags[k.strip().lower()] = str(val)
    return flags, acct


def _read_era_flags(acct):
    """{'RoK': {'lord nagafen', ...}, 'SoV': {...}} for one account.

    Cannot reuse _read_bucket(): these rows carry the account id in the KEY
    ('30886-progress-flag-RoK') with both id COLUMNS left at 0, and the value is
    a flat 'name=1;name=1' list rather than JSON."""
    if not acct:
        return {}
    with get_db(logic.engine) as s:
        rows = s.execute(text("SELECT `key`, value FROM data_buckets WHERE `key` LIKE :p"),
                         {'p': '%s-progress-flag-%%' % int(acct)}).all()
    out = {}
    for key, val in rows:
        held = out.setdefault(key.rsplit('-', 1)[-1], set())
        for part in (val or '').split(';'):
            name, _, v = part.partition('=')
            name = name.strip().lower()
            if name and v.strip() not in ('', '0'):
                held.add(name)
    return out


def _compute_gates(era):
    """(gate view-models, earned, first unearned boss or None)."""
    gates, earned, nxt = [], 0, None
    for G in ERA_GATES:
        held = era.get(G['code'], set())
        bosses, done = [], 0
        for B in G['bosses']:
            d = B['key'] in held
            done += 1 if d else 0
            bosses.append({'name': B['name'], 'npc': B['npc'], 'zone': B['zone'],
                           'short': B['short'], 'zid': B['zid'], 'level': B['level'],
                           'done': d})
            if not d and nxt is None:
                nxt = {'name': B['name'],
                       'where': 'Gate %d &middot; %s &middot; %s' % (G['n'], G['era'], B['zone'])}
        total = len(bosses)
        earned += done
        gates.append({'n': G['n'], 'code': G['code'], 'mark': G['mark'], 'era': G['era'],
                      'title': G['title'], 'blurb': G['blurb'], 'zones': G['zones'],
                      'opens': G['opens'], 'bosses': bosses,
                      'done': done, 'total': total, 'complete': done == total,
                      'pct': round(100.0 * done / total) if total else 0})
    # exactly one gate is "now" -- the first that is not finished
    first_open = next((g for g in gates if not g['complete']), None)
    for g in gates:
        g['state'] = 'done' if g['complete'] else ('now' if g is first_open else 'soon')
    return gates, earned, nxt


def compute(char_id, profile=None):
    """View-model for templates/character_progression.html."""
    flags, acct = _read_bucket(char_id)
    era = _read_era_flags(acct)

    gates, gates_done, next_step = _compute_gates(era)
    # PoP itself opens on the 4th gate; the Time ladder is unreachable before then.
    pop_open = bool(gates and gates[-1]['complete'])
    gates_cleared = sum(1 for g in gates if g['complete'])

    trials = [{'key': k, 'name': n, 'done': k in flags} for k, n in TRIALS]
    trial_done = any(t['done'] for t in trials)
    held_trial = next((t['name'] for t in trials if t['done']), None)

    # NB: next_step carries over from the gates -- a gate boss always outranks a
    # Time flag, because the Planes are not reachable until the gates are open.
    tiers, done_total, req_total = [], 0, 0

    def _where(T, G):
        return 'Tier %d &middot; %s &middot; %s' % (T['n'], T['name'].replace('&amp;', '&'), G['name'])

    for T in TIERS:
        groups, t_done, t_req = [], 0, 0
        for G in T['groups']:
            steps = []
            for S in G['steps']:
                d = S['key'] in flags
                steps.append({'name': S['name'], 'desc': S['desc'],
                              'stages': S.get('stages'), 'done': d})
                t_req += 1
                if d:
                    t_done += 1
                elif next_step is None:
                    next_step = {'name': S['name'], 'where': _where(T, G)}
            g = {'name': G['name'], 'zone': G['zone'], 'steps': steps, 'note': G.get('note')}
            if G.get('trials'):
                g['trials'] = trials
                g['trial_done'] = trial_done
                g['held_trial'] = held_trial
                t_req += 1
                if trial_done:
                    t_done += 1
                elif next_step is None:
                    next_step = {'name': 'Complete any one Tribunal trial', 'where': _where(T, G)}
            groups.append(g)
        done_total += t_done
        req_total += t_req
        state = 'done' if (t_req and t_done == t_req) else ('now' if t_done else 'soon')
        tiers.append({'n': T['n'], 'roman': T['roman'], 'name': T['name'], 'blurb': T['blurb'],
                      'summit': T.get('summit', False), 'groups': groups,
                      'done': t_done, 'total': t_req, 'state': state,
                      'pct': round(100.0 * t_done / t_req) if t_req else 0})

    # the first not-yet-cleared tier is the "current" one
    for t in tiers:
        if t['state'] == 'soon' and t['done'] == 0:
            break
    cleared = sum(1 for t in tiers if t['state'] == 'done')
    complete = done_total >= req_total and req_total > 0

    # "The road" is both systems end to end: 16 gate bosses then 42 Time flags.
    road_done = gates_done + done_total
    road_total = GATE_TOTAL + req_total

    return {
        'account_id': acct,
        'has_data': bool(flags or era),
        'flags': flags,

        # --- expansion gates ---
        'gates': gates,
        'gates_done': gates_done, 'gates_total': GATE_TOTAL,
        'gates_cleared': gates_cleared, 'gates_count': len(gates),
        'pop_open': pop_open,

        # --- Time flags (unchanged meaning) ---
        'done': done_total, 'total': req_total,
        'pct': round(100.0 * done_total / req_total) if req_total else 0,
        'tiers_cleared': cleared, 'tiers_total': len(tiers),

        # --- the whole road ---
        'road_done': road_done, 'road_total': road_total,
        'road_pct': round(100.0 * road_done / road_total) if road_total else 0,
        'remaining': max(0, road_total - road_done),
        'stages_cleared': gates_cleared + cleared,
        'stages_total': len(gates) + len(tiers),

        'next_step': next_step, 'complete': complete,
        'tiers': tiers, 'key_parts': KEY_PARTS,
        'story': flags.get('story'),
    }
