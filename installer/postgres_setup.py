"""Resume only installer-owned external roles and databases; never adopt foreign data."""

import json
from pathlib import Path
import uuid

import preflight
import configuration_schema
import state_store


# Apply in the same session BEFORE any statement containing a role password.
# psql -f/ON_ERROR_STOP prevents later statements being sent if permission is denied.
PRIVATE_ROLE_LOGGING_SQL = """SET log_statement='none';
SET log_duration='off';
SET log_min_duration_statement='-1';
SET log_min_duration_sample='-1';
SET log_statement_stats='off';
SET log_parser_stats='off';
SET log_planner_stats='off';
SET log_executor_stats='off';
SET log_error_verbosity='terse';
SET log_min_error_statement='panic';
DO $privacy$ BEGIN
 IF EXISTS(SELECT FROM pg_settings WHERE name='pgaudit.log') THEN
  PERFORM set_config('pgaudit.log','none',false);
 END IF;
END $privacy$;
"""


def literal(value):
    # SQL goes through stdin, never argv, output, or a diagnostic string.
    return "'" + value.replace("'", "''") + "'"


def initialize(root, postgres):
    root = Path(root)
    values = preflight.settings(root)
    metadata = json.loads((root / 'installation.json').read_text())
    identity = {k: values[k] for k in ('POSTGRES_HOST', 'POSTGRES_PORT')}
    identity['server_name'] = metadata['domain']
    names = configuration_schema.owned_databases(values)
    scope = ','.join(literal(name) for name in names)
    checkpoint = root / 'config/external-postgres-owner.json'
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text())
        if saved.get('format') != 1 or saved.get('identity') != identity:
            raise RuntimeError(
                'External PostgreSQL initialization belongs to another cluster; retain the checkpoint and use the original settings'
            )
    else:
        saved = {'format': 1, 'identity': identity, 'owner': uuid.uuid4().hex}
    try:
        postgres(
            'psql',
            '-d',
            'postgres',
            '-X',
            '-v',
            'ON_ERROR_STOP=1',
            '-f',
            '-',
            stdin=PRIVATE_ROLE_LOGGING_SQL,
        )
    except Exception:
        raise RuntimeError(
            'External PostgreSQL connection or required session privacy controls failed before role creation. Check private credentials, TLS and connectivity first; if connection works, ask the dedicated cluster administrator for the SET privileges listed in storage.md. Keep existing data/checkpoints, then retry up or database-external/database-recover as appropriate. No role password statement sent by this precheck'
        ) from None
    if not checkpoint.exists():
        state_store.write_json(checkpoint, saved)
    marker = 'matrix-installer:' + saved['owner']
    # Inspect both names before creating anything: a foreign second role must
    # not leave an avoidable first role behind.
    query = f"""SELECT json_build_object('roles',
      (SELECT coalesce(json_agg(json_build_object('name',rolname,'marker',
        shobj_description(oid,'pg_authid'))),'[]') FROM pg_roles WHERE rolname IN ({scope})),
      'databases',(SELECT coalesce(json_agg(json_build_object('name',datname,
        'owner',pg_get_userbyid(datdba),'encoding',pg_encoding_to_char(encoding),
        'collate',datcollate,'ctype',datctype)),'[]') FROM pg_database WHERE datname IN ({scope})));"""

    def inspect():
        return json.loads(
            postgres('psql', '-d', 'postgres', '-X', '-At', '-v', 'ON_ERROR_STOP=1', '-c', query)
        )

    def validate(observed):
        roles = {row['name']: row for row in observed['roles']}
        for row in roles.values():
            if row['marker'] != marker:
                raise RuntimeError(
                    'External PostgreSQL contains a foreign Matrix role; use an empty dedicated cluster, never delete or reset it automatically'
                )
        for row in observed['databases']:
            if (
                row['name'] not in roles
                or row['owner'] != row['name']
                or row['encoding'] != 'UTF8'
                or row['collate'] != 'C'
                or row['ctype'] != 'C'
            ):
                raise RuntimeError(
                    'External PostgreSQL contains an incompatible or foreign Matrix database; keep its data and choose a dedicated cluster'
                )

    observed = inspect()
    validate(observed)
    for name in names:
        password = (root / 'secrets' / ('db_' + name)).read_text().strip()
        # Role creation and its ownership comment commit together. A lost
        # response can be retried without changing an existing password.
        delimiter = '$matrix_' + uuid.uuid4().hex + '$'
        while delimiter in password:
            delimiter = '$matrix_' + uuid.uuid4().hex + '$'
        sql = (
            PRIVATE_ROLE_LOGGING_SQL
            + f"""SET standard_conforming_strings=on;
BEGIN;
SET LOCAL lock_timeout='10s';
SET LOCAL statement_timeout='60s';
SELECT pg_advisory_xact_lock(738412609);
DO {delimiter} BEGIN
 IF EXISTS(SELECT FROM pg_roles WHERE rolname={literal(name)}) THEN
  IF (SELECT shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname={literal(name)}) IS DISTINCT FROM {literal(marker)} THEN
   RAISE EXCEPTION 'Foreign Matrix role';
  END IF;
 ELSE
  CREATE ROLE {name} LOGIN PASSWORD {literal(password)};
  COMMENT ON ROLE {name} IS {literal(marker)};
 END IF;
END {delimiter};
COMMIT;
"""
        )
        postgres('psql', '-d', 'postgres', '-X', '-v', 'ON_ERROR_STOP=1', '-f', '-', stdin=sql)
        observed = inspect()
        validate(observed)
        if not any(row['name'] == name for row in observed['databases']):
            postgres(
                'psql',
                '-d',
                'postgres',
                '-X',
                '-v',
                'ON_ERROR_STOP=1',
                '-c',
                'CREATE DATABASE '
                + name
                + ' OWNER '
                + name
                + " ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0",
            )
    validate(inspect())
    return {'installer_owned_roles': len(names), 'installer_owned_databases': len(names)}
