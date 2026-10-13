"""Guild Portal -- the guild directory, guild pages, and heraldry.

READS (all live, nothing cached to disk):
    guilds            14 rows -- name, leader (a character_id), motd, url, channel
    guild_members     char_id, guild_id, rank
    guild_ranks       per-guild rank titles: 1 Leader, 2 Senior Officer, 3 Officer, ...
    character_data    level / aa / last_login / account_id
    data_buckets      'pop' (Time flags, per ACCOUNT) and '{account}-progress-flag-{era}'
    player_event_logs 'Killed Raid NPC' -- guild raid kills

WHY KILLS COME FROM THE EVENT LOG, NOT THE LEADERBOARD
    The leaderboard only covers Solo/Duo/Trio groups, so a guild's
    actual raids are precisely what it filters out. player_event_logs records every
    kill at any group size, so guild kill counts are read straight from there.

RANKING
    Raw averages rank guilds badly: a five-person guild with nobody logged in for a
    fortnight posts a higher average level than the server's biggest active guild,
    because averaging a roster punishes anyone who recruits. So every pillar of
    standing scores a RATE (what share of the guild has done it) and a DEPTH (how
    many of them have), and the two are weighted evenly -- see _standing().
    Every raw column is still shown and sortable -- the composite is a default
    ordering, not a verdict.

HERALDRY
    A banner is six enum values, never an uploaded file: shape, field, two colours,
    a charge and a metal. Stored as a ~60 byte JSON blob and rendered to SVG at
    request time, which removes image storage, moderation, EXIF and malware from
    the design entirely. normalize_banner() whitelists every value, so a hand-made
    POST cannot inject anything into the rendered markup.
"""
from db import get_db
import json
import re

import logic
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

MAX_LEVEL = 65
ACTIVE_DAYS = 14
# Rank <= 2 -- Leader and Senior Officer only. Deliberately NOT 3: several guilds
# hand out plain Officer freely (Helmet Kids United is 13 members and 13 officers,
# Logic Wizards 21 of 26), which would have made "officer controls" mean the whole
# guild. Every edit is also stamped with the editor's character name, so a misuse
# has someone's name on it.
OFFICER_RANK = 2
LEADER_RANK = 1

# --------------------------------------------------------------------------- #
# Heraldry                                                                     #
# --------------------------------------------------------------------------- #
# Every option is an enum. render_banner() only ever emits values it looked up
# from these dicts, so nothing user-supplied reaches the SVG.
BANNER_SHAPES = {
    'heater':  'M20,18 H180 V120 C180,180 140,212 100,226 C60,212 20,180 20,120 Z',
    'banner':  'M22,16 H178 V206 L100,224 L22,206 Z',
    'pennant': 'M22,16 H178 V186 L100,226 L22,186 Z',
    'roundel': 'M100,20 A90,90 0 1,1 99.9,20 Z',
    'spade':   'M100,14 C150,60 180,96 180,138 C180,186 142,214 100,226 '
               'C58,214 20,186 20,138 C20,96 50,60 100,14 Z',
}

BANNER_FIELDS = {
    'solid':   None,
    'bend':    'M0,0 L200,0 L0,240 Z',
    'pale':    'M70,0 H130 V240 H70 Z',
    'fess':    'M0,96 H200 V150 H0 Z',
    'quarter': 'M0,0 H100 V120 H0 Z M100,120 H200 V240 H100 Z',
    'chevron': 'M100,58 L200,168 V240 H158 L100,148 L42,240 H0 V168 Z',
    'gyronny': 'M100,120 L0,0 H100 Z M100,120 L200,0 V120 Z M100,120 L200,240 H100 Z '
               'M100,120 L0,240 V120 Z',
}

# Field colours -- the shield ground. Presets are quick picks only; any valid
# six-digit hex is accepted (see normalize_banner), so the builder can offer a
# full colour wheel without opening a hole: a hex triplet cannot contain a quote
# or an angle bracket, so it is safe to drop straight into an SVG attribute.
BANNER_COLORS = [
    ('#141a24', 'Iron'),      ('#0b1b3a', 'Midnight'),  ('#1d4a75', 'Deep'),
    ('#37475c', 'Slate'),     ('#4db2ff', 'Arcane'),    ('#8fa5eb', 'Twilight'),
    ('#0f2f24', 'Forest'),    ('#2d4a1e', 'Moss'),      ('#5fe0a3', 'Jade'),
    ('#5a1f12', 'Oxblood'),   ('#7a1220', 'Blood'),     ('#8a3a1a', 'Rust'),
    ('#ff8a4a', 'Ember'),     ('#7a5a1e', 'Bronze'),    ('#2c2340', 'Void'),
    ('#4a1d52', 'Plum'),      ('#6b2f6b', 'Amethyst'),  ('#ece5d6', 'Bone'),
]

# The charge + trim colour. "Metal" is the heraldic term, but the set runs well
# past gold and silver -- an earlier palette had FOUR near-whites (silver,
# platinum, ivory, electrum) which read as one option, so this one is spread
# across the hue wheel instead and keeps only two light neutrals.
BANNER_METALS = [
    ('#ffd23f', 'Sunfire'),   ('#e8c76a', 'Gold'),      ('#c8873f', 'Bronze'),
    ('#ffb289', 'Copper'),    ('#ff6a3d', 'Molten'),    ('#ff5d5d', 'Crimson'),
    ('#ff7fb0', 'Rose'),      ('#c084fc', 'Amethyst'),  ('#8b6cf0', 'Arcane Violet'),
    ('#4f9dff', 'Azure'),     ('#8fd0ff', 'Frost'),     ('#35d6e8', 'Cyan'),
    ('#4fd3b0', 'Verdigris'), ('#4fd97d', 'Emerald'),   ('#b4e04a', 'Toxic'),
    ('#93a7bd', 'Steel'),     ('#d7e0ea', 'Silver'),    ('#f0e6d2', 'Bone'),
]

