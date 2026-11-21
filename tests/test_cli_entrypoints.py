"""Launch the reorganized CLIs through their supported public commands."""
import subprocess
import sys
import tempfile
import unittest

from pcb_assistant.paths import ROOT


class CliEntrypointTests(unittest.TestCase):
    def help(self, arguments, cwd):
        process = subprocess.run(
            [sys.executable, '-X', 'utf8', '-B', *arguments, '--help'],
            cwd=cwd, text=True, encoding='utf-8', capture_output=True, timeout=30,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn('usage:', process.stdout.lower())
        return process.stdout

    def test_catalog_wrapper_launches_outside_workspace_and_module_launches_inside(self):
        with tempfile.TemporaryDirectory() as temporary:
            legacy = self.help([str(ROOT / 'knowledge_catalog.py')], temporary)
        packaged = self.help(['-m', 'pcb_assistant.catalog.cli'], ROOT)
        for output in (legacy, packaged):
            self.assertIn('--catalog', output)
            self.assertIn('chat', output)
            self.assertIn('ingest', output)

    def test_optional_tools_show_help_without_processing_data_or_training(self):
        modules = [
            'pcb_assistant.ingestion.mail',
            'pcb_assistant.ingestion.attachments',
            'pcb_assistant.ingestion.ocr',
            'pcb_assistant.training.local',
            'pcb_assistant.training.worker',
            'pcb_assistant.training.service',
            'pcb_assistant.evaluation.stdio_trial',
            'pcb_assistant.evaluation.codex_preflight',
        ]
        for module in modules:
            with self.subTest(module=module):
                self.help(['-m', module], ROOT)


if __name__ == '__main__':
    unittest.main()
