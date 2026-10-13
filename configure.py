"""Write a blank configuration file.

Usage: python configure.py [output-file]      (default: configuration.ini)

Every secret is left empty. Fill in at least the [database] section before
starting the site. Environment variables (DB_HOST, DB_USER, FLASK_SECRET_KEY,
DISCORD_CLIENT_ID, ...) override the matching values at runtime.
"""
import os
import sys
import configparser

here = os.path.dirname(os.path.abspath(__file__))
out_name = sys.argv[1] if len(sys.argv) > 1 else 'configuration.ini'
out_path = out_name if os.path.isabs(out_name) else os.path.join(here, out_name)
if os.path.exists(out_path):
    print('%s already exists, please modify it instead' % out_name)
    sys.exit(0)

site_config = configparser.ConfigParser()

site_config['DEFAULT'] = {'site_fqdn': 'localhost',
                          'site_type': 'Development',
                          'debug': True,
                          'site_version': '0.0.0.0'}

# expansion: fallback content expansion if no server rule is found.
# expansion_rule: optional rule_values row to read the live expansion from
#   (build_gear_scores.py otherwise tries Expansion:CurrentExpansion then
#   Custom:AdvLootExpansionLootLimit). Leave blank to use the defaults.
site_config['triune'] = {'expansion': 5, 'expansion_rule': ''}

# The EQEmu game/content database (PEQ schema). Read-only use.
site_config['database'] = {'driver': 'mysql+pymysql',
                           'user': '',
                           'password': '',
                           'database': 'peq',
                           'host': '127.0.0.1',
                           'port': '3306'}

# The site's own small store (weights sets, guild page profiles, user preferences).
site_config['local_database'] = {'connection': 'sqlite:///local_db.db'}

# Leaderboard database (kept separate from the content DB). An SQLAlchemy URL such as
# mysql+pymysql://user:password@host:3306/eqdb_leaderboard. Create the tables with
# python create_leaderboard_db.py.
site_config['leaderboard_database'] = {'connection': ''}

# staff_discord_ids: numeric Discord ids (comma-separated) allowed into staff-only pages.
# ingest_token: shared secret for an encounter-ingest extension; empty = ingest refused.
site_config['leaderboard'] = {'staff_discord_ids': '',
                              'ingest_token': ''}

# Forum moderators (numeric Discord ids, comma-separated). Empty = the staff list.
site_config['forums'] = {'moderator_ids': ''}

site_config['path'] = {'app_log': 'logs/eqdb.log',
                       'flask_log': 'logs/flask.log'}

# Discord OAuth login. Leave blank to run without sign-in.
site_config['discord'] = {'client_id': '',
                          'client_secret': ''}

# Flask session key. Blank = a random key per process (sessions reset on restart).
# The FLASK_SECRET_KEY environment variable wins over this value.
site_config['flask'] = {'secret_key': ''}

# Public, non-secret values shown on pages.
site_config['site'] = {'name': 'EQDB',
                       'base_url': '',
                       'patcher_url': '',
                       'discord_invite': '',
                       'support_url': '',
                       'partner_feed_enabled': 'false',
                       'partner_feed_path': ''}

# Login-server web API for the player count. If api_url or api_token is empty, no call
# is made and /api/player-count reports "unavailable".
site_config['loginserver'] = {'api_url': '',
                              'api_token': '',
                              'cors_origin': '*'}

# Comma-separated peer addresses allowed to call /internal/* endpoints that an
# extension may add. Empty = nobody.
site_config['internal'] = {'allow_ips': ''}

with open(out_path, 'w') as fh:
    site_config.write(fh)
print('Wrote %s' % out_name)
