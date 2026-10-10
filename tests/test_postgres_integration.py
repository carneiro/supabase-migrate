"""Disposable local PostgreSQL only; never connects to Supabase projects."""
import os
from pathlib import Path
import tempfile
import unittest

from supamigrate.core import MigrationError
from supamigrate.database import Database, application_inventory


@unittest.skipUnless(os.environ.get('MIGRATE_TEST_POSTGRES')=='1','isolated PostgreSQL fixture not enabled')
class PostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Fixed loopback host, unique test DBs, no caller-provided remote DSN.
        cls.env=dict(os.environ,PGHOST='127.0.0.1',PGPORT='5432',PGUSER='postgres',
                     PGPASSWORD='migration-test-only',PGDATABASE='postgres',PGSSLMODE='disable')
        admin=Database(cls.env)
        cls.dbs=[]
        for name in ('migration_fixture_source','migration_fixture_target'):
            admin.sql('CREATE DATABASE '+name+';')
            cls.dbs.append(Database(dict(cls.env,PGDATABASE=name)))
        fixture='''
        CREATE SCHEMA auth;
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
            self.target.restore(folder,old,inv)
            actual=self.target.inventory()
            self.assertEqual(inv['checksums'],self.target.checksums(inv))
            self.assertEqual(self.target.last_row_counts['auth.users'],1)
            self.assertEqual(application_inventory(actual)['tables'],application_inventory(inv)['tables'])
            self.assertEqual(actual['policies'],inv['policies'])
            self.assertEqual(actual['application_policies'],inv['application_policies'])
            self.assertEqual(actual['sql_functions'],inv['sql_functions'])
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
