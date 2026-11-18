"""Minimal desktop backend (#84): a task injected into a Desktop Computer Use thread through `codex queue`.

Every test runs on FakeRuntime: no real thread is ever written to."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import cli, core, mcp_server, store
from test_orchd import FakeRuntime

THREAD = "019f0000-aaaa-bbbb-cccc-000000000001"


class DesktopRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.worktrees_created, self.started = [], []

    def create_worktree(self, *args, **kwargs):
        self.worktrees_created.append(args)
        return super().create_worktree(*args, **kwargs)

    def start_codex_worker(self, *args, **kwargs):
        self.started.append(args)
        raise AssertionError("a desktop task must not start a worker process")

    start_worker = start_codex_worker

    def remove_worktree(self, *args, **kwargs):
        self.removed.append(args)

    def stop_task_worker(self, *args, **kwargs):
        self.stopped.append(args)


class DesktopBackendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.con.close)
        self.rt = DesktopRuntime()

    def dispatch(self, thread=THREAD, **kw):
        kw = {"backend": "desktop", "desktop_thread": thread, "model_reason": "needs a GUI",
              "task_type": "investigation", **kw}
        return core.dispatch(self.con, self.rt, orch_thread="orch-A", repo="demo", title="Look at the page",
                             instructions="open the dashboard and read the totals", done_when="totals reported", **kw)

    # -- dispatch ---------------------------------------------------------------
    def test_dispatch_creates_no_worktree_and_starts_no_process(self):
        t = self.dispatch()
        self.assertEqual((self.rt.worktrees_created, self.rt.started), ([], []))
        self.assertEqual((t["backend"], t["desktop_thread"], t["status"]), ("desktop", THREAD, "running"))
        self.assertIsNone(t["worktree"])
        self.assertIsNone(t["branch"])
        self.assertIsNone(t["job_id"])
        self.assertIsNone(t["session_id"])

    def test_dispatch_injects_task_into_the_thread_through_the_queue(self):
        t = self.dispatch()
        self.assertEqual(len(self.rt.woken), 1)
        binary, thread, text = self.rt.woken[0]
        self.assertEqual((binary, thread), ("/fake/codex", THREAD))
        self.assertIn(f"[orchd task {t['id']}]", text)
        self.assertIn("open the dashboard and read the totals", text)
        self.assertIn("totals reported", text)
        self.assertIn(f"ack {t['id']}", text)
        self.assertIn("read-only", text.lower())
        self.assertIn(f" ask {t['id']} ", text)
        self.assertNotIn("Worktree:", text)

    def test_dispatch_requires_a_valid_thread_id(self):
        for bad in (None, "", "   ", "a b", "x;rm -rf", "../x", "a" * 129):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "desktop_thread"):
                self.dispatch(thread=bad)
        self.assertEqual(self.rt.woken, [])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)

    def test_desktop_thread_is_rejected_for_other_backends(self):
        with self.assertRaisesRegex(ValueError, "desktop_thread"):
            core.dispatch(self.con, self.rt, orch_thread="orch-A", repo="demo", title="T", instructions="i",
                          done_when="d", model="sonnet", model_reason="r", task_type="code", desktop_thread=THREAD)

    def test_desktop_rejects_options_that_need_a_worktree_or_model(self):
        for extra in (dict(base_branch="main"), dict(verifies="abcd1234"), dict(model="sol"),
                      dict(workflow="w"), dict(attachment_refs=[{"attachment_id": "a"}])):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "desktop"):
                self.dispatch(**extra)
        self.assertEqual(self.rt.woken, [])

    def test_queue_failure_marks_task_failed_and_keeps_no_leftovers(self):
        self.rt.wake_fails = True
        with self.assertRaises(OSError):
            self.dispatch()
        row = self.con.execute("SELECT status, note FROM tasks").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("queue down", row["note"])
        self.assertEqual((self.rt.worktrees_created, self.rt.removed), ([], []))
        self.rt.wake_fails = False
        self.assertTrue(self.dispatch()["id"])  # a failed task no longer blocks the thread

    def test_one_open_task_per_thread(self):
        first = self.dispatch()
        with self.assertRaisesRegex(ValueError, first["id"]):
            self.dispatch()
        self.assertEqual(len(self.rt.woken), 1)
        self.assertTrue(self.dispatch(thread="019f0000-other")["id"])  # another thread is independent
        core.close(self.con, self.rt, first["id"])
        self.assertTrue(self.dispatch()["id"])

    # -- ack / report ------------------------------------------------------------
    def test_ack_progress_report_link_to_the_task_and_wake_the_orch(self):
        t = self.dispatch()
        self.rt.woken.clear()
        core.ack(self.con, t["id"])
        self.assertEqual(store.get_task(self.con, t["id"])["status"], "acked")
        core.progress(self.con, self.rt, t["id"], "page loaded")
        core.report(self.con, self.rt, t["id"], "done", "totals: 42", "screenshot reports/x/1.png")
        self.assertEqual(store.get_task(self.con, t["id"])["status"], "done")
        self.assertEqual([w[1] for w in self.rt.woken], ["orch-A", "orch-A"])  # Orch, never the desktop thread
        kinds = [r["kind"] for r in self.con.execute("SELECT kind FROM messages WHERE task_id=? ORDER BY id", (t["id"],))]
        self.assertEqual(kinds, ["dispatch", "ack", "progress", "report"])

    def test_inbox_and_list_open_mark_desktop_events_unverified(self):
        t = self.dispatch()
        core.ack(self.con, t["id"])
        core.report(self.con, self.rt, t["id"], "done", "totals: 42", "evidence")
        rows = core.inbox(self.con, "orch-A")
        self.assertTrue(rows)
        self.assertEqual({r["source"] for r in rows}, {"desktop-unverified"})
        shown = core.list_open(self.con, self.rt)
        self.assertEqual([r["source"] for r in shown], ["desktop-unverified"])

    def test_regular_tasks_carry_no_source_marker(self):
        rt = FakeRuntime()
        t = core.dispatch(self.con, rt, orch_thread="orch-A", repo="demo", title="T", instructions="i",
                          done_when="d", model="sonnet", model_reason="r", task_type="code", backend="exec")
        core.report(self.con, rt, t["id"], "done", "s", "e")
        self.assertNotIn("source", core.inbox(self.con, "orch-A")[0])
        self.assertNotIn("source", core.list_open(self.con, rt)[0])

    # -- answer ------------------------------------------------------------------
    def test_answer_is_injected_into_the_same_thread_and_recorded(self):
        t = self.dispatch()
        core.ack(self.con, t["id"])
        core.ask(self.con, self.rt, t["id"], "send it?")
        self.rt.woken.clear()
        result = core.answer(self.con, self.rt, t["id"], "yes, send exactly that")
        self.assertEqual((result["status"], result["delivered"], result["pending"]), ("delivered", 1, 0))
        self.assertEqual(len(self.rt.woken), 1)
        binary, thread, text = self.rt.woken[0]
        self.assertEqual(thread, THREAD)
        self.assertEqual(text, f"[orchd answer {t['id']}]\nyes, send exactly that")
        self.assertEqual(store.pending_answer_count(self.con, t["id"]), 0)
        self.assertEqual(store.get_task(self.con, t["id"])["status"], "acked")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE task_id=? AND kind='answer'",
                                          (t["id"],)).fetchone()[0], 1)

    def test_failed_answer_stays_queued_and_flush_redelivers_once(self):
        t = self.dispatch()
        core.ask(self.con, self.rt, t["id"], "send it?")
        self.rt.woken.clear()
        self.rt.wake_fails = True
        result = core.answer(self.con, self.rt, t["id"], "yes")
        self.assertEqual((result["status"], result["delivered"], result["pending"]), ("failed", 0, 1))
        self.assertIn("queue down", result["error"])
        self.assertEqual(store.get_task(self.con, t["id"])["status"], "question")
        self.rt.wake_fails = False
        again = core.answer(self.con, self.rt, t["id"], flush=True)
        self.assertEqual((again["status"], again["delivered"], again["pending"]), ("delivered", 1, 0))
        self.assertEqual([w[2] for w in self.rt.woken], [f"[orchd answer {t['id']}]\nyes"])
        self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True)["delivered"], 0)
        self.assertEqual(len(self.rt.woken), 1)  # nothing re-sent

    def test_followup_goes_through_the_same_injection(self):
        t = self.dispatch()
        core.followup(self.con, self.rt, t["id"], "also read the footer")
        self.assertEqual(self.rt.woken[-1][1], THREAD)
        self.assertIn("also read the footer", self.rt.woken[-1][2])

    def test_interrupt_and_retry_are_refused(self):
        t = self.dispatch()
        with self.assertRaisesRegex(ValueError, "desktop"):
            core.interrupt(self.con, self.rt, t["id"], "stop")
        with self.assertRaisesRegex(ValueError, "desktop"):
            core.retry(self.con, self.rt, t["id"], "sonnet", "try again")
        self.assertEqual(len(self.rt.woken), 1)

    def test_auto_flush_skips_desktop_tasks(self):
        t = self.dispatch()
        self.assertEqual(core.auto_flush(self.con, self.rt, t["id"])["status"], "skipped")

    # -- view / close -------------------------------------------------------------
    def test_view_returns_the_thread_link_without_opening_anything(self):
        t = self.dispatch()
        text = core.view(self.con, self.rt, t["id"])
        self.assertIn(f"codex://threads/{THREAD}", text)
        self.assertEqual(self.rt.viewed, [])

    def test_close_writes_the_event_without_touching_a_worktree_or_process(self):
        t = self.dispatch()
        result = core.close(self.con, self.rt, t["id"], outcome="done")
        self.assertTrue(result["closed"])
        self.assertEqual(store.get_task(self.con, t["id"])["status"], "closed")
        self.assertEqual((self.rt.removed, self.rt.stopped), ([], []))
        with self.assertRaisesRegex(ValueError, "closed"):
            core.answer(self.con, self.rt, t["id"], "late")

    # -- surfaces -----------------------------------------------------------------
    def test_mcp_dispatch_schema_and_call(self):
        tool = next(x for x in mcp_server.TOOLS if x["name"] == "dispatch")
        props = tool["inputSchema"]["properties"]
        self.assertIn("desktop", props["backend"]["enum"])
        self.assertIn("desktop_thread", props)
        out = mcp_server.call("dispatch", {"repo": "demo", "title": "T", "instructions": "i", "done_when": "d",
                                           "model_reason": "r", "task_type": "investigation", "backend": "desktop",
                                           "desktop_thread": THREAD}, "orch-A", self.con, self.rt, [])
        self.assertEqual((out["status"], out["worktree"], out["desktop_thread"]), ("running", None, THREAD))

    def test_cli_accepts_backend_desktop(self):
        env = patch.dict(os.environ, {"ORCHD_HOME": self.tmp.name, "ORCHD_CONFIG_DIR": self.tmp.name,
                                     "CODEX_THREAD_ID": "caller-thread"})
        with env, patch("orchd.home_migrations.ensure_current", return_value=[]), \
                patch.object(cli, "Runtime", return_value=self.rt), contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(["dispatch", "--repo", "demo", "--backend", "desktop", "--desktop-thread", THREAD,
                             "--title", "T", "--instructions", "i", "--done-when", "d", "--model-reason", "r",
                             "--task-type", "investigation"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["desktop_thread"], THREAD)


if __name__ == "__main__":
    unittest.main()
