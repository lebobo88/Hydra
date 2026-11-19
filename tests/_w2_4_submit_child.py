"""Hydra#92 W2.4 step 3: shared harness and child process for the submit-convergence tests.

Not collected by pytest (leading underscore). It has two roles:

* a module imported by ``tests/test_w2_4_submit_convergence.py`` for the real-SqliteSaver
  harness (the same production-serde construction as the step-1 capture script
  ``fixtures/w2_4_attended_step_baseline_S8ad24e5.capture.txt``);
* a plain script run as a child process by ``test_two_process_submits_converge``:

    python tests/_w2_4_submit_child.py <project> <db> <result> <run_id> <call_key> \
        <barrier_dir> <name> <peers> <deadline_epoch> <max_retries>

  It is an ordinary Python subprocess calling ``cli._cmd_attended_submit`` once (with
  bounded retries on ``resume_in_progress``). It starts no Hydra workflow, daemon or
  detached job. State paths come from the environment the parent passes in.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest import mock
from uuid import UUID

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from hydra_core import cli, head_registry  # noqa: E402
from hydra_core.state import HydraState, TaskState, make_checkpoint_serde  # noqa: E402
from tests.test_w2_1_cli_head_legs import _Disp, _Pack, _Written, _tid, _wf  # noqa: E402


def _resolver(squad, head, **_kw):
    return head_registry.HeadResolution(
        squad=squad, head=head, plugin="plug", agent_name=head,
        qualified_name=f"plug:{head}", agent_path=None, rule="slug")


def open_sup(db):
    """A real compiled graph over HydraState on a real SqliteSaver (production serde)."""
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, StateGraph
    g = StateGraph(HydraState)
    g.add_node("n", lambda s: {})
    g.add_node("postcheck", lambda s: {})
    g.set_entry_point("n")
    g.add_edge("n", "postcheck")
    g.add_edge("postcheck", END)
    conn = sqlite3.connect(str(db), check_same_thread=False)
    return g.compile(checkpointer=SqliteSaver(conn, serde=make_checkpoint_serde())), conn


def _run(fn, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*args)
    return rc, out.getvalue(), err.getvalue()


class Harness:
    """One scenario: a project root plus a real checkpoint file, fresh connection per call."""

    def __init__(self, project, db, heads=("h1", "h2", "h3")):
        self.project = Path(project)
        self.db = Path(db)
        self.heads = list(heads)
        self.cfg = {"configurable": {"thread_id": _wf}}
        self.pack = _Pack("claude-native")

    def seed(self):
        sup, conn = open_sup(self.db)
        try:
            st = HydraState(root_goal="brief the board", workflow_id=UUID(_wf))
            st.budget.budget_usd = 50.0
            task = TaskState(task_id=UUID(_tid), owner_squad="executive",
                             description="brief the board", heads=self.heads)
            sup.update_state(self.cfg, {
                "root_goal": st.root_goal, "workflow_id": st.workflow_id,
                "budget": st.budget, "tasks": [task]}, as_node="n")
        finally:
            conn.close()

    @contextlib.contextmanager
    def _patched(self, sup):
        disp = _Disp()
        packs = {self.pack.slug: self.pack}
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(head_registry, "resolve_head", _resolver))
            es.enter_context(mock.patch.object(
                cli, "_attended_live_dispatcher", lambda *a, **k: disp))
            es.enter_context(mock.patch("hydra_core.supervisor.build_supervisor",
                                        lambda **_k: sup))
            es.enter_context(mock.patch("hydra_core.squad_loader.discover_squads",
                                        lambda *a, **k: packs))
            es.enter_context(mock.patch("hydra_core.squad_node._maybe_write_claude_shim",
                                        lambda *a, **k: None))
            es.enter_context(mock.patch("hydra_core.artifact_store.write_native_artifact",
                                        lambda *a, **k: _Written()))
            yield

    def _call(self, fn, args):
        sup, conn = open_sup(self.db)  # fresh connection per verb call
        try:
            with self._patched(sup):
                return _run(fn, args)
        finally:
            conn.close()

    def step(self, want_rc=0):
        rc, out, err = self._call(cli._cmd_attended_step, argparse.Namespace(
            project=str(self.project), workflow_id=_wf, verbose=False))
        assert rc == want_rc, (rc, out or err)[:800]
        return json.loads(out)

    def submit_raw(self, run_id, call_key, result_file):
        return self._call(cli._cmd_attended_submit, argparse.Namespace(
            project=str(self.project), workflow_id=_wf, run_id=run_id,
            call_key=call_key, result=str(result_file), verbose=False))

    def values(self):
        sup, conn = open_sup(self.db)
        try:
            return sup.get_state(self.cfg).values
        finally:
            conn.close()


def _child_main(argv):
    (project, db, result, run_id, call_key, barrier_dir, name, peers,
     deadline, max_retries) = argv
    deadline_epoch = float(deadline)
    barrier = Path(barrier_dir)
    # Imports and the supervisor-free setup are done: announce ready, then wait for
    # every peer so both children enter the submit together.
    (barrier / f"{name}.ready").write_text("1", encoding="utf-8")
    while len(list(barrier.glob("*.ready"))) < int(peers):
        if time.time() > deadline_epoch:
            print(json.dumps({"name": name, "error": "barrier_timeout"}))
            return 3
        time.sleep(0.005)
    h = Harness(project, db)
    attempts, resp, rc = 0, None, None
    for attempts in range(1, int(max_retries) + 1):
        rc, out, err = h.submit_raw(run_id, call_key, result)
        resp = json.loads(out or err)
        if resp.get("status") != "resume_in_progress":
            break
        if time.time() > deadline_epoch:
            break
        time.sleep(0.01)
    print(json.dumps({"name": name, "rc": rc, "attempts": attempts, "resp": resp}))
    return 0 if rc == 0 and resp.get("status") != "resume_in_progress" else 2


if __name__ == "__main__":
    sys.exit(_child_main(sys.argv[1:]))
