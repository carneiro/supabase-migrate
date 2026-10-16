from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
from urllib.parse import parse_qs, unquote, urlparse

from .core import MigrationError, identifier as qi, literal as ql, run, write_json

# Platform schema DDL must not be replaced. Application tables inside these schemas
# are checked separately; unsupported extension state blocks destructive work.
MANAGED = {'auth', 'storage', 'extensions', 'realtime', '_realtime', '_analytics',
           '_supavisor', 'supabase_functions', 'cron', 'graphql', 'graphql_public',
           'net', 'pgsodium', 'pgsodium_masks', 'vault', 'pgbouncer', 'pgtle',
           'repack', 'dbdev', 'etl'}
BUILTIN_ROLES = {'postgres', 'anon', 'authenticated', 'service_role', 'authenticator',
                 'dashboard_user', 'cli_login_postgres', 'pgbouncer'}


def custom_role(name):
    return not (name in BUILTIN_ROLES or name.startswith(('pg_', 'supabase_')))


def application_inventory(inv):
    """Project-owned scope, excluding continuously managed Realtime internals."""
    result = dict(inv)
    result['tables'] = [t for t in inv.get('tables',[]) if t['schema'] not in MANAGED or t['schema'] in ('auth','storage')]
    result['schemas'] = [s for s in inv.get('schemas',[]) if s['name'] not in MANAGED]
    result['publications'] = [p for p in inv.get('publications',[]) if not p['name'].startswith('supabase_realtime_')]
    result['publication_tables'] = [p for p in inv.get('publication_tables',[]) if not p['pubname'].startswith('supabase_realtime_')]
    return result


def portable_toc(toc):
    lines = []
    for line in toc.splitlines():
        if re.match(r'^\d+; \d+ \d+ (EVENT TRIGGER|DEFAULT ACL) ', line) and line.endswith(' supabase_admin'):
            continue
        if re.match(r'^\d+; \d+ \d+ PUBLICATION(?: TABLE| TABLES IN SCHEMA)? ', line) and 'supabase_realtime_' in line:
            continue
        lines.append(line)
    return '\n'.join(lines) + '\n'


def connection_env(url, ref):
    u = urlparse(url)
    query = parse_qs(u.query)
    if u.scheme not in ('postgres', 'postgresql') or not u.password or not u.hostname:
        raise MigrationError('Enter a PostgreSQL URL with a password from the Connect panel.')
    host, user = u.hostname, unquote(u.username or '')
    direct = host == f'db.{ref}.supabase.co' and user == 'postgres'
    pooler = (re.fullmatch(r'aws-[0-9]+-[a-z0-9-]+\.pooler\.supabase\.com', host)
              and user == 'postgres.' + ref)
    if not (direct or pooler) or (u.port or 5432) != 5432 or u.path != '/postgres':
        raise MigrationError('URL does not match the project. Use a direct connection or Session pooler on port 5432.')
    if set(query) - {'sslmode'} or query.get('sslmode', ['require'])[0] not in ('require', 'verify-full'):
        raise MigrationError('URL parameters rejected; TLS is required.')
    env = {k: v for k, v in os.environ.items() if not k.startswith('PG')}
    env.update(PGHOST=host, PGPORT='5432', PGUSER=user, PGPASSWORD=unquote(u.password),
               PGDATABASE='postgres', PGSSLMODE=query.get('sslmode', ['require'])[0],
               PGCONNECT_TIMEOUT='20', PGAPPNAME='supabase-migrate')
    return env


