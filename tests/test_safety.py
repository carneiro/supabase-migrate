import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from supamigrate.core import HTTP, MigrationError, NoRedirect, Project, identifier, literal, validate_refs
from supamigrate.database import Database, connection_env, role_statements
from supamigrate.runner import blockers, check_manifest, estimate, main, replace, save_manifest
from supamigrate.services import (capture_storage, clear_storage, masked, object_path,
                                 restore_storage, writable_config)

SOURCE = 'a'*20
TARGET = 'b'*20


def inventory():
    return dict(version=170000, bytes=1024, extensions=[], roles=[], memberships=[],
                tables=[], schemas=[], policies=[], triggers=[], publications=[],
                publication_tables=[], subscriptions=[], buckets=[], objects=[], cron=[])


def services():
    return dict(functions=[],secret_names=[],configs={},sso={'items':[]},third_party=[])


class SafetyTests(unittest.TestCase):
    def test_ref_validation_blocks_equal_and_injection(self):
        for a,b in [(SOURCE,SOURCE),('../'+SOURCE,TARGET),('A'*20,TARGET),('',TARGET)]:
            with self.assertRaises(MigrationError):
                validate_refs(a,b)
        validate_refs(SOURCE,TARGET)

    def test_readonly_api_blocks_every_mutation(self):
        http = HTTP('https://example.test','secret',source=True)
        http.opener = MagicMock()
        for method in ['POST','PATCH','DELETE','PUT']:
            with self.assertRaises(MigrationError):
                http.request(method,'/anything')
        http.opener.open.assert_not_called()

    def test_credentials_not_in_command_and_connection_bound_to_ref(self):
        env = connection_env(f'postgresql://postgres:p%40ss@db.{SOURCE}.supabase.co:5432/postgres',SOURCE)
        self.assertEqual(env['PGPASSWORD'],'p@ss')
        self.assertEqual(env['PGSSLMODE'],'require')
        with self.assertRaises(MigrationError):
            connection_env(f'postgresql://postgres:secret@db.{TARGET}.supabase.co:5432/postgres',SOURCE)
        env = connection_env(f'postgresql://postgres.{SOURCE}:x@aws-0-us-east-1.pooler.supabase.com:5432/postgres',SOURCE)
        self.assertEqual(env['PGUSER'],'postgres.'+SOURCE)

    def test_transaction_pooler_tls_downgrade_and_override_rejected(self):
        base = f'postgresql://postgres.{SOURCE}:x@aws-0-us-east-1.pooler.supabase.com'
        urls = [base+':6543/postgres',base+':5432/postgres?sslmode=disable',base+':5432/postgres?host=evil.test',
                f'postgresql://postgres:x@db.{SOURCE}.supabase.co.evil.test/postgres']
        for url in urls:
            with self.assertRaises(MigrationError):
                connection_env(url,SOURCE)

    def test_source_pg_readonly_and_snapshot_import(self):
        db = Database({'PATH':os.environ['PATH']},source=True)
        db.snapshot = '00000001-00000002-1'
        with patch('supamigrate.database.run',return_value=b'[]\n') as execute:
            db.rows('SELECT 1')
        kwargs=execute.call_args.kwargs
        self.assertIn('default_transaction_read_only=on',kwargs['env']['PGOPTIONS'])
        self.assertIn(b'SET TRANSACTION SNAPSHOT',kwargs['input'])
        self.assertNotIn('00000001',str(execute.call_args.args))
        with self.assertRaises(MigrationError):
            db.restore('.',{}, {})

    def test_project_identity_checked_before_fetching_service_key(self):
        project=Project(SOURCE,'private',source=True)
        project.api=MagicMock()
        project.api.request.return_value={'id':TARGET,'status':'ACTIVE_HEALTHY'}
        with self.assertRaises(MigrationError):
            project.connect()
        self.assertEqual(project.api.request.call_count,1)

    def test_redirects_cannot_exfiltrate_bearer(self):
        with self.assertRaises(MigrationError):
            NoRedirect().redirect_request(None,None,302,'',{},'https://evil.test')

    def test_identifier_and_literal_quote_metacharacters(self):
        self.assertEqual(identifier('odd"name'),'"odd""name"')
        self.assertEqual(literal("o'reilly"),"'o''reilly'")

    def test_atomic_flag_fails_before_dependencies_or_credentials(self):
        with patch('supamigrate.runner.credentials') as creds,patch('supamigrate.runner.dependencies') as deps:
            with self.assertRaises(MigrationError):
                main(['--require-atomic'])
        creds.assert_not_called()
        deps.assert_not_called()

    def test_noninteractive_destructive_run_is_rejected(self):
        with patch('sys.stdin.isatty',return_value=False),patch('supamigrate.runner.credentials') as creds:
            with self.assertRaises(MigrationError):
                main(['--source',SOURCE,'--target',TARGET])
        creds.assert_not_called()

    def test_estimate_grows_with_volume(self):
        small=inventory()
        large=inventory()
        large['bytes']=100*1024**3
        self.assertGreater(estimate(large,small,services(),10)['min_minutes'],estimate(small,small,services(),10)['min_minutes'])

    def test_blockers_prevent_partial_unsupported_migration(self):
        a,b=inventory(),inventory()
        self.assertEqual(blockers(a,b,services(),services()),[])
        a['vault.secrets']=1
        a['subscriptions']=[{'name':'external'}]
        a['version']=150000
        self.assertGreaterEqual(len(blockers(a,b,services(),services())),3)

    def test_auth_storage_version_mismatch_blocked(self):
        a,b=inventory(),inventory()
        a['auth_versions']=[{'version':'one'}]
        b['auth_versions']=[{'version':'two'}]
        self.assertTrue(blockers(a,b,services(),services()))

    def test_masked_config_is_never_written(self):
        service=services()
        service['configs']={'config/auth':{'smtp_pass':'********'}}
        self.assertTrue(blockers(inventory(),inventory(),service,services()))
        self.assertTrue(masked({'a':'[REDACTED]'}))

    def test_readonly_config_fields_filtered_recursively(self):
        schema={'properties':{'features':{'properties':{'enabled':{}}}}}
        self.assertEqual(writable_config({'features':{'enabled':True,'internal':1},'id':'x'},schema),{'features':{'enabled':True}})

    def test_backup_detects_tampering_and_path_escape(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            (folder/'data').write_bytes(b'good')
            save_manifest(folder)
            check_manifest(folder)
            (folder/'data').write_bytes(b'bad')
            with self.assertRaises(MigrationError):
                check_manifest(folder)
            (folder/'manifest.json').write_text(json.dumps({'../outside':'a'}))
            with self.assertRaises(MigrationError):
                check_manifest(folder)

    def test_storage_paths_are_urls_not_local_filenames(self):
        self.assertEqual(object_path({'bucket_id':'my bucket','name':'folder/a?#.png'}),
                         '/object/my%20bucket/folder/a%3F%23.png')

    def test_storage_capture_detects_size_drift_before_mutation(self):
        project=MagicMock()
        inv=inventory()
        inv['objects']=[{'bucket_id':'b','name':'a','metadata':{'size':5}}]
        def request(method,path,**kwargs):
            Path(kwargs['download']).write_bytes(b'new')
            return 'sha'
        project.storage.request.side_effect=request
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(MigrationError):
                capture_storage(project,inv,Path(d))
        self.assertTrue(all(c.args[0]=='GET' for c in project.storage.request.call_args_list))

    def test_storage_cleanup_batches_without_prefix_recursion(self):
        project=MagicMock()
        inv=inventory()
        inv['buckets']=[{'id':'b'}]
        inv['objects']=[{'bucket_id':'b','name':str(i)} for i in range(201)]
        clear_storage(project,inv)
        calls=project.storage.request.call_args_list
        self.assertEqual([len(c.args[2]['prefixes']) for c in calls[:-1]],[100,100,1])
        self.assertEqual(calls[-1].args,('DELETE','/bucket/b'))

    def test_storage_owner_id_preserved_and_physical_version_not_overwritten(self):
        project,db=MagicMock(),MagicMock()
        inv=inventory()
        obj={'id':'uuid','owner':'owner-uuid','owner_id':'external-id','name':'a','bucket_id':'b','version':'old-physical-version','metadata':{}}
        inv['objects']=[obj]
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            (folder/'objects').mkdir()
            (folder/'objects'/'0.bin').write_bytes(b'data')
            from supamigrate.core import digest
            (folder/'objects.json').write_text(json.dumps([{'object':obj,'file':'0.bin','sha256':digest(folder/'objects'/'0.bin')}]))
            restore_storage(project,db,inv,folder)
        sql=db.sql.call_args.args[0]
        self.assertIn('"owner_id"=\'external-id\'',sql)
        self.assertIn('"owner"=\'owner-uuid\'',sql)
        self.assertNotIn('old-physical-version',sql)

    def test_failure_stops_before_later_services(self):
        project,db=MagicMock(),MagicMock()
        db.restore.side_effect=MigrationError('restore failure')
        phases=[]
        with patch('supamigrate.runner.clear_functions'),patch('supamigrate.runner.clear_storage'),patch('supamigrate.runner.restore_configs') as config:
            with self.assertRaises(MigrationError):
                replace(project,db,inventory(),inventory(),services(),services(),Path('.'),'token',phases.append)
        config.assert_not_called()
        self.assertEqual(phases, ['clearing_functions','clearing_storage','restoring_database'])

    def test_database_restore_is_single_transaction_with_stop_on_error(self):
        inv=inventory()
        inv['schemas']=[{'name':'public'},{'name':'auth'},{'name':'storage'}]
        with tempfile.TemporaryDirectory() as d,patch('supamigrate.database.run') as execute:
            Database({}).restore(Path(d),inv,inv)
            sql=(Path(d)/'cleanup.sql').read_text()
        args=execute.call_args.args[0]
        self.assertIn('--single-transaction',args)
        self.assertIn('ON_ERROR_STOP=1',args)
        self.assertIn('DROP SCHEMA IF EXISTS "public" CASCADE',sql)
        self.assertNotIn('DROP SCHEMA IF EXISTS "auth"',sql)
        self.assertNotIn('DROP SCHEMA IF EXISTS "storage"',sql)


if __name__ == '__main__':
    unittest.main()
