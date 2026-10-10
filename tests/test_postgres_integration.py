"""Disposable local PostgreSQL only; never connects to Supabase projects."""
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock,patch

from supamigrate.core import MigrationError
from supamigrate.database import Database, application_inventory
from supamigrate.services import restore_storage,verify_storage
from supamigrate.core import digest


@unittest.skipUnless(os.environ.get('MIGRATE_TEST_POSTGRES')=='1','isolated PostgreSQL fixture not enabled')
class PostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Only fixture commands/values exist here. Expose stderr in CI diagnostics,
        # while the production runner continues to suppress sensitive stderr.
        original_run=subprocess.run
        def fixture_run(*args,**kwargs):
            result=original_run(*args,**kwargs)
            if result.returncode and result.stderr:
                print(result.stderr.decode(errors='replace'),file=sys.stderr)
            return result
        diagnostic=patch('supamigrate.core.subprocess.run',side_effect=fixture_run)
        diagnostic.start()
        cls.addClassCleanup(diagnostic.stop)
        # Fixed loopback host, unique test DBs, no caller-provided remote DSN.
        cls.env=dict(os.environ,PGHOST='127.0.0.1',PGPORT='5432',PGUSER='postgres',
                     PGPASSWORD='migration-test-only',PGDATABASE='postgres',PGSSLMODE='disable')
        admin=Database(cls.env)
        admin.sql('CREATE ROLE supabase_auth_admin;')
        cls.dbs=[]
        for name in ('migration_fixture_source','migration_fixture_target'):
            admin.sql('CREATE DATABASE '+name+';')
            cls.dbs.append(Database(dict(cls.env,PGDATABASE=name)))
        fixture='''
        CREATE SCHEMA auth;
        ALTER DEFAULT PRIVILEGES FOR ROLE supabase_auth_admin IN SCHEMA auth GRANT SELECT ON TABLES TO postgres;
        CREATE TABLE auth.schema_migrations(version text PRIMARY KEY);
        INSERT INTO auth.schema_migrations VALUES ('20260101');
        CREATE TABLE auth.users(id uuid PRIMARY KEY,email text);
        ALTER TABLE auth.users ENABLE ROW LEVEL SECURITY;
        CREATE SCHEMA storage;
        CREATE TABLE storage.migrations(id integer PRIMARY KEY,name text,hash text,executed_at timestamptz DEFAULT now());
        INSERT INTO storage.migrations(id,name,hash) VALUES(1,'first','abc');
        CREATE TABLE storage.buckets(id text PRIMARY KEY,name text,public boolean,owner uuid,owner_id text);
        CREATE TABLE storage.objects(id uuid PRIMARY KEY,bucket_id text REFERENCES storage.buckets,name text,
          owner uuid REFERENCES auth.users,owner_id text,metadata jsonb,user_metadata jsonb);
        CREATE POLICY readable ON storage.objects FOR SELECT TO PUBLIC USING (true);
        ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;
        CREATE SCHEMA realtime;
        CREATE TABLE realtime.messages(id integer);
        CREATE PUBLICATION supabase_realtime_messages_publication FOR TABLE realtime.messages;
        CREATE EXTENSION unaccent WITH SCHEMA public;
        CREATE TABLE public.records(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          user_id uuid REFERENCES auth.users,body text);
        ALTER TABLE public.records ENABLE ROW LEVEL SECURITY;
        CREATE POLICY own_records ON public.records USING(true);
        CREATE FUNCTION public.normalize(text) RETURNS text LANGUAGE sql AS 'SELECT public.unaccent($1)';
        CREATE PUBLICATION supabase_realtime FOR TABLE public.records;
        CREATE SCHEMA supabase_migrations;
        CREATE TABLE supabase_migrations.schema_migrations(version text PRIMARY KEY);
        INSERT INTO supabase_migrations.schema_migrations VALUES('20261010010101');
        INSERT INTO auth.users VALUES('11111111-1111-1111-1111-111111111111','fixture@example.test');
        INSERT INTO public.records(user_id,body) VALUES('11111111-1111-1111-1111-111111111111','Olá');
        '''
        for db in cls.dbs:
            db.sql(fixture)
        cls.source=Database(cls.dbs[0].env,source=True)
        cls.target=cls.dbs[1]
        cls.target.sql("UPDATE public.records SET body='old destination'; CREATE TABLE realtime.target_only(id integer);")

    def test_01_source_readonly_is_enforced_by_postgres(self):
        with self.assertRaises(MigrationError):
            self.source.sql('DELETE FROM public.records;')
        self.assertEqual(self.source.sql('SELECT count(*) FROM public.records;'),'1')

    def test_02_shared_snapshot_does_not_capture_later_writes(self):
        with self.source.consistent_snapshot():
            self.assertEqual(self.source.sql('SELECT count(*) FROM public.records;'),'1')
            self.dbs[0].sql("INSERT INTO public.records(user_id,body) VALUES('11111111-1111-1111-1111-111111111111','later');")
            self.assertEqual(self.source.sql('SELECT count(*) FROM public.records;'),'1')
        self.assertEqual(self.source.sql('SELECT count(*) FROM public.records;'),'2')

    def test_03_dump_restore_auth_foreign_keys_policies_and_publications(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            old=self.target.inventory()
            with self.source.consistent_snapshot():
                inv=self.source.inventory()
                self.source.dump(folder,inv)
            self.assertNotIn('supabase_auth_admin',(folder/'managed.list').read_text())
            self.target.restore(folder,old,inv)
            actual=self.target.inventory()
            self.assertEqual(inv['checksums'],self.target.checksums(inv))
            self.assertEqual(self.target.last_row_counts['auth.users'],1)
            self.assertEqual(application_inventory(actual)['tables'],application_inventory(inv)['tables'])
            self.assertEqual(actual['policies'],inv['policies'])
            self.assertEqual(actual['application_policies'],inv['application_policies'])
            self.assertEqual(actual['sql_functions'],inv['sql_functions'])
            self.assertEqual(actual['managed_default_acl'],inv['managed_default_acl'])
            self.assertEqual(self.target.sql("SELECT count(*) FROM pg_publication WHERE pubname='supabase_realtime_messages_publication';"),'1')
            self.assertEqual(self.target.sql("SELECT to_regclass('realtime.target_only') IS NOT NULL;"),'t')
            self.assertEqual(self.target.sql("SELECT public.normalize('Olá');"),'Ola')

    def test_04_injected_restore_failure_rolls_back_schema_and_data(self):
        self.target.sql("UPDATE public.records SET body='preserve after rollback';")
        original=self.target.checksums(self.target.inventory())
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            old=self.target.inventory()
            with self.source.consistent_snapshot():
                inv=self.source.inventory()
                self.source.dump(folder,inv)
            with (folder/'database-post-data.sql').open('a') as f:
                f.write('\nSELECT definitely_missing_function();\n')
            with self.assertRaises(MigrationError):
                self.target.restore(folder,old,inv)
        self.assertEqual(original,self.target.checksums(self.target.inventory()))
        self.assertEqual(self.source.sql("SELECT count(*) FROM public.records WHERE body='preserve after rollback';"),'0')

    def test_05_storage_owners_timestamps_and_physical_version_with_real_trigger(self):
        self.target.sql('''
        ALTER TABLE storage.objects ADD COLUMN created_at timestamptz DEFAULT now();
        ALTER TABLE storage.objects ADD COLUMN updated_at timestamptz DEFAULT now();
        ALTER TABLE storage.objects ADD COLUMN version text DEFAULT 'target-physical-version';
        CREATE FUNCTION storage.touch() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN NEW.updated_at=now(); RETURN NEW; END $$;
        CREATE TRIGGER touch BEFORE UPDATE ON storage.objects FOR EACH ROW EXECUTE FUNCTION storage.touch();
        ''')
        obj={'id':'22222222-2222-2222-2222-222222222222','bucket_id':'b','name':'file.txt',
             'owner':'11111111-1111-1111-1111-111111111111','owner_id':'external-owner',
             'created_at':'2020-01-01T00:00:00+00:00','updated_at':'2020-02-01T00:00:00+00:00',
             'version':'source-physical-version','metadata':{'size':4,'mimetype':'text/plain'},'user_metadata':{'key':'value'}}
        inv={'buckets':[{'id':'b','name':'b','public':False,'owner':obj['owner'],'owner_id':'bucket-owner'}],'objects':[obj]}
        project=MagicMock()
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            (folder/'objects').mkdir()
            file=folder/'objects'/'0.bin'
            file.write_bytes(b'data')
            sha=digest(file)
            (folder/'objects.json').write_text(json.dumps([{'object':obj,'file':'0.bin','sha256':sha}]))
            def api(method,path,*args,**kwargs):
                if method=='POST' and path=='/bucket':
                    self.target.sql("INSERT INTO storage.buckets(id,name,public) VALUES('b','b',false);")
                elif method=='POST':
                    self.target.sql("INSERT INTO storage.objects(id,bucket_id,name) VALUES('33333333-3333-3333-3333-333333333333','b','file.txt');")
                elif method=='GET':
                    return sha
            project.storage.request.side_effect=api
            restore_storage(project,self.target,inv,folder)
            verify_storage(project,self.target,inv,folder)
            self.assertEqual(self.target.sql('SELECT version FROM storage.objects;'),'target-physical-version')
            self.target.sql("UPDATE storage.buckets SET owner_id='wrong-owner';")
            with self.assertRaises(MigrationError):
                verify_storage(project,self.target,inv,folder)
