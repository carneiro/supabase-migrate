import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from supamigrate.database import application_inventory, portable_toc
from supamigrate.reporting import build_report, finish_report, observation, write_report
from test_safety import inventory, services


def observed(checksum='abc'):
    inv=inventory()
    inv['checksums']={'public.records':checksum,'auth.users':'users'}
    inv['row_counts']={'public.records':2,'auth.users':1}
    inv['tables']=[{'schema':'public','name':'records','columns':[]}, {'schema':'auth','name':'users','columns':[]}]
    return observation(inv,services())


def context():
    return {'state':{'source':'a'*20,'target':'b'*20,'phase':'completed'},
            'observations':{'source_snapshot':observed(),'target_before':observed('old'),
                            'target_verified':observed(),'source_current':observed(),'target_current':observed()}}


class ReportingTests(unittest.TestCase):
    def test_live_writes_dont_turn_verified_snapshot_into_failed_migration(self):
        ctx=context()
        ctx['observations']['source_current']=observed('new-live-write')
        report=build_report(ctx)
        self.assertTrue(report['migration_completed'])
        self.assertEqual(report['features']['database']['snapshot_diff'],'equal')
        self.assertEqual(report['features']['database']['current_diff'],'different')
        diff=report['diffs']['source_snapshot_vs_current_source']['database']
        self.assertEqual(diff['changed'],['data:public.records'])
        self.assertEqual(report['observations']['source_snapshot']['counts']['row_counts']['public.records'],2)

    def test_report_never_contains_configuration_values_sql_rows_or_owners(self):
        ctx=context()
        for o in ctx['observations'].values():
            o['services']['configs']={'config/auth':{'smtp_pass':'SECRET_DO_NOT_LEAK','smtp_user':'person@example.test'}}
            o['inventory']['objects']=[{'id':'object-uuid','bucket_id':'b','name':'PRIVATE_PATIENT_NAME.wav',
                'owner':'PRIVATE_OWNER_UUID','owner_id':'PRIVATE_EXTERNAL_ID','user_metadata':{'text':'PRIVATE_NOTE'},'metadata':{'size':3}}]
            o['inventory']['policies']=[{'schemaname':'storage','tablename':'objects','policyname':'policy','qual':"secret='SQL_SECRET'"}]
        ctx['observations']['target_current']['services']['configs']['config/auth']['smtp_pass']='DIFFERENT_PASSWORD'
        report=build_report(ctx)
        serialized=json.dumps(report)
        for private in ['SECRET_DO_NOT_LEAK','person@example.test','PRIVATE_PATIENT_NAME','PRIVATE_OWNER_UUID',
                        'PRIVATE_EXTERNAL_ID','PRIVATE_NOTE','SQL_SECRET','DIFFERENT_PASSWORD']:
            self.assertNotIn(private,serialized)
        self.assertIn('config/auth/smtp_pass',serialized)

    def test_failure_and_recovery_never_claim_features_migrated(self):
        ctx=context()
        ctx['state']['phase']='failed_recovered'
        report=build_report(ctx)
        self.assertFalse(report['migration_completed'])
        self.assertEqual(report['features']['database']['migration_status'],'not_completed')

    def test_missing_data_checksums_do_not_mean_equal_data(self):
        ctx=context()
        del ctx['observations']['source_current']['inventory']['checksums']
        report=build_report(ctx)
        self.assertEqual(report['features']['database']['current_diff'],'metadata_equal_data_not_checked')

    def test_missing_observation_marked_not_collected(self):
        ctx=context()
        del ctx['observations']['target_current']
        report=build_report(ctx)
        self.assertFalse(report['comparison_complete'])
        self.assertEqual(report['features']['storage']['current_diff'],'not_collected')

    def test_managed_realtime_diff_doesnt_pollute_application_diff(self):
        ctx=context()
        ctx['observations']['source_current']['inventory']['tables'].append({'schema':'realtime','name':'messages_future'})
        report=build_report(ctx)
        self.assertEqual(report['features']['database']['current_diff'],'equal')
        self.assertEqual(report['features']['platform']['current_diff'],'different')

    def test_reporting_failure_does_not_call_recovery(self):
        ctx=context()
        ctx.update(source_db=object(),source_project=object(),target_db=object(),target_project=object())
        with tempfile.TemporaryDirectory() as d,patch('supamigrate.reporting.observe',side_effect=RuntimeError('secret failure')):
            ctx['run_dir']=Path(d)
            with patch('supamigrate.runner.recover') as recovery:
                result=finish_report(ctx)
            recovery.assert_not_called()
            self.assertEqual(result['errors'],['source_observation_failed','target_observation_failed'])
            self.assertNotIn('secret failure',json.dumps(result))
            self.assertTrue((Path(d)/'final-report.md').is_file())

    def test_report_files_are_private(self):
        with tempfile.TemporaryDirectory() as d:
            ctx=context()
            ctx['run_dir']=Path(d)
            write_report(ctx)
            for f in ('final-report.json','final-report.md'):
                self.assertEqual((Path(d)/f).stat().st_mode & 0o777,0o600)

    def test_platform_publications_removed_from_restore_toc_only(self):
        toc='''10; 6104 11 PUBLICATION - supabase_realtime postgres
11; 6106 12 PUBLICATION TABLE public supabase_realtime records postgres
12; 6104 13 PUBLICATION - supabase_realtime_messages_publication supabase_admin
13; 6106 14 PUBLICATION TABLE realtime supabase_realtime_messages_publication messages postgres
14; 3466 15 EVENT TRIGGER - pgrst_ddl_watch supabase_admin
15; 1255 16 FUNCTION public foo() postgres
'''
        filtered=portable_toc(toc)
        self.assertIn('PUBLICATION - supabase_realtime postgres',filtered)
        self.assertIn('FUNCTION public foo()',filtered)
        self.assertNotIn('messages_publication',filtered)
        self.assertNotIn('pgrst_ddl_watch',filtered)
