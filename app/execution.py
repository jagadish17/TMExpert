"""Test execution: recording results, evidence and the append-only execution history.

The test run row holds the *current* result.  Every save (quick status, full
execution or step results) also appends one row to execution/Execution_History.xlsx
with the attempt number, previous status, actual result, notes, defect IDs,
evidence and a summary of the step results, so no earlier result is ever lost.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .attachments import AttachmentService
from .storage import ExcelStorageService, utcnow

RUN_STATUSES = ("Not Run", "In Progress", "Passed", "Failed", "Blocked", "Not Applicable")
EXECUTED = {"Passed", "Failed", "Blocked", "Not Applicable"}
OPEN = {"Not Run", "In Progress"}
MAX_TEXT = 4000
_DEFECT = re.compile(r"^[^\s<>\"']{1,120}$")


class StaleExecution(ValueError):
    """Raised when the run was updated by someone else since it was opened."""


def parse_defects(value: Any) -> list[str]:
    items = value if isinstance(value, list) else re.split(r"[,\s]+", str(value or ""))
    defects = list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))
    if len(defects) > 20: raise ValueError("Link at most 20 defects")
    bad = [d for d in defects if not _DEFECT.match(d)]
    if bad: raise ValueError(f"“{bad[0][:40]}” is not a valid defect ID or link")
    return defects


class ExecutionService:
    def __init__(self, store: ExcelStorageService, service: Any, attachments: AttachmentService):
        self.store = store
        self.service = service
        self.attachments = attachments

    # ---------------------------------------------------------------- helpers
    def _run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get("test_runs", run_id, include_archived=False)
        if not run: raise KeyError("Test run not found")
        return run
    def _open_cycle(self, run: dict[str, Any]) -> dict[str, Any]:
        cycle = self.store.get("test_cycles", run["cycle_id"], include_archived=False)
        if not cycle: raise ValueError("This test case's cycle has been archived")
        if cycle.get("status") == "Completed": raise ValueError("This cycle is completed. Reopen it to record results.")
        return cycle
    @staticmethod
    def _attempt(run: dict[str, Any]) -> int:
        try: return int(run.get("attempt_count") or 0)
        except (TypeError, ValueError): return 0
    def _check_base(self, run: dict[str, Any], base_attempt: Any) -> None:
        if base_attempt in (None, ""): return
        try: base = int(base_attempt)
        except (TypeError, ValueError) as exc: raise ValueError("Invalid base attempt") from exc
        if base != self._attempt(run):
            raise StaleExecution("This test case was updated by someone else after you opened it. Reload to see the latest result before saving.")
    def _step_summary(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.store.list("execution_results", filters={"test_run_id": run_id}, page_size=1_000_000)["items"]
        summary = [{"step": str(r.get("step_number", "")), "status": r.get("status", ""), "actual": str(r.get("actual_result", ""))[:500]}
                   for r in sorted(rows, key=lambda r: int(r.get("step_number") or 0))]
        # An Excel cell holds at most 32,767 characters; shorten the per-step text if needed.
        for limit in (500, 150, 40, 0):
            for item in summary: item["actual"] = item["actual"][:limit]
            if len(json.dumps(summary, ensure_ascii=False)) <= 30000: break
        return summary

    def _record(self, run: dict[str, Any], cycle: dict[str, Any], status: str, actor_id: str, source: str,
                changes: dict[str, Any] | None = None, attachment_ids: list[str] | None = None) -> dict[str, Any]:
        """Persist the run's new current state and append the history entry."""
        if status not in RUN_STATUSES: raise ValueError(f"Status must be one of: {', '.join(RUN_STATUSES)}")
        now = utcnow(); actor = actor_id if self.store.get("users", actor_id) else ""
        attempt = self._attempt(run) + 1
        update = {"status": status, "executed_by": actor, "executed_at": now, "attempt_count": str(attempt), **(changes or {})}
        if status != "Not Run": update["started_at"] = run.get("started_at") or now
        update["completed_at"] = now if status in EXECUTED else ""
        updated = self.store.update("test_runs", run["id"], update, actor_id)
        if run.get("test_case_id") and self.store.get("test_cases", run["test_case_id"]):
            self.store.update("test_cases", run["test_case_id"], {"last_status": status}, actor_id)
        if cycle.get("status") in ("", None, "Not Started") and status != "Not Run":
            self.store.update("test_cycles", cycle["id"], {"status": "In Progress"}, actor_id)
        self.store.create("execution_history", {
            "project_id": run.get("project_id", ""), "cycle_id": run["cycle_id"], "test_run_id": run["id"], "case_key": run.get("case_key", ""),
            "attempt": str(attempt), "status": status, "previous_status": run.get("status") or "Not Run",
            "actual_result": updated.get("actual_result", ""), "notes": updated.get("execution_notes", ""),
            "defect_ids": updated.get("defect_ids") or [], "attachment_ids": attachment_ids or [],
            "step_results": self._step_summary(run["id"]), "source": source, "executed_by": actor, "executed_at": now,
        }, actor_id)
        return updated

    # --------------------------------------------------------------- actions
    def quick_status(self, run_id: str, status: str, actor_id: str, base_attempt: Any = None) -> dict[str, Any]:
        run = self._run(run_id); cycle = self._open_cycle(run)
        self._check_base(run, base_attempt)
        return self._record(run, cycle, status, actor_id, "quick")

    def save_steps(self, run_id: str, results: Any, actor_id: str) -> dict[str, Any]:
        run = self._run(run_id); cycle = self._open_cycle(run)
        status = self.service.write_step_results(run_id, results, actor_id)
        self._record(self._run(run_id), cycle, status, actor_id, "steps")
        return self.detail(run_id)

    def execute(self, run_id: str, payload: dict[str, Any], actor_id: str) -> dict[str, Any]:
        run = self._run(run_id); cycle = self._open_cycle(run)
        self._check_base(run, payload.get("base_attempt"))
        status = str(payload.get("status", ""))
        if status not in RUN_STATUSES: raise ValueError("Choose an execution status")
        # Fields not sent keep their current value (a quick "Fail" must not wipe notes or defects).
        actual = str(payload["actual_result"] if "actual_result" in payload else run.get("actual_result", "") or "").strip()
        notes = str(payload["notes"] if "notes" in payload else run.get("execution_notes", "") or "").strip()
        if len(actual) > MAX_TEXT or len(notes) > MAX_TEXT: raise ValueError(f"Actual result and notes must be {MAX_TEXT} characters or fewer")
        defects = parse_defects(payload["defect_ids"] if "defect_ids" in payload else run.get("defect_ids") or [])
        attachment_ids = [str(i) for i in dict.fromkeys(payload.get("attachment_ids") or [])]
        for attachment_id in attachment_ids:
            record = self.store.get("attachments", attachment_id, include_archived=False)
            if not record or record.get("owner_type") != "test_run" or record.get("owner_id") != run_id:
                raise ValueError("Evidence must be uploaded to this test case first")
        if payload.get("step_results"):
            self.service.write_step_results(run_id, payload["step_results"], actor_id)
        updated = self._record(self._run(run_id), cycle, status, actor_id, "execution",
                               {"actual_result": actual, "execution_notes": notes, "defect_ids": defects}, attachment_ids)
        return {"run": updated, **self.detail(run_id)}

    # --------------------------------------------------------------- reading
    def history(self, run_id: str) -> list[dict[str, Any]]:
        names = {u["id"]: u.get("name", "") for u in self.store.list("users", page_size=1_000_000, include_archived=True)["items"]}
        files = {a["id"]: a for a in self.store.list("attachments", filters={"owner_type": "test_run", "owner_id": run_id}, page_size=1_000_000, include_archived=True)["items"]}
        rows = self.store.list("execution_history", filters={"test_run_id": run_id}, page_size=1_000_000)["items"]
        rows.sort(key=lambda r: int(r.get("attempt") or 0), reverse=True)
        return [{**r, "executed_by_name": names.get(r.get("executed_by", ""), "") or "System",
                 "attachments": [self.attachments.public(files[i]) | {"archived": files[i].get("archived") is True} for i in (r.get("attachment_ids") or []) if i in files]} for r in rows]

    def detail(self, run_id: str) -> dict[str, Any]:
        base = self.service.execution_detail(run_id)
        run = base["run"]
        cycle = self.store.get("test_cycles", run["cycle_id"]) or {}
        names = {u["id"]: u.get("name", "") for u in self.store.list("users", page_size=1_000_000, include_archived=True)["items"]}
        return {**base, "cycle": {k: cycle.get(k, "") for k in ("id", "name", "status", "testing_type", "environment", "build")},
                "assignee_name": names.get(run.get("assignee_id", ""), ""), "executed_by_name": names.get(run.get("executed_by", ""), ""),
                "attachments": self.attachments.list("test_run", run_id), "history": self.history(run_id)}

    def workspace(self, project_id: str, assignee: str, cycle_id: str = "", include_completed: bool = False) -> dict[str, Any]:
        cycles = {c["id"]: c for c in self.store.list("test_cycles", filters={"project_id": project_id}, page_size=1_000_000)["items"]}
        shown_cycles = {cid: c for cid, c in cycles.items() if include_completed or c.get("status") != "Completed"}
        runs = [r for r in self.store.list("test_runs", filters={"project_id": project_id}, page_size=1_000_000)["items"] if r.get("cycle_id") in shown_cycles]
        if assignee == "__none": runs = [r for r in runs if not r.get("assignee_id")]
        elif assignee != "__all": runs = [r for r in runs if r.get("assignee_id") == assignee]
        if cycle_id: runs = [r for r in runs if r.get("cycle_id") == cycle_id]
        names = {u["id"]: u.get("name", "") for u in self.store.list("users", page_size=1_000_000, include_archived=True)["items"]}
        counts = {status: 0 for status in RUN_STATUSES}
        for run in runs: counts[run.get("status") or "Not Run"] = counts.get(run.get("status") or "Not Run", 0) + 1
        executed = sum(counts[s] for s in EXECUTED)
        rows = sorted(runs, key=lambda r: (r.get("status") not in OPEN, shown_cycles[r["cycle_id"]].get("name", ""), str(r.get("module_path", "")), str(r.get("case_key", ""))))
        return {
            "runs": [{**{k: r.get(k, "") for k in ("id", "cycle_id", "case_key", "case_title", "module_path", "priority", "status", "assignee_id", "assignment_source", "test_case_version", "executed_at", "defect_ids", "attempt_count")},
                      "cycle_name": shown_cycles[r["cycle_id"]].get("name", ""), "cycle_status": shown_cycles[r["cycle_id"]].get("status", ""),
                      "assignee_name": names.get(r.get("assignee_id", ""), "")} for r in rows],
            "counts": counts, "total": len(runs), "executed": executed,
            "completion": round(executed * 100 / len(runs), 1) if runs else 0.0,
            "cycles": [{"id": c["id"], "name": c.get("name", ""), "status": c.get("status", "")} for c in sorted(shown_cycles.values(), key=lambda c: str(c.get("created_at", "")), reverse=True)],
        }