# Charges drawn from Norrath. 'C' is substituted with the chosen metal colour;
# '#0a0f18' is the shadow used for cut-in detail (sockets, fullers, wood grain).
#
# DRAWING RULES, learned the hard way -- the first pass was primitives bolted
# together and read as clip-art:
#   * silhouette first. A charge is identified by its outline at 54px on a
#     directory card, long before any interior detail is legible.
#   * detail is CUT OUT of the silhouette in shadow, never added as outlines on
#     top -- that is what separates an engraved look from a sticker.
#   * the charge sits in a ~110x120 box centred near (100,118); anything wider
#     collides with the shield edge on the pennant and roundel shapes.
#   * favour one continuous compound path over many overlapping shapes; seams
#     between stacked primitives show as hairlines against a dark field.
BANNER_CHARGES = {
    'none':    ('None', ''),

    'triune':  ('Triune Sigil',
                '<path d="M100,60 L152,152 H48 Z M100,88 L128,138 H72 Z" fill="C" fill-rule="evenodd"/>'
                '<circle cx="100" cy="60" r="11" fill="C"/><circle cx="152" cy="152" r="11" fill="C"/>'
                '<circle cx="48" cy="152" r="11" fill="C"/>'
                '<circle cx="100" cy="60" r="4.5" fill="#0a0f18"/>'
                '<circle cx="152" cy="152" r="4.5" fill="#0a0f18"/>'
                '<circle cx="48" cy="152" r="4.5" fill="#0a0f18"/>'),

    # Dragon's head in profile, facing left -- a full-body wyrm is unreadable at
    # 54px, but a head silhouette (long snout, backswept horns, open jaw) is not.
    'wyrm':    ('Dragon',
                '<path d="M104,66 C112,50 130,38 154,34 C144,50 134,66 128,84 Z" fill="C"/>'
                '<path d="M122,92 C136,78 152,70 170,70 C156,80 146,92 142,106 Z" fill="C"/>'
                '<path d="M34,124 C48,110 68,99 90,94 C98,86 104,77 104,66 '
                'C112,73 119,83 122,92 C138,101 148,118 148,140 '
                'C148,164 132,182 108,188 C112,174 108,161 98,155 '
                'C74,155 52,145 36,134 C42,131 41,127 34,124 Z" fill="C"/>'
                '<circle cx="92" cy="108" r="7" fill="#0a0f18"/>'
                '<circle cx="94" cy="105" r="2.4" fill="C"/>'
                '<ellipse cx="47" cy="120" rx="5" ry="3.5" fill="#0a0f18" opacity=".7"/>'
                '<path d="M56,136 l7,10 6,-8 Z M74,142 l7,10 6,-9 Z M94,146 l6,10 6,-9 Z"'
                ' fill="#0a0f18" opacity=".75"/>'
                '<path d="M126,110 C136,120 140,132 139,146 C132,136 126,128 118,124 Z"'
                ' fill="#0a0f18" opacity=".35"/>'),

    'skull':   ('Skull',
                '<path d="M100,58 C132,58 154,82 154,112 C154,130 146,143 134,151 '
                'L134,168 C134,176 128,181 118,181 L82,181 C72,181 66,176 66,168 '
                'L66,151 C54,143 46,130 46,112 C46,82 68,58 100,58 Z" fill="C"/>'
                '<path d="M62,110 C62,98 72,90 84,90 C94,90 100,98 98,110 '
                'C96,122 86,128 76,126 C67,124 62,118 62,110 Z" fill="#0a0f18"/>'
                '<path d="M138,110 C138,98 128,90 116,90 C106,90 100,98 102,110 '
                'C104,122 114,128 124,126 C133,124 138,118 138,110 Z" fill="#0a0f18"/>'
                '<path d="M100,124 L110,148 H90 Z" fill="#0a0f18"/>'
                '<path d="M78,158 h8 v23 h-8 Z M94,158 h12 v23 h-12 Z M114,158 h8 v23 h-8 Z"'
                ' fill="#0a0f18" opacity=".8"/>'),

    'flame':   ('Flame',
                '<path d="M100,52 C114,80 130,96 138,114 C148,136 140,162 118,174 '
                'C124,158 120,146 110,140 C114,156 106,172 92,178 C74,172 64,154 68,134 '
                'C70,124 74,116 74,108 C80,116 84,122 90,124 C84,102 88,74 100,52 Z" fill="C"/>'
                '<path d="M100,104 C108,120 114,130 114,142 C114,158 106,168 96,170 '
                'C86,164 82,152 86,140 C90,128 98,118 100,104 Z" fill="#0a0f18" opacity=".42"/>'),

    'tower':   ('Tower',
                '<path d="M58,182 V96 H52 V78 H70 V64 H84 V78 H93 V64 H107 V78 H116 V64 H130 V78 '
                'H148 V96 H142 V182 Z" fill="C"/>'
                '<path d="M86,182 V148 C86,140 92,134 100,134 C108,134 114,140 114,148 V182 Z"'
                ' fill="#0a0f18"/>'
                '<path d="M74,104 h14 v20 h-14 Z M112,104 h14 v20 h-14 Z" fill="#0a0f18"/>'
                '<path d="M52,182 h96 v8 h-96 Z" fill="C"/>'),

    'sword':   ('Sword',
                '<path d="M100,46 C104,58 108,68 109,78 V132 H91 V78 C92,68 96,58 100,46 Z" fill="C"/>'
                '<path d="M100,62 V128" stroke="#0a0f18" stroke-width="3" opacity=".5"/>'
                '<path d="M64,132 C76,128 124,128 136,132 C130,144 118,146 100,146 '
                'C82,146 70,144 64,132 Z" fill="C"/>'
                '<path d="M93,146 h14 v30 h-14 Z" fill="C"/>'
                '<path d="M93,152 h14 M93,160 h14 M93,168 h14" stroke="#0a0f18" stroke-width="2.5" opacity=".55"/>'
                '<circle cx="100" cy="184" r="10" fill="C"/>'
                '<circle cx="100" cy="184" r="4" fill="#0a0f18" opacity=".5"/>'),

    # Hafts as filled quads rather than strokes -- a stroked line keeps its width
    # under transform but reads thin and wiry next to the solid blades.
    'axes':    ('Crossed Axes',
                '<path d="M56,180 L68,186 L146,64 L134,58 Z" fill="C"/>'
                '<path d="M144,180 L132,186 L54,64 L66,58 Z" fill="C"/>'
                # Each blade belongs on the END of its own haft: haft one runs
                # bottom-left to top-RIGHT, so its head is the right-hand blade.
                '<path d="M140,40 C166,48 180,72 172,98 C154,92 142,78 136,58 Z" fill="C"/>'
                '<path d="M60,40 C34,48 20,72 28,98 C46,92 58,78 64,58 Z" fill="C"/>'
                '<path d="M141,52 C158,60 168,76 165,92 C153,86 145,74 140,60 Z"'
                ' fill="#0a0f18" opacity=".26"/>'
                '<path d="M59,52 C42,60 32,76 35,92 C47,86 55,74 60,60 Z"'
                ' fill="#0a0f18" opacity=".26"/>'
                '<circle cx="100" cy="122" r="11" fill="C"/>'
                '<circle cx="100" cy="122" r="4.5" fill="#0a0f18" opacity=".6"/>'),

    'hammer':  ('War Hammer',
                '<path d="M44,64 H120 C128,64 134,70 134,78 V102 C134,110 128,116 120,116 '
                'H44 C38,116 34,112 34,106 V74 C34,68 38,64 44,64 Z" fill="C"/>'
                '<path d="M134,80 L168,88 L168,92 L134,100 Z" fill="C"/>'
                '<path d="M42,72 h14 v36 h-14 Z" fill="#0a0f18" opacity=".4"/>'
                '<path d="M118,72 h10 v36 h-10 Z" fill="#0a0f18" opacity=".22"/>'
                '<path d="M74,116 h20 v70 h-20 Z" fill="C"/>'
                '<path d="M74,130 h20 v5 h-20 Z M74,146 h20 v5 h-20 Z M74,162 h20 v5 h-20 Z"'
                ' fill="#0a0f18" opacity=".5"/>'),

    'crown':   ('Crown',
                '<path d="M50,158 L44,84 L72,114 L86,72 L100,98 L114,72 L128,114 L156,84 L150,158 Z"'
                ' fill="C"/>'
                '<path d="M48,160 h104 v20 h-104 Z" fill="C"/>'
                '<circle cx="44" cy="80" r="8" fill="C"/><circle cx="86" cy="68" r="8" fill="C"/>'
                '<circle cx="114" cy="68" r="8" fill="C"/><circle cx="156" cy="80" r="8" fill="C"/>'
                '<circle cx="72" cy="170" r="5" fill="#0a0f18" opacity=".5"/>'
                '<circle cx="100" cy="170" r="5" fill="#0a0f18" opacity=".5"/>'
                '<circle cx="128" cy="170" r="5" fill="#0a0f18" opacity=".5"/>'),

    'eye':     ('Watching Eye',
                '<path d="M34,118 C58,80 142,80 166,118 C142,156 58,156 34,118 Z" fill="C"/>'
                '<path d="M46,118 C66,92 134,92 154,118 C134,144 66,144 46,118 Z" fill="#0a0f18"/>'
                '<circle cx="100" cy="118" r="24" fill="C"/>'
                '<ellipse cx="100" cy="118" rx="8" ry="20" fill="#0a0f18"/>'
                '<circle cx="91" cy="108" r="5" fill="#0a0f18" opacity=".35"/>'),

    # Beziers, not arcs: the A command's flags are easy to get subtly wrong and a
    # half-drawn crescent looks like a rendering fault rather than a moon.
    'moon':    ('Luclin',
                '<path d="M116,44 C76,50 46,80 46,118 C46,156 76,186 116,192 '
                'C86,176 68,150 68,118 C68,86 86,60 116,44 Z" fill="C"/>'
                '<circle cx="60" cy="98" r="7" fill="#0a0f18" opacity=".3"/>'
                '<circle cx="72" cy="146" r="5" fill="#0a0f18" opacity=".26"/>'
                '<circle cx="58" cy="132" r="4" fill="#0a0f18" opacity=".22"/>'
                '<path d="M150,62 l5,15 15,5 -15,5 -5,15 -5,-15 -15,-5 15,-5 Z" fill="C"/>'
                '<path d="M158,132 l3.5,10 10,3.5 -10,3.5 -3.5,10 -3.5,-10 -10,-3.5 10,-3.5 Z"'
                ' fill="C"/>'),

    'tree':    ('World Tree',
                '<path d="M100,40 C126,40 146,56 152,78 C170,86 176,110 164,128 '
                'C154,144 132,150 114,144 V178 H86 V144 C68,150 46,144 36,128 '
                'C24,110 30,86 48,78 C54,56 74,40 100,40 Z" fill="C"/>'
                '<path d="M50,192 C68,178 82,170 86,164 H114 C118,170 132,178 150,192 '
                'C130,182 114,178 100,178 C86,178 70,182 50,192 Z" fill="C"/>'
                '<path d="M100,66 C114,82 118,100 112,118 C108,106 104,100 100,98 '
                'C96,100 92,106 88,118 C82,100 86,82 100,66 Z" fill="#0a0f18" opacity=".3"/>'
                '<path d="M96,144 h8 v32 h-8 Z" fill="#0a0f18" opacity=".22"/>'),

    'wolf':    ('Wolf',
                '<path d="M100,190 C82,184 70,170 64,152 L54,86 L84,110 '
                'C90,105 110,105 116,110 L146,86 L136,152 C130,170 118,184 100,190 Z" fill="C"/>'
                '<path d="M62,96 L80,112 L72,124 Z M138,96 L120,112 L128,124 Z"'
                ' fill="#0a0f18" opacity=".42"/>'
                '<path d="M68,130 L92,138 L86,148 L66,140 Z" fill="#0a0f18"/>'
                '<path d="M132,130 L108,138 L114,148 L134,140 Z" fill="#0a0f18"/>'
                '<path d="M100,150 C110,150 116,158 116,168 C116,178 108,185 100,187 '
                'C92,185 84,178 84,168 C84,158 90,150 100,150 Z" fill="#0a0f18" opacity=".5"/>'
                '<path d="M100,156 C106,156 110,160 110,164 C110,169 105,172 100,172 '
                'C95,172 90,169 90,164 C90,160 94,156 100,156 Z" fill="#0a0f18"/>'
                '<path d="M100,172 v10 M100,182 l-8,5 M100,182 l8,5"'
                ' stroke="#0a0f18" stroke-width="3"/>'),

    'spider':  ('Spider',
                '<g stroke="C" stroke-width="6" stroke-linecap="round" fill="none">'
                '<path d="M80,116 C60,110 48,96 44,78"/><path d="M78,130 C56,128 40,120 30,106"/>'
                '<path d="M78,144 C58,150 44,162 38,178"/><path d="M82,154 C70,166 64,176 62,186"/>'
                '<path d="M120,116 C140,110 152,96 156,78"/><path d="M122,130 C144,128 160,120 170,106"/>'
                '<path d="M122,144 C142,150 156,162 162,178"/><path d="M118,154 C130,166 136,176 138,186"/>'
                '</g>'
                '<ellipse cx="100" cy="142" rx="27" ry="34" fill="C"/>'
                '<ellipse cx="100" cy="104" rx="18" ry="16" fill="C"/>'
                '<path d="M100,120 C92,132 88,144 90,158 C94,168 106,168 110,158 '
                'C112,144 108,132 100,120 Z" fill="#0a0f18" opacity=".45"/>'
                '<circle cx="92" cy="100" r="4" fill="#0a0f18"/><circle cx="108" cy="100" r="4" fill="#0a0f18"/>'),

    # An S-curve (heraldic "glissant") reads as a snake instantly; the spiral it
    # replaced read as a seahorse.
    'serpent': ('Serpent',
                '<path d="M58,186 C58,158 94,158 96,132 C98,106 62,104 64,78 '
                'C66,58 84,48 104,50" fill="none" stroke="C" stroke-width="17"'
                ' stroke-linecap="round"/>'
                '<path d="M102,38 C124,38 144,46 154,60 C142,66 126,68 112,64 '
                'C106,58 102,48 102,38 Z" fill="C"/>'
                '<circle cx="118" cy="52" r="4.5" fill="#0a0f18"/>'
                '<path d="M154,58 L174,52 M154,63 L174,70" stroke="C" stroke-width="4"'
                ' stroke-linecap="round"/>'
                '<path d="M74,96 C86,94 94,102 94,114 C86,110 78,106 74,96 Z"'
                ' fill="#0a0f18" opacity=".25"/>'),

    'harp':    ('Harp',
                '<path d="M74,182 C60,148 62,102 86,70 C102,50 124,42 142,42 L142,58 '
                'C126,58 108,66 96,84 C78,112 76,150 88,182 Z" fill="C"/>'
                '<g stroke="C" stroke-width="3" opacity=".85">'
                '<path d="M136,60 L84,168"/><path d="M138,74 L92,172"/>'
                '<path d="M140,88 L100,175"/><path d="M141,102 L108,177"/>'
                '<path d="M142,116 L116,179"/></g>'
                '<path d="M136,42 h14 v138 h-14 Z" fill="C"/>'
                '<path d="M66,180 h34 v10 h-34 Z" fill="C"/>'),

    # Horn tapering left, waisted body, splayed foot -- the three things that make
    # an anvil an anvil rather than a lump.
    'anvil':   ('Anvil',
                '<path d="M22,96 C44,84 70,78 100,78 H160 V92 '
                'C160,104 152,112 138,115 L132,142 H150 V158 H50 V142 H68 L62,115 '
                'C44,112 30,106 22,96 Z" fill="C"/>'
                '<path d="M22,96 C42,96 58,94 72,90 C60,102 42,104 26,100 Z"'
                ' fill="#0a0f18" opacity=".35"/>'
                '<path d="M44,160 C44,170 52,174 66,176 H134 C148,174 156,170 156,160 '
                'L158,186 H42 Z" fill="C"/>'
                '<path d="M118,84 h16 v9 h-16 Z" fill="#0a0f18" opacity=".55"/>'
                '<path d="M84,115 h32 v27 h-32 Z" fill="#0a0f18" opacity=".22"/>'),

    'chalice': ('Chalice',
                '<path d="M58,64 H142 C142,102 126,130 100,138 C74,130 58,102 58,64 Z" fill="C"/>'
                '<path d="M70,76 H130 C129,100 118,120 100,127 C82,120 71,100 70,76 Z"'
                ' fill="#0a0f18" opacity=".38"/>'
                '<path d="M93,138 h14 v30 h-14 Z" fill="C"/>'
                '<path d="M66,180 C66,170 78,166 100,166 C122,166 134,170 134,180 L136,188 H64 Z"'
                ' fill="C"/>'
                '<circle cx="100" cy="152" r="8" fill="C"/>'
                '<circle cx="100" cy="152" r="3.5" fill="#0a0f18" opacity=".5"/>'),

    'star':    ('Radiant Star',
                '<path d="M100,42 C106,86 112,104 122,112 C132,120 148,124 172,118 '
                'C148,124 132,128 122,136 C112,144 106,162 100,196 '
                'C94,162 88,144 78,136 C68,128 52,124 28,118 '
                'C52,124 68,120 78,112 C88,104 94,86 100,42 Z" fill="C"/>'
                '<circle cx="100" cy="118" r="10" fill="#0a0f18" opacity=".4"/>'),
}

