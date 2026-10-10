from contextlib import ExitStack, nullcontext
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from supamigrate.core import MigrationError
from supamigrate.runner import main, stable_inventory
from test_safety import SOURCE, TARGET, inventory, services


class OrchestrationTests(unittest.TestCase):
    def run_scenario(self, *, plan=False, confirm=True, fail_replace=False, fail_recover=False, fail_capture=False):
        result = {}
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            src_db,dst_db=MagicMock(),MagicMock()
            for db in (src_db,dst_db):
                db.consistent_snapshot.side_effect=lambda:nullcontext()
                db.inventory.side_effect=lambda:copy.deepcopy(inventory())
                db.checksums.return_value={}
            stack.enter_context(patch('supamigrate.runner.dependencies'))
            stack.enter_context(patch('sys.stdin.isatty',return_value=True))
            stack.enter_context(patch('supamigrate.runner.credentials',return_value=('token',src_db,dst_db)))
            stack.enter_context(patch('supamigrate.runner.Project'))
            stack.enter_context(patch('supamigrate.runner.inspect_services',side_effect=lambda project:services()))
            stack.enter_context(patch('supamigrate.runner.check_manifest'))
            stack.enter_context(patch('builtins.input',return_value='MIGRAR '+TARGET if confirm else 'no'))
            stack.enter_context(patch('sys.stdout',new=io.StringIO()))
            stack.enter_context(patch('sys.stderr',new=io.StringIO()))
            def capture(project,db,inv,service,folder,token):
                folder.mkdir()
                inv['checksums']={}
                if fail_capture:
                    raise MigrationError('capture failed')
            result['capture']=stack.enter_context(patch('supamigrate.runner.capture',side_effect=capture))
            result['replace']=stack.enter_context(patch('supamigrate.runner.replace',side_effect=MigrationError('injected') if fail_replace else None))
            result['recover']=stack.enter_context(patch('supamigrate.runner.recover',side_effect=MigrationError('recovery injected') if fail_recover else None))
            args=['--source',SOURCE,'--target',TARGET,'--work-dir',directory]+(['--plan'] if plan else [])
            try:
                result['code']=main(args)
            except MigrationError as exc:
                result['error']=str(exc)
            states=list(Path(directory).glob('*/state.json'))
            result['state']=json.loads(states[0].read_text()) if states else {}
            result['dst_db']=dst_db
        return result

    def test_plan_stops_before_capture_or_mutation(self):
        result=self.run_scenario(plan=True)
        self.assertEqual(result['code'],0)
        self.assertEqual(result['state']['phase'],'planned')
        result['capture'].assert_not_called()
        result['replace'].assert_not_called()

    def test_cancel_leaves_target_intact(self):
        result=self.run_scenario(confirm=False)
        self.assertIn('cancelada',result['error'])
        result['capture'].assert_not_called()
        result['replace'].assert_not_called()
        result['recover'].assert_not_called()

    def test_backup_failure_never_starts_cleanup(self):
        result=self.run_scenario(fail_capture=True)
        self.assertIn('capture failed',result['error'])
        result['replace'].assert_not_called()
        result['recover'].assert_not_called()

    def test_failure_after_start_recovers_but_returns_error(self):
        result=self.run_scenario(fail_replace=True)
        result['recover'].assert_called_once()
        self.assertEqual(result['state']['phase'],'failed_recovered')
        self.assertIn('não concluída',result['error'])

    def test_failed_recovery_is_not_reported_as_success(self):
        result=self.run_scenario(fail_replace=True,fail_recover=True)
        self.assertEqual(result['state']['phase'],'recovery_failed')
        self.assertIn('error',result)
        self.assertNotIn('code',result)

    def test_success_requires_both_captures_and_verify_pipeline(self):
        result=self.run_scenario()
        self.assertEqual(result['capture'].call_count,2)
        result['replace'].assert_called_once()
        result['recover'].assert_not_called()
        self.assertEqual(result['state']['phase'],'completed')

    def test_drift_ignores_only_access_telemetry_and_database_size(self):
        a,b=inventory(),inventory()
        a['objects']=[{'name':'file','version':'1','last_accessed_at':'old'}]
        b['objects']=[{'name':'file','version':'1','last_accessed_at':'new'}]
        b['bytes']=2000
        self.assertEqual(stable_inventory(a),stable_inventory(b))
        b['objects'][0]['version']='2'
        self.assertNotEqual(stable_inventory(a),stable_inventory(b))
