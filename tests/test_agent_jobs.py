from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent import jobs
from agent.cli import main
from agent.evaluation import EVAL_SCHEMA
from pcb_assistant.catalog.common import CatalogError


def write_suite(path: Path) -> None:
    path.write_text(json.dumps({"case_id": "synthetic-1", "question": "压合后出现异常，应该怎么排查？"}, ensure_ascii=False) + "\n", encoding="utf-8")


class AgentJobsTests(unittest.TestCase):
    def test_start_progress_result_and_report_stay_in_job_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private" / "jobs"
            suite = Path(directory) / "suite.jsonl"
            write_suite(suite)
            with patch("agent.jobs.subprocess.Popen") as popen:
                popen.return_value.pid = 424242
                started = jobs.start_evaluation(suite, catalog=directory, root=root)

            job_id = started["job_id"]
            self.assertEqual(started["status"], "running")
            self.assertEqual(started["progress"], {"completed": 0, "total": 1, "case_id": None})
            self.assertTrue(Path(started["report_path"]).resolve().is_relative_to(root.resolve()))
            with patch("agent.jobs._alive", return_value=True):
                self.assertEqual(jobs.get_job(job_id, root=root)["status"], "running")

            expected_report = {"schema": EVAL_SCHEMA, "summary": {"case_count": 1}, "cases": []}
            with patch("agent.jobs.evaluate_file", return_value=expected_report) as evaluate:
                self.assertEqual(jobs.run_worker(job_id, root=root), 0)
            evaluate.assert_called_once()
            record, report = jobs.get_result(job_id, root=root)
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["progress"]["completed"], 1)
            self.assertEqual(report, expected_report)
            self.assertEqual(jobs.list_jobs(root=root)[0]["job_id"], job_id)

    def test_worker_records_per_case_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "jobs"
            suite = Path(directory) / "suite.jsonl"
            write_suite(suite)
            with patch("agent.jobs.subprocess.Popen") as popen:
                popen.return_value.pid = 123456
                started = jobs.start_evaluation(suite, catalog=directory, root=root)

            def evaluate(*args, progress_callback, **kwargs):
                progress_callback(1, 1, "synthetic-1")
                return {"schema": EVAL_SCHEMA, "summary": {"case_count": 1}, "cases": []}

            with patch("agent.jobs.evaluate_file", side_effect=evaluate):
                self.assertEqual(jobs.run_worker(started["job_id"], root=root), 0)
            record = json.loads((root / f"{started['job_id']}.json").read_text(encoding="utf-8"))
            self.assertEqual(record["progress"]["completed"], 1)
            self.assertIsNone(record["progress"]["case_id"])

    def test_cancel_and_incomplete_result_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "jobs"
            suite = Path(directory) / "suite.jsonl"
            write_suite(suite)
            with patch("agent.jobs.subprocess.Popen") as popen:
                popen.return_value.pid = 777
                started = jobs.start_evaluation(suite, catalog=directory, root=root)
            with patch("agent.jobs._alive", return_value=True), patch("agent.jobs.os.kill") as kill:
                stopped = jobs.stop_job(started["job_id"], root=root)
            self.assertEqual(stopped["status"], "cancelled")
            kill.assert_called_once()
            with self.assertRaisesRegex(CatalogError, "job_not_completed"):
                jobs.get_result(started["job_id"], root=root)

    def test_cli_lists_and_reports_background_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "jobs"
            suite = Path(directory) / "suite.jsonl"
            write_suite(suite)
            with patch("agent.jobs.subprocess.Popen") as popen:
                popen.return_value.pid = 888
                started = jobs.start_evaluation(suite, catalog=directory, root=root)
            with patch("agent.cli.list_jobs", return_value=[started]):
                self.assertEqual(main(["jobs", "list"]), 0)
            with patch("agent.cli.get_job", return_value=started):
                self.assertEqual(main(["jobs", "status", started["job_id"]]), 0)


if __name__ == "__main__":
    unittest.main()