DEFAULT_BANNER = {'shape': 'heater', 'field': 'bend', 'c1': '#1d4a75',
                  'c2': '#4db2ff', 'charge': 'triune', 'metal': '#e8c76a'}

_HEX = re.compile(r'^#[0-9a-fA-F]{6}$')


def _hex_or(raw, fallback):
    """Any six-digit hex colour, or the fallback.

    Colours are validated by SHAPE rather than membership, which is what lets the
    builder offer a full colour wheel. It is not a loosening of the guarantee:
    the pattern is anchored and admits only '#' plus six hex digits, so the value
    can hold no quote, angle bracket, semicolon or url() -- nothing that could
    escape the SVG attribute it lands in. Shape, field and charge stay strict
    enums because those DO expand into markup.
    """
    v = (raw or '').strip()
    return v if _HEX.match(v) else fallback


# --------------------------------------------------------------------------- #
# Officer-supplied links                                                       #
# --------------------------------------------------------------------------- #
# These end up as clickable links on a PUBLIC page, filled in by whoever holds
# Senior Officer somewhere. That is a phishing surface, so both are validated
# rather than trusted:
#   * a Discord invite must actually be a Discord invite -- host-whitelisted, so
#     "our discord" can never quietly point at a credential-harvesting clone.
#   * a website must be http(s) with a plausible host; javascript:, data: and
#     bare text are rejected outright.
# The template also renders the real hostname next to the link and marks it
# rel="nofollow ugc noopener", so nobody clicks blind.
_DISCORD = re.compile(
    r'^https://(?:discord\.gg|discord\.com/invite|discordapp\.com/invite)/[A-Za-z0-9-]{2,32}$')