class Database:
    def __init__(self, env, *, source=False):
        self.env = dict(env)
        self.source = source
        self.snapshot = None
        self.env['PGOPTIONS'] = '-c standard_conforming_strings=on -c timezone=UTC -c extra_float_digits=3 -c lock_timeout=3000 -c statement_timeout=120000'
        if source:
            self.env['PGOPTIONS'] += ' -c default_transaction_read_only=on'

    def sql(self, sql):
        prefix = ''
        if self.snapshot:
            prefix = 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET TRANSACTION SNAPSHOT ' + ql(self.snapshot) + ';\n'
        raw = run(['psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1'], env=self.env,
                  input=(prefix + sql + ('\nCOMMIT;' if prefix else '')).encode())
        return raw.decode().strip()

    def rows(self, query):
        return json.loads(self.sql('SELECT coalesce(json_agg(x),\'[]\'::json) FROM (' + query + ') x;'))

    @contextmanager
    def consistent_snapshot(self):
        env = dict(self.env)
        env['PGOPTIONS'] = self.env['PGOPTIONS'] + ' -c default_transaction_read_only=on'
        p = subprocess.Popen(['psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1'],
                             env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
        try:
            p.stdin.write(b'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SELECT pg_export_snapshot();\n')
            p.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(p.stdout, selectors.EVENT_READ)
                if not selector.select(30):
                    raise MigrationError('Timed out opening a PostgreSQL snapshot.')
            snapshot = p.stdout.readline().decode().strip()
            if not re.fullmatch(r'[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9]+', snapshot):
                raise MigrationError('Could not export a PostgreSQL snapshot.')
            self.snapshot = snapshot
            yield
            if p.poll() is not None:
                raise MigrationError('Snapshot session ended during capture.')
        finally:
            self.snapshot = None
            if p.poll() is None:
                p.stdin.close()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
            p.stdout.close()

    def inventory(self):
        data = {}
        data['version'] = int(self.sql('SHOW server_version_num;'))
        data['bytes'] = int(self.sql('SELECT pg_database_size(current_database());'))
        data['schemas'] = self.rows("SELECT nspname AS name,pg_get_userbyid(nspowner) AS owner FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' ORDER BY 1")
        data['extensions'] = self.rows('SELECT extname AS name, extversion AS version FROM pg_extension ORDER BY 1')
        data['roles'] = self.rows("SELECT rolname AS name, rolsuper, rolcreatedb, rolcreaterole, rolinherit, rolcanlogin, rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil::text, rolconfig FROM pg_roles ORDER BY 1")
        data['memberships'] = self.rows('SELECT r.rolname AS role, m.rolname AS member, a.admin_option FROM pg_auth_members a JOIN pg_roles r ON r.oid=a.roleid JOIN pg_roles m ON m.oid=a.member ORDER BY 1,2')
        data['tables'] = self.rows("""SELECT n.nspname AS schema, c.relname AS name, c.relkind,
            pg_get_userbyid(c.relowner) AS owner, c.relrowsecurity AS rls, c.relforcerowsecurity AS force_rls,
            (SELECT json_agg(json_build_object('name',a.attname,'type',format_type(a.atttypid,a.atttypmod),
               'notnull',a.attnotnull,'generated',a.attgenerated) ORDER BY a.attnum)
             FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped) AS columns
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE c.relkind IN ('r','p') AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' ORDER BY 1,2""")
        data['policies'] = self.rows("SELECT schemaname, tablename, policyname, permissive, roles, cmd, qual, with_check FROM pg_policies WHERE schemaname IN ('auth','storage') ORDER BY 1,2,3")
        data['triggers'] = self.rows("""SELECT n.nspname AS schema,c.relname AS table,t.tgname AS name,
            pg_get_triggerdef(t.oid) AS definition FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE NOT t.tgisinternal
            AND n.nspname IN ('auth','storage') ORDER BY 1,2,3""")
        data['publications'] = self.rows("SELECT pubname AS name, puballtables, pubinsert, pubupdate, pubdelete, pubtruncate FROM pg_publication ORDER BY 1")
        data['publication_tables'] = self.rows('SELECT pubname,schemaname,tablename,attnames,rowfilter FROM pg_publication_tables ORDER BY 1,2,3')
        data['auth_versions'] = self.rows('SELECT version FROM auth.schema_migrations ORDER BY version')
        data['storage_versions'] = self.rows('SELECT id,name,hash FROM storage.migrations ORDER BY id')
        data['foreign_servers'] = self.rows('SELECT srvname AS name FROM pg_foreign_server')
        data['large_objects'] = int(self.sql('SELECT count(*) FROM pg_largeobject_metadata;'))
        data['custom_managed_tables'] = self.rows("""SELECT n.nspname AS schema,c.relname AS name FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner
            WHERE n.nspname IN ('auth','storage') AND c.relkind IN ('r','p')
            AND r.rolname NOT LIKE 'supabase_%'""")
        data['event_triggers'] = self.rows("SELECT evtname AS name FROM pg_event_trigger e JOIN pg_roles r ON r.oid=e.evtowner WHERE r.rolname NOT LIKE 'supabase_%'")
        data['subscriptions'] = self.rows('SELECT subname AS name FROM pg_subscription')
        data['buckets'] = self.rows('SELECT * FROM storage.buckets ORDER BY id')
        data['objects'] = self.rows('SELECT * FROM storage.objects ORDER BY bucket_id,name')
        for table in ('storage.buckets_vectors', 'storage.vector_indexes', 'storage.buckets_analytics',
                      'storage.s3_multipart_uploads', 'vault.secrets', 'pgsodium.key', 'cron.job'):
            if self.sql('SELECT to_regclass(' + ql(table) + ') IS NOT NULL;') == 't':
                data[table] = int(self.sql('SELECT count(*) FROM ' + '.'.join(map(qi, table.split('.'))) + ';'))
        data['cron'] = self.rows('SELECT * FROM cron.job ORDER BY jobid') if data.get('cron.job') else []
        data['managed_functions'] = self.rows("""SELECT n.nspname AS schema,p.proname AS name FROM pg_proc p
            JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_roles r ON r.oid=p.proowner
            WHERE n.nspname IN ('auth','storage') AND r.rolname NOT LIKE 'supabase_%'
            AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass
            AND d.objid=p.oid AND d.deptype='e') ORDER BY 1,2""")
        data['managed_default_acl'] = self.rows("""SELECT r.rolname AS role,n.nspname AS schema,
            d.defaclobjtype AS kind,d.defaclacl::text AS acl FROM pg_default_acl d
            JOIN pg_roles r ON r.oid=d.defaclrole JOIN pg_namespace n ON n.oid=d.defaclnamespace
            WHERE n.nspname IN ('auth','storage') AND r.rolname LIKE 'supabase_%' ORDER BY 1,2,3""")
        app_filter = "n.nspname NOT IN (" + ','.join(ql(x) for x in sorted(MANAGED)) + ") AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'"
        data['sql_functions'] = self.rows("""SELECT n.nspname AS schema,p.proname AS name,
            pg_get_function_identity_arguments(p.oid) AS identity,md5(pg_get_functiondef(p.oid)) AS definition_hash,
            pg_get_userbyid(p.proowner) AS owner,p.prosecdef AS security_definer,
            p.proacl::text AS acl FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE p.prokind IN ('f','p') AND """ + app_filter + ' ORDER BY 1,2,3')
        data['application_policies'] = self.rows("""SELECT p.schemaname AS schema,p.tablename AS table,p.policyname AS name,
            p.permissive,p.roles,p.cmd,md5(coalesce(p.qual,'')||'|'||coalesce(p.with_check,'')) AS expression_hash
            FROM pg_policies p JOIN pg_namespace n ON n.nspname=p.schemaname WHERE """ + app_filter + ' ORDER BY 1,2,3')
        data['application_triggers'] = self.rows("""SELECT n.nspname AS schema,c.relname AS table,t.tgname AS name,
            md5(pg_get_triggerdef(t.oid)) AS definition_hash,t.tgenabled FROM pg_trigger t
            JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE NOT t.tgisinternal AND """ + app_filter + ' ORDER BY 1,2,3')
        data['views'] = self.rows("""SELECT n.nspname AS schema,c.relname AS name,c.relkind,
            md5(pg_get_viewdef(c.oid)) AS definition_hash,pg_get_userbyid(c.relowner) AS owner,c.relacl::text AS acl
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE c.relkind IN ('v','m') AND """ + app_filter + ' ORDER BY 1,2')
        data['indexes'] = self.rows("""SELECT n.nspname AS schema,c.relname AS table,i.relname AS name,
            md5(pg_get_indexdef(i.oid)) AS definition_hash FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid
            JOIN pg_class c ON c.oid=x.indrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE """ + app_filter + ' ORDER BY 1,2,3')
        data['constraints'] = self.rows("""SELECT n.nspname AS schema,coalesce(c.relname,'') AS table,k.conname AS name,
            md5(pg_get_constraintdef(k.oid)) AS definition_hash FROM pg_constraint k
            JOIN pg_namespace n ON n.oid=k.connamespace LEFT JOIN pg_class c ON c.oid=k.conrelid
            WHERE """ + app_filter + ' ORDER BY 1,2,3')
        data['sequences'] = self.rows("""SELECT s.schemaname AS schema,s.sequencename AS name,s.sequenceowner,
            s.data_type::text,s.start_value,s.min_value,s.max_value,s.increment_by,s.cycle,s.cache_size
            FROM pg_sequences s JOIN pg_namespace n ON n.nspname=s.schemaname WHERE """ + app_filter + ' ORDER BY 1,2')
        return data

    def dump(self, folder, inventory):
        folder = Path(folder)
        main = ['pg_dump', '--format=custom', '--quote-all-identifiers', '--no-subscriptions']
        for schema in sorted(MANAGED):
            main += ['--exclude-schema=' + schema]
        # Existing platform extensions are checked for compatible versions and retained.
        if self.snapshot:
            main += ['--snapshot=' + self.snapshot]
        run(main + ['--file=' + str(folder / 'database.dump')], env=self.env)
        args = ['pg_dump', '--format=custom', '--data-only', '--schema=auth',
                '--exclude-table-data=auth.schema_migrations', '--quote-all-identifiers']
        if self.snapshot:
            args += ['--snapshot=' + self.snapshot]
        run(args + ['--file=' + str(folder / 'auth.dump')], env=self.env)
        managed = ['pg_dump', '--format=custom', '--schema-only', '--schema=auth', '--schema=storage']
        if self.snapshot:
            managed += ['--snapshot=' + self.snapshot]
        run(managed + ['--file=' + str(folder/'managed.dump')], env=self.env)
        toc = run(['pg_restore','--list',str(folder/'managed.dump')], env=self.env).decode()
        selected = [line for line in toc.splitlines() if re.match(r'^\d+; \d+ \d+ (ACL|DEFAULT ACL) ', line)
                    and not (' DEFAULT ACL ' in line and line.split()[-1].startswith('supabase_'))]
        (folder/'managed.list').write_text('\n'.join(selected)+'\n')
        run(['pg_restore','--use-list='+str(folder/'managed.list'),'--file='+str(folder/'managed.sql'),
             str(folder/'managed.dump')], env=self.env)
        # Platform event triggers/default privileges are maintained by Supabase;
        # replaying superuser-owned DDL is neither allowed nor portable.
        toc = run(['pg_restore','--list',str(folder/'database.dump')], env=self.env).decode()
        (folder/'database.list').write_text(portable_toc(toc))
        for section in ('pre-data','data','post-data'):
            run(['pg_restore','--use-list='+str(folder/'database.list'),'--section='+section,
                 '--file='+str(folder/('database-'+section+'.sql')),'--no-comments',
                 str(folder/'database.dump')],env=self.env)
        # pg_dump omits CREATE SCHEMA public when it matches the initdb schema.
        # Our transactional cleanup drops it, so supply the initial definition
        # before extension/table DDL. Any dumped ACL changes are replayed later.
        if not re.search(r'^\d+; \d+ \d+ SCHEMA - public ',toc,re.M):
            public=next((s for s in inventory['schemas'] if s['name']=='public'),None)
            if public:
                path=folder/'database-pre-data.sql'
                privileges='USAGE' if inventory['version'] >= 150000 else 'USAGE, CREATE'
                prefix='CREATE SCHEMA "public" AUTHORIZATION '+qi(public['owner'])+';\n'
                prefix+='GRANT '+privileges+' ON SCHEMA "public" TO PUBLIC;\n'
                path.write_text(prefix+path.read_text(),encoding='utf8')
        run(['pg_restore','--file='+str(folder/'auth.sql'),'--no-comments',str(folder/'auth.dump')],env=self.env)
        inventory['checksums'] = self.checksums(inventory)
        inventory['row_counts'] = dict(self.last_row_counts)
        write_json(folder / 'database.json', inventory)

    def checksums(self, inventory):
        hashes = {}
        counts = {}
        for t in inventory['tables']:
            schema, name = t['schema'], t['name']
            if t['relkind'] != 'r' or (schema in MANAGED and schema != 'auth') or (schema == 'auth' and name == 'schema_migrations'):
                continue
            prefix = ''
            if self.snapshot:
                prefix = 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET TRANSACTION SNAPSHOT ' + ql(self.snapshot) + ';'
            query = prefix + 'COPY (SELECT md5(row_to_json(t)::text) AS h FROM ONLY ' + qi(schema) + '.' + qi(name) + ' t ORDER BY h) TO STDOUT;'
            p = subprocess.Popen(['psql','-X','-qAt','-v','ON_ERROR_STOP=1'], env=self.env,
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            p.stdin.write(query.encode())
            p.stdin.close()
            h = hashlib.sha256()
            count = 0
            for chunk in iter(lambda: p.stdout.read(1024 * 1024), b''):
                h.update(chunk)
                count += chunk.count(b'\n')
            p.stdout.close()
            if p.wait():
                raise MigrationError('Failed to calculate table checksum.')
            hashes[schema + '.' + name] = h.hexdigest()
            counts[schema + '.' + name] = count
        self.last_row_counts = counts
        return hashes

    def restore(self, folder, previous, incoming):
        if self.source:
            raise MigrationError('Restoring to the source is blocked.')
        folder = Path(folder)
        cleanup = ['SET LOCAL lock_timeout = \'15s\';', 'SET LOCAL session_replication_role = replica;']
        for p in previous['policies']:
            cleanup.append(f'DROP POLICY IF EXISTS {qi(p["policyname"])} ON {qi(p["schemaname"])}.{qi(p["tablename"])};')
        for t in previous['triggers']:
            cleanup.append(f'DROP TRIGGER IF EXISTS {qi(t["name"])} ON {qi(t["schema"])}.{qi(t["table"])};')
        for p in application_inventory(previous)['publications']:
            cleanup.append('DROP PUBLICATION ' + qi(p['name']) + ';')
        for schema in previous['schemas']:
            if schema['name'] not in MANAGED:
                cleanup.append('DROP SCHEMA IF EXISTS ' + qi(schema['name']) + ' CASCADE;')
        auth = [qi(t['schema']) + '.' + qi(t['name']) for t in previous['tables']
                if t['schema'] == 'auth' and t['name'] != 'schema_migrations' and t['relkind'] == 'r']
        if auth:
            # Older Storage schemas may keep FKs to auth.users even after the
            # Storage API emptied the buckets. TRUNCATE rejects those structural
            # references. DELETE under replica mode avoids CASCADE into managed
            # tables; Auth sequence values are restored by the data-only dump.
            cleanup += ['DELETE FROM ONLY ' + table + ';' for table in auth]
        if previous.get('cron.job'):
            cleanup.append('SELECT cron.unschedule(jobid) FROM cron.job;')
        incoming_roles = {r['name'] for r in incoming['roles']}
        for role in previous['roles']:
            if custom_role(role['name']) and role['name'] not in incoming_roles:
                # Recovery removes roles introduced by this migration after their
                # application schemas/publications have already been removed.
                cleanup += ['DROP OWNED BY ' + qi(role['name']) + ';', 'DROP ROLE ' + qi(role['name']) + ';']
        cleanup += role_statements(incoming, previous)
        (folder / 'cleanup.sql').write_text('\n'.join(cleanup), encoding='utf8')
        finish = []
        for t in incoming['tables']:
            if t['schema'] in ('auth','storage'):
                table = qi(t['schema']) + '.' + qi(t['name'])
                finish.append('ALTER TABLE ' + table + (' ENABLE' if t['rls'] else ' DISABLE') + ' ROW LEVEL SECURITY;')
                finish.append('ALTER TABLE ' + table + (' FORCE' if t['force_rls'] else ' NO FORCE') + ' ROW LEVEL SECURITY;')
        for p in incoming['policies']:
            statement = f'CREATE POLICY {qi(p["policyname"])} ON {qi(p["schemaname"])}.{qi(p["tablename"])} AS {p["permissive"]} FOR {p["cmd"]} TO ' + ','.join('PUBLIC' if r=='public' else qi(r) for r in p['roles'])
            if p['qual']:
                statement += ' USING (' + p['qual'] + ')'
            if p['with_check']:
                statement += ' WITH CHECK (' + p['with_check'] + ')'
            finish.append(statement + ';')
        finish += [t['definition'] + ';' for t in incoming['triggers']]
        finish.append("NOTIFY pgrst, 'reload schema';")
        (folder / 'finish.sql').write_text('\n'.join(finish), encoding='utf8')
        run(['psql','-X','-q','--single-transaction','-v','ON_ERROR_STOP=1',
             '-f',str(folder/'cleanup.sql'),'-f',str(folder/'database-pre-data.sql'),
             '-f',str(folder/'auth.sql'),'-f',str(folder/'database-data.sql'),
             '-f',str(folder/'database-post-data.sql'),'-f',str(folder/'managed.sql'),'-f',str(folder/'finish.sql')], env=self.env)


def role_statements(incoming, previous):
    existing = {r['name']: r for r in previous['roles']}
    result = []
    for r in incoming['roles']:
        if not custom_role(r['name']):
            continue
        if r['rolsuper'] or r['rolreplication'] or r['rolcreaterole']:
            raise MigrationError('Privileged role cannot be migrated automatically: ' + r['name'])
        # Avoid changing passwords/attributes of pre-existing roles.
        if r['name'] in existing:
            if r != existing[r['name']]:
                raise MigrationError('Existing role has different attributes: ' + r['name'])
            continue
        attrs = ['LOGIN' if r['rolcanlogin'] else 'NOLOGIN',
                 'INHERIT' if r['rolinherit'] else 'NOINHERIT',
                 'CREATEDB' if r['rolcreatedb'] else 'NOCREATEDB',
                 'BYPASSRLS' if r['rolbypassrls'] else 'NOBYPASSRLS']
        result.append('CREATE ROLE ' + qi(r['name']) + ' ' + ' '.join(attrs) +
                      ' CONNECTION LIMIT ' + str(r['rolconnlimit']) + ';')
        if r['rolvaliduntil']:
            result.append('ALTER ROLE ' + qi(r['name']) + ' VALID UNTIL ' + ql(r['rolvaliduntil']) + ';')
        for config in r['rolconfig'] or []:
            key, value = config.split('=', 1)
            result.append('ALTER ROLE ' + qi(r['name']) + ' SET ' + qi(key) + ' TO ' + ql(value) + ';')
    for m in incoming['memberships']:
        if custom_role(m['member']) or custom_role(m['role']):
            result.append('GRANT ' + qi(m['role']) + ' TO ' + qi(m['member']) +
                          (' WITH ADMIN OPTION' if m['admin_option'] else '') + ';')
    return result
