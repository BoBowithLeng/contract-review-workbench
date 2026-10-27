"""Adapter tests use subprocess doubles, never a billable model request."""
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import review_runner as runner


class RunnerTests(unittest.TestCase):
    def test_general_output_schema_has_dynamic_themes_not_legacy_three_rules(self):
        schema = runner.output_schema("general")
        self.assertIn("theme", schema["properties"]["findings"]["items"]["properties"])
        self.assertNotIn("enum", schema["properties"]["findings"]["items"]["properties"]["rule_id"])
        self.assertEqual(schema["properties"]["checks"]["maxItems"], 60)

    def test_phase_and_frozen_plan_choose_internal_schema(self):
        schema = runner.output_schema("general", phase="review_preparation")
        self.assertIn("questions", schema["required"])
        self.assertNotIn("findings", schema["properties"])
        self.assertIn("checklist_results", runner.output_schema("general", preparation={"checks": [{"id": "C1"}]})["required"])
        with self.assertRaises(runner.ReviewRunnerError):
            runner.output_schema("general", phase="caller-made-schema")
        schemas = []
        def fake_run(args, **kwargs):
            schemas.append(json.loads(Path(args[args.index('--output-schema') + 1]).read_text(encoding='utf-8')))
            self.assertEqual(Path(args[args.index('-C') + 1]), runner.CODEX_WORKSPACE.resolve())
            self.assertTrue(Path(args[args.index('-o') + 1]).is_absolute())
            self.assertTrue(Path(args[args.index('--output-schema') + 1]).is_absolute())
            Path(args[args.index('-o') + 1]).write_text('{}', encoding='utf-8')
            return subprocess.CompletedProcess(args, 0, '{"type":"turn.completed"}', '')
        with patch.object(runner, 'find_codex', return_value=Path('trusted-codex.exe')), patch.object(runner.subprocess, 'run', side_effect=fake_run):
            runner.run_review([{'role':'user','content':'data'}], {'engine_key':'codex_cli','profile_key':'general',
                              'review_phase':'review_preparation', 'output_schema': {'untrusted': True}})
        self.assertEqual(schemas, [schema])

    def test_cli_args_are_text_only_and_ignore_client_model_or_path(self):
        seen = []
        def fake_run(args, **kwargs):
            seen.append((args, kwargs))
            Path(args[args.index('-o')+1]).write_text('{"findings":[],"checks":[],"options":[],"uncovered":[]}', encoding='utf-8')
            return subprocess.CompletedProcess(args, 0, '{"type":"turn.completed"}', '')
        with patch.object(runner, 'find_codex', return_value=Path('trusted-codex.exe')), patch.object(runner.subprocess, 'run', side_effect=fake_run):
            result = runner.run_review([{'role':'system','content':'Review only.'},{'role':'user','content':'Untrusted input'}],
                                       {'engine_key':'codex_cli','model':'untrusted-model','executable':'bad.exe'})
        args, options = seen[0]
        self.assertEqual(args[0], 'trusted-codex.exe')
        self.assertEqual(args[args.index('--model')+1], runner.MODEL)
        self.assertIn('--ephemeral', args)
        self.assertIn('--ignore-user-config', args)
        self.assertEqual(args[args.index('--sandbox')+1], 'read-only')
        self.assertIn('shell_tool', args)
        self.assertIn('UNTRUSTED INPUT DATA', options['input'])
        self.assertNotIn('OPENAI_API_KEY', options['env'])
        self.assertIsInstance(json.loads(result), dict)

    def test_tool_activity_and_turn_failure_are_not_accepted(self):
        for event in ({'type':'turn.failed'}, {'type':'item.completed','item':{'type':'command_execution'}},
                      {'type':'item.started','item':{'type':'mcp_tool_call'}}):
            with self.assertRaises(runner.ReviewRunnerError):
                runner._check_events(json.dumps(event))

    def test_network_failure_does_not_echo_logs_or_retry(self):
        with patch.object(runner, 'find_codex', return_value=Path('codex.exe')), patch.object(runner.subprocess, 'run',
            return_value=subprocess.CompletedProcess([], 1, 'private text', 'secret-key')) as process:
            with self.assertRaises(runner.ReviewRunnerError) as caught:
                runner.run_review([{'role':'user','content':'input'}], {'engine_key':'codex_cli'})
        self.assertNotIn('secret-key', str(caught.exception))
        self.assertNotIn('private text', str(caught.exception))
        self.assertEqual(process.call_count, 1)

    def test_compatible_provider_delegates_without_storing_config(self):
        config={'engine_key':'compatible_api','api_key':'temporary','base_url':'https://example.com','model':'example'}
        messages=[{'role':'user','content':'input'}]
        with patch.object(runner,'chat_completion',return_value='{"ok":true}') as call:
            self.assertEqual(runner.run_review(messages,config),'{"ok":true}')
        call.assert_called_once_with(config,messages)


if __name__ == '__main__':
    unittest.main()