_URL = re.compile(r'^https?://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:[/?#][^\s<>"\']*)?$')
MAX_URL = 200


def clean_discord(raw):
    """A Discord invite URL, or ''. Accepts a bare code or a discord.gg link and
    normalises both to the canonical https form."""
    v = (raw or '').strip()
    if not v:
        return ''
    if re.match(r'^[A-Za-z0-9-]{2,32}$', v):          # they pasted just the code
        v = 'https://discord.gg/%s' % v
    v = re.sub(r'^http://', 'https://', v)
    v = re.sub(r'^(?!https://)(?=(?:www\.)?discord)', 'https://', v)
    v = v.replace('https://www.', 'https://')
    return v[:MAX_URL] if _DISCORD.match(v) else ''


def clean_url(raw):
    """An http(s) URL, or ''."""
    v = (raw or '').strip()
    if v and '://' not in v:
        v = 'https://' + v
    return v[:MAX_URL] if _URL.match(v) else ''


def host_of(url):
    """Hostname for display beside a link, so nobody clicks blind."""
    m = re.match(r'^https?://([A-Za-z0-9.-]+)', url or '')
    return m.group(1) if m else ''


def normalize_banner(raw):
    """Coerce anything (a form dict, a stored JSON string, None) into a safe
    banner config. Structural keys are whitelisted enums; colours must match a
    strict hex pattern. Anything else falls back to the default, so a forged POST
    can never reach the SVG."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None
    raw = raw or {}
    get = raw.get if hasattr(raw, 'get') else (lambda k, d=None: d)
    cfg = dict(DEFAULT_BANNER)
    if get('shape') in BANNER_SHAPES:
        cfg['shape'] = get('shape')
    if get('field') in BANNER_FIELDS:
        cfg['field'] = get('field')
    if get('charge') in BANNER_CHARGES:
        cfg['charge'] = get('charge')
    cfg['c1'] = _hex_or(get('c1'), DEFAULT_BANNER['c1'])
    cfg['c2'] = _hex_or(get('c2'), DEFAULT_BANNER['c2'])
    cfg['metal'] = _hex_or(get('metal'), DEFAULT_BANNER['metal'])
    return cfg


def render_banner(cfg, width=110, uid=''):
    """Banner config -> standalone SVG string. `uid` disambiguates the clipPath id
    when several banners share a page (duplicate ids would cross-clip)."""
    c = normalize_banner(cfg)
    shape = BANNER_SHAPES[c['shape']]
    field = BANNER_FIELDS[c['field']]
    charge = BANNER_CHARGES[c['charge']][1].replace('C"', c['metal'] + '"')
    cid = 'bcl%s' % (uid or c['shape'])
    h = int(width * 240 / 200.0)
    return (
        '<svg class="gp-banner" viewBox="0 0 200 240" width="%d" height="%d" '
        'role="img" aria-label="Guild banner">'
        '<defs><clipPath id="%s"><path d="%s"/></clipPath></defs>'
        '<g clip-path="url(#%s)">'
        '<rect x="0" y="0" width="200" height="240" fill="%s"/>'
        '%s%s</g>'
        '<path d="%s" fill="none" stroke="#0a0f18" stroke-width="5"/>'
        '<path d="%s" fill="none" stroke="%s" stroke-width="2"/>'
        '</svg>'
    ) % (width, h, cid, shape, cid, c['c1'],
         ('<path d="%s" fill="%s"/>' % (field, c['c2'])) if field else '',
         charge, shape, shape, c['metal'])


# --------------------------------------------------------------------------- #
# Reads                                                                        #
# --------------------------------------------------------------------------- #
# Private characters are excluded exactly as they are on the player boards:
# gm > 0, deleted, or anon = 1 never appear. anon = 2 (roleplay) is public.
_VISIBLE = "cd.gm = 0 AND cd.deleted_at IS NULL AND cd.anon <> 1"


def _activity_anchor(s):
    """Unix time the 'active' window counts back from -- the most recent login on
    the server. On live that is effectively now; on a restored snapshot it anchors
    to the snapshot instead of reporting the whole server as inactive."""
    return int(s.execute(text("SELECT COALESCE(MAX(last_login), 0) FROM character_data")).scalar() or 0)


def _roster_stats(s):
    """{guild_id: {...}} -- size, activity, levels, AA."""
    cutoff = _activity_anchor(s) - ACTIVE_DAYS * 86400
    rows = s.execute(text(f"""
        SELECT gm.guild_id,
               COUNT(*)                                              AS members,
               SUM(cd.last_login >= :cut)                            AS active,
               ROUND(AVG(cd.level), 1)                               AS avg_level,
               MAX(cd.level)                                         AS max_level,
               SUM(cd.level >= :ml)                                  AS capped,
               ROUND(AVG(cd.aa_points_spent))                        AS avg_aa,
               ROUND(AVG(CASE WHEN cd.level >= :ml
                              THEN cd.aa_points_spent END))          AS capped_aa,
               COUNT(DISTINCT cd.account_id)                         AS accounts
        FROM guild_members gm
        JOIN character_data cd ON cd.id = gm.char_id AND {_VISIBLE}
        GROUP BY gm.guild_id
    """), {'cut': cutoff, 'ml': MAX_LEVEL}).mappings().all()
    return {r['guild_id']: dict(r) for r in rows}


def _flag_stats(s):
    """{guild_id: {'pop_unlocked': n, 'time_started': n}} -- both per ACCOUNT,
    because both flag systems are account-owned (see pop_progress)."""
    out = {}
    for gid, n in s.execute(text(f"""
        SELECT gm.guild_id, COUNT(DISTINCT cd.account_id)
        FROM guild_members gm
        JOIN character_data cd ON cd.id = gm.char_id AND {_VISIBLE}
        JOIN data_buckets db ON db.`key` = CONCAT(cd.account_id, '-progress-flag-PoP')
        GROUP BY gm.guild_id
    """)).all():
        out.setdefault(gid, {})['pop_unlocked'] = n
    for gid, n in s.execute(text(f"""
        SELECT gm.guild_id, COUNT(DISTINCT cd.account_id)
        FROM guild_members gm
        JOIN character_data cd ON cd.id = gm.char_id AND {_VISIBLE}
        JOIN data_buckets db ON db.`key` = 'pop' AND db.account_id = cd.account_id
        GROUP BY gm.guild_id
    """)).all():
        out.setdefault(gid, {})['time_started'] = n
    return out


KILL_WINDOW_DAYS = 7


def _kill_stats(s):
    """{guild_id: raid kills in the last KILL_WINDOW_DAYS}.

    A ROLLING 7-DAY figure, not a lifetime total -- player_event_logs prunes kill
    events after a week. It measures current raid tempo and self-limits; label it
    as such wherever it is shown.

    READ FROM THE ACCUMULATED DAY TABLE, NOT THE EVENT LOG. Deriving this from
    raw events on every page load is what made guild pages take ~13.5s on live
    while every other tab answered in under half a second: it scanned ~435k rows
    and built a COUNT(DISTINCT CONCAT(...)) set of 181,090 values -- 140,722 of
    them in a single group -- which overflowed tmp_table_size and spilled to an
    on-disk temp table. The spill is why 8x the data cost ~28x the time. Every
    guild page paid it, which is why a three-member guild was as slow as the
    biggest one. guild_achievements.accumulate_raid_days() now folds the same
    counts forward into ~180 small rows.
    """
    try:
        import guild_achievements
        counts = guild_achievements.raid_kill_window(KILL_WINDOW_DAYS)
        if counts:
            return counts
    except Exception:
        pass
    # Never seeded (or the leaderboard DB is unreachable): scan the log so the
    # page shows real numbers rather than zeros. Slow, and self-correcting -- the
    # next accumulator run seeds the table and this path stops being taken.
    return _kill_stats_live(s)


def _kill_stats_live(s):
    """The original event-log scan. Fallback and reference only -- see above.

    Filtering on event_type_name rather than event_type_id looks like it wastes
    the (event_type_id, created_at) index, but it does not: measured, the
    optimizer drives this from guild_members and reaches the log through the
    character_id index, so both spellings produce the same plan and the same
    time.
    """
    rows = s.execute(text("""
        SELECT gm.guild_id,
               COUNT(DISTINCT CONCAT(l.created_at, '-',
                     JSON_EXTRACT(l.event_data, '$.npc_id')))
        FROM player_event_logs l
        JOIN guild_members gm ON gm.char_id = l.character_id
        WHERE l.event_type_name = 'Killed Raid NPC'
        GROUP BY gm.guild_id
    """)).all()
    return {gid: n for gid, n in rows}


# Depth caps -- the point at which a pillar is considered maxed. Set near the top
# of the live server's real distribution so the leading guild saturates and
# everyone else is scored as a fraction of the best anyone has actually managed.
DEPTH_POP = 40        # accounts through the PoP gate
DEPTH_TIME = 30       # accounts into Plane of Time
DEPTH_ACTIVE = 60     # characters seen inside the activity window
DEPTH_CAPPED = 20     # level-65 members
# Raid kills are SQRT-damped, not linear: the top guild runs ~140k in the window
# and the next ~7k, so linear would make this a one-guild category. sqrt keeps
# the ordering while leaving a mid-size guild a visible share of the pillar.
KILL_CAP = 50000


def _standing(g):
    """0-1000 composite. Deliberately NOT an average of the roster.

    RATE AND DEPTH, EVENLY WEIGHTED. The first version scored progression and
    activity as pure roster percentages, which quietly punished the exact thing
    the ranking claims to reward: a 144-member guild that had taken 55 accounts
    through PoP scored WORSE on progression than a 15-man guild where all 15 had
    done it, and recruiting one unflagged newbie lowered your standing. So each
    pillar now scores both -- what share of the guild has done the thing, and how
    many of them have -- at 50/50. Power already worked this way (the AA of
    max-level members, plus how many were fielded) and is unchanged.

    RAIDING IS ITS OWN PILLAR. Raid kills were previously worth nothing at all
    despite being the most prominent number on the card, so the server's most
    active raid guild got no credit for a 20x lead. NOTE this is a ROLLING 7-DAY
    count -- player_event_logs prunes kills at seven days -- so it measures
    current raid tempo, not lifetime, and cannot inflate forever.

    ACHIEVEMENTS are worth 13%, not more, ON PURPOSE. Seven of the eleven badges
    are computed from stats this score already counts (level 65s, AA, PoP flags,
    activity), so a heavier weight would be double-counting the same facts and
    quietly turning standing into "progression, twice".

    SIZE dropped 10% -> 5% because the depth halves above already reward a large
    ACTIVE guild. Left in at a token weight so recruiting still nudges upward,
    but a big idle roster no longer collects a tenth of the score for existing.
    """
    accounts = max(g['accounts'], 1)
    members = max(g['members'], 1)

    prog_rate = (g['pop_unlocked'] / accounts) * 0.6 + (g['time_started'] / accounts) * 0.4
    prog_depth = (min(g['pop_unlocked'] / float(DEPTH_POP), 1.0) * 0.6
                  + min(g['time_started'] / float(DEPTH_TIME), 1.0) * 0.4)
    progression = prog_rate * 0.5 + prog_depth * 0.5

    activity = ((g['active'] / members) * 0.5
                + min(g['active'] / float(DEPTH_ACTIVE), 1.0) * 0.5)

    # power: the AA of a guild's MAX-LEVEL members, plus how many it has fielded
    capped_aa = g['capped_aa'] or 0
    power = (min(capped_aa / 2500.0, 1.0) * 0.6
             + min(g['capped'] / float(DEPTH_CAPPED), 1.0) * 0.4)

    raids = min((g['kills'] ** 0.5) / (KILL_CAP ** 0.5), 1.0)

    ach = min(g.get('ach_pct', 0) / 100.0, 1.0)
    # scale: damped so 83 members is worth ~2x of 9, not ~9x
    scale = min((members ** 0.5) / 9.0, 1.0)

    return round(1000 * (progression * 0.26 + power * 0.22 + activity * 0.18
                         + raids * 0.16 + ach * 0.13 + scale * 0.05))


def list_guilds(include_empty=False):
    """Every guild, ranked by standing. `include_empty` keeps guilds with no
    visible members (there are a couple) for staff views.

    Uncached -- call directory() instead from a request path.
    """
    try:
        import guild_achievements
        ach = guild_achievements.for_all()
    except Exception:
        ach = {}                        # achievements simply contribute 0

    with get_db(logic.engine) as s:
        guilds = s.execute(text(
            "SELECT id, name, leader, motd, url FROM guilds")).mappings().all()
        roster = _roster_stats(s)
        flags = _flag_stats(s)
        kills = _kill_stats(s)

    out = []
    for g in guilds:
        r = roster.get(g['id'])
        if not r and not include_empty:
            continue
        r = r or {'members': 0, 'active': 0, 'avg_level': None, 'max_level': None,
                  'capped': 0, 'avg_aa': 0, 'capped_aa': 0, 'accounts': 0}
        f = flags.get(g['id'], {})
        row = {
            'id': g['id'], 'name': g['name'], 'leader': g['leader'],
            'motd': (g['motd'] or '').strip(), 'url': (g['url'] or '').strip(),
            'members': int(r['members'] or 0), 'active': int(r['active'] or 0),
            'avg_level': float(r['avg_level']) if r['avg_level'] is not None else 0.0,
            'max_level': int(r['max_level'] or 0),
            'capped': int(r['capped'] or 0),
            'avg_aa': int(r['avg_aa'] or 0), 'capped_aa': int(r['capped_aa'] or 0),
            'accounts': int(r['accounts'] or 0),
            'pop_unlocked': int(f.get('pop_unlocked') or 0),
            'time_started': int(f.get('time_started') or 0),
            'kills': int(kills.get(g['id']) or 0),
        }
        row['active_pct'] = round(100.0 * row['active'] / row['members']) if row['members'] else 0
        row['pop_pct'] = round(100.0 * row['pop_unlocked'] / row['accounts']) if row['accounts'] else 0
        a = ach.get(g['id']) or {}
        row['ach_points'] = a.get('points', 0)
        row['ach_max'] = a.get('max_points', 0)
        row['ach_pct'] = a.get('pct_points', 0)
        row['ach_earned'] = a.get('earned', 0)
        row['ach_total'] = a.get('total', 0)
        row['standing'] = _standing(row)
        out.append(row)

    out.sort(key=lambda x: (x['standing'], x['members']), reverse=True)
    for i, row in enumerate(out, 1):
        row['rank'] = i
    return out


# The directory is a server-wide computation -- every roster, every flag, and a
# scan of the raid-kill log -- and it was being run in full THREE times to draw
# one guild page (once for the directory itself, once inside get_guild just to
# pull a single summary row, and the achievement half of it again for the badges).
# Cached at 60s to match the site's other read caches.
_dir_cache = {}
_DIR_TTL = 60


def directory(include_empty=False):
    """list_guilds(), cached. Use this from request paths.

    Returns FRESH COPIES of the rows, never the cached objects themselves. The
    directory route decorates each row with per-viewer fields (`can_manage`, and
    the banner/blurb from the site's own profile table), and handing out the
    shared dicts would let one signed-in officer's "Yours" badge stick to the
    cache and show on the next visitor's page. The rows are flat scalars, so a
    shallow copy each is enough -- and 26 small dicts is nothing against the
    server-wide query it replaces.
    """
    import time
    now = time.time()
    hit = _dir_cache.get(bool(include_empty))
    if not hit or (now - hit[0]) >= _DIR_TTL:
        rows = list_guilds(include_empty=include_empty)
        _dir_cache[bool(include_empty)] = (now, rows)
        hit = (now, rows)
    return [dict(r) for r in hit[1]]


def get_guild(guild_id):
    """One guild + its full roster, or None. Roster is ordered by guild rank then
    level, so officers head the list."""
    with get_db(logic.engine) as s:
        g = s.execute(text("SELECT id, name, leader, motd, motd_setter, url, channel "
                           "FROM guilds WHERE id = :i"), {'i': int(guild_id)}).mappings().first()
        if not g:
            return None
        titles = {r[0]: r[1] for r in s.execute(text(
            "SELECT `rank`, title FROM guild_ranks WHERE guild_id = :i"), {'i': g['id']}).all()}
        members = s.execute(text(f"""
            SELECT cd.id, cd.name, cd.level, cd.aa_points_spent AS aa, cd.race,
                   cd.last_login, cd.anon, gm.`rank`, gm.public_note
            FROM guild_members gm
            JOIN character_data cd ON cd.id = gm.char_id AND {_VISIBLE}
            WHERE gm.guild_id = :i
            ORDER BY gm.`rank` ASC, cd.level DESC, cd.name ASC
        """), {'i': g['id']}).mappings().all()
        anchor = _activity_anchor(s)

    cutoff = anchor - ACTIVE_DAYS * 86400
    roster = []
    for m in members:
        roster.append({
            'id': m['id'], 'name': m['name'], 'level': m['level'], 'aa': m['aa'] or 0,
            'rank': m['rank'], 'title': titles.get(m['rank'], 'Member'),
            'is_officer': m['rank'] is not None and m['rank'] <= OFFICER_RANK,
            'note': (m['public_note'] or '').strip(),
            'active': bool(m['last_login'] and m['last_login'] >= cutoff),
            'last_login': m['last_login'],
        })

    summary = next((r for r in directory(include_empty=True) if r['id'] == g['id']), None)
    return {
        'id': g['id'], 'name': g['name'], 'leader_id': g['leader'],
        'motd': (g['motd'] or '').strip(), 'motd_setter': g['motd_setter'],
        'url': (g['url'] or '').strip(), 'channel': (g['channel'] or '').strip(),
        'titles': titles, 'roster': roster, 'summary': summary,
        'officers': [r for r in roster if r['is_officer']],
    }


def contributors(guild_id, limit=12):
    """Who inside ONE guild is actually carrying it.

    Four columns, chosen because each is a genuinely different kind of effort and
    none can be inflated by simply having a big roster:
        raid kills   -- from the accumulator (per character, any group size)
        AA           -- long-haul character investment
        discoveries  -- server-firsts, permanent and unfakeable
        days played  -- time_played, the plainest measure of showing up

    Ranked by the number of categories a character leads, then by kills. A guild
    of alts cannot fake this: every column is per CHARACTER, not per account.
    """
    with get_db(logic.engine) as s:
        rows = s.execute(text(f"""
            SELECT cd.id, cd.name, cd.level, cd.aa_points_spent, cd.time_played,
                   gm.`rank`, gr.title
            FROM guild_members gm
            JOIN character_data cd ON cd.id = gm.char_id AND {_VISIBLE}
            LEFT JOIN guild_ranks gr ON gr.guild_id = gm.guild_id AND gr.`rank` = gm.`rank`
            WHERE gm.guild_id = :g
        """), {'g': int(guild_id)}).all()
        if not rows:
            return {'rows': [], 'leaders': {}}
        names = [r[1] for r in rows]
        disc = {}
        if names:
            q = (text("SELECT char_name, COUNT(*) FROM discovered_items "
                      "WHERE char_name IN :n GROUP BY char_name")
                 .bindparams(bindparam('n', expanding=True)))
            disc = {n: int(c) for n, c in s.execute(q, {'n': names}).all()}

    try:
        import guild_achievements
        kills = guild_achievements.char_kill_counts([r[0] for r in rows])
    except Exception:
        kills = {}

    out = []
    for cid, name, level, aa, played, rank, title in rows:
        out.append({
            'id': cid, 'name': name, 'level': level,
            'rank': rank, 'title': title or 'Member',
            'aa': int(aa or 0),
            'days': round((played or 0) / 1440.0, 1),   # time_played is minutes
            'discoveries': disc.get(name, 0),
            'kills': (kills.get(cid) or {}).get('total', 0),
        })

    # Who tops each column -- shown as a crown on the row.
    leaders = {}
    for key in ('kills', 'aa', 'discoveries', 'days'):
        best = max(out, key=lambda r: r[key])
        if best[key] > 0:
            leaders[key] = best['id']
    for r in out:
        r['leads'] = [k for k, v in leaders.items() if v == r['id']]

    out.sort(key=lambda r: (len(r['leads']), r['kills'], r['aa']), reverse=True)
    return {'rows': out[:limit], 'leaders': leaders, 'total': len(out)}


# --------------------------------------------------------------------------- #
# Permissions -- DERIVED, never stored                                         #
# --------------------------------------------------------------------------- #
def manageable_guilds(discord_id):
    """{guild_id: rank} for every guild this Discord user may edit.

    Officer status must be derived from the game database on every call (never
    stored), which needs a Discord-to-game-account link. The public build has no
    account linking, so nobody can manage a guild page here; a private extension
    may replace ``guild_portal.manageable_guilds`` and ``guild_portal.officer_identity``.
    """
    return {}


def can_manage(discord_id, guild_id):
    return int(guild_id) in manageable_guilds(discord_id)


def officer_identity(discord_id, guild_id):
    """Who this Discord user IS inside one guild: {'name', 'rank', 'title'}, or None.
    Used to stamp guild page edits with a character name. Public default: None
    (see manageable_guilds)."""
    return None
