"""Historical comparison of test cycles.

Test cases are matched by their stable record ID (never by title).  Each cell
keeps the version the cycle executed, so definition changes are visible.
Classification compares a chosen previous and current cycle; the matrix and
trends cover every selected cycle in chronological order.
"""
from __future__ import annotations

import io
import tempfile
from pathlib import Path
from typing import Any

from .storage import ExcelStorageService, XlsxCodec

EXECUTED = {"Passed", "Failed", "Blocked", "Not Applicable"}
CATEGORIES = {
    "regression": "Regression (passed → failed)",
    "new_failure": "New failure",
    "repeated_failure": "Repeated failure",
    "fixed": "Fixed (failed → passed)",
    "consistently_passing": "Consistently passing",
    "passing": "Passing",
    "blocked": "Blocked",
    "not_executed": "Not executed",
    "new_case": "Newly added",
    "returned": "Back in scope",
    "removed": "Removed",
    "elsewhere": "Only in other selected cycles",
}


def _status(cell: dict[str, Any] | None) -> str:
    return (cell or {}).get("status") or "Not Run"


class ComparisonService:
    def __init__(self, store: ExcelStorageService):
        self.store = store

    def _ordered_cycles(self, project_id: str, cycle_ids: list[str]) -> list[dict[str, Any]]:
        cycles = []
        for cycle_id in dict.fromkeys(cycle_ids):
            cycle = self.store.get("test_cycles", cycle_id)
            if not cycle or cycle.get("project_id") != project_id: raise ValueError("Choose cycles from the selected project")
            cycles.append(cycle)
        if len(cycles) < 2: raise ValueError("Select at least two cycles to compare")
        if len(cycles) > 12: raise ValueError("Compare at most 12 cycles at once")
        return sorted(cycles, key=lambda c: (str(c.get("start_date") or "") or str(c.get("created_at") or "")[:10], str(c.get("created_at") or "")))

    def default_cycle_ids(self, project_id: str, count: int = 3) -> list[str]:
        """The latest cycles that have at least one recorded result (fallback: the latest cycles)."""
        cycles = self.store.list("test_cycles", filters={"project_id": project_id}, page_size=1_000_000)["items"]
        executed = {r["cycle_id"] for r in self.store.list("test_runs", filters={"project_id": project_id}, page_size=1_000_000)["items"] if (r.get("status") or "Not Run") in EXECUTED}
        order = lambda c: (str(c.get("start_date") or "") or str(c.get("created_at") or "")[:10], str(c.get("created_at") or ""))
        with_results = sorted((c for c in cycles if c["id"] in executed), key=order)
        chosen = with_results[-count:] if len(with_results) >= 2 else sorted(cycles, key=order)[-count:]
        return [c["id"] for c in chosen]

    def compare(self, project_id: str, cycle_ids: list[str], previous_id: str = "", current_id: str = "") -> dict[str, Any]:
        if not self.store.get("projects", project_id): raise ValueError("Select a valid project")
        cycle_ids = cycle_ids or self.default_cycle_ids(project_id)
        cycles = self._ordered_cycles(project_id, cycle_ids)
        ids = [c["id"] for c in cycles]
        runs_by_cycle = {c["id"]: self.store.list("test_runs", filters={"cycle_id": c["id"]}, page_size=1_000_000)["items"] for c in cycles}
        with_results = [cid for cid in ids if any((r.get("status") or "Not Run") in EXECUTED for r in runs_by_cycle[cid])]
        if not previous_id and not current_id:
            # Default: the two latest selected cycles that have results (comparing an empty cycle says nothing).
            pair = with_results[-2:] if len(with_results) >= 2 else ids[-2:]
            previous_id, current_id = pair[0], pair[1]
        previous_id = previous_id or (ids[-2] if current_id != ids[-2] else ids[-1])
        current_id = current_id or ids[-1]
        if previous_id not in ids or current_id not in ids or previous_id == current_id:
            raise ValueError("Choose two different selected cycles as previous and current")
        if ids.index(previous_id) > ids.index(current_id): previous_id, current_id = current_id, previous_id
        names = {u["id"]: u.get("name", "") for u in self.store.list("users", page_size=1_000_000, include_archived=True)["items"]}
        cases = {c["id"]: c for c in self.store.list("test_cases", filters={"project_id": project_id}, page_size=1_000_000, include_archived=True)["items"]}
        rows: dict[str, dict[str, Any]] = {}
        for cycle in cycles:
            for run in runs_by_cycle[cycle["id"]]:
                case_id = run.get("test_case_id", "")
                row = rows.setdefault(case_id, {"test_case_id": case_id, "cells": {}})
                row["cells"][cycle["id"]] = {
                    "run_id": run["id"], "status": run.get("status") or "Not Run", "version": str(run.get("test_case_version") or ""),
                    "assignee_id": run.get("assignee_id", ""), "assignee_name": names.get(run.get("assignee_id", ""), ""),
                    "executed_at": run.get("executed_at", ""), "defect_ids": run.get("defect_ids") or [],
                    "case_key": run.get("case_key", ""), "title": run.get("case_title", ""), "module_path": run.get("module_path", ""),
                }
        result_rows = []
        for case_id, row in rows.items():
            cells = row["cells"]
            latest = cells[[cid for cid in ids if cid in cells][-1]]
            case = cases.get(case_id, {})
            # Identity comes from the record ID; display from the newest snapshot (falling back to the repository).
            row.update(case_key=latest.get("case_key") or case.get("case_key", ""), title=latest.get("title") or case.get("title", ""),
                       module_path=latest.get("module_path", ""), archived=case.get("archived") is True)
            row.update(self._classify(cells, ids, previous_id, current_id))
            result_rows.append(row)
        result_rows.sort(key=lambda r: (str(r["module_path"]), str(r["case_key"])))
        summary: dict[str, int] = {key: 0 for key in CATEGORIES}
        modified = reopened = 0
        for row in result_rows:
            summary[row["category"]] += 1
            modified += "modified" in row["flags"]
            reopened += "reopened" in row["flags"]
        return {
            "cycles": [{**{k: c.get(k, "") for k in ("id", "name", "testing_type", "status", "start_date", "end_date", "build", "environment", "created_at")}, "has_results": c["id"] in with_results} for c in cycles],
            "previous_id": previous_id, "current_id": current_id, "rows": result_rows,
            "summary": summary, "modified": modified, "reopened": reopened, "labels": CATEGORIES,
            "trend": [self._cycle_stats(c, rows) for c in cycles],
        }

    @staticmethod
    def _classify(cells: dict[str, dict[str, Any]], ids: list[str], previous_id: str, current_id: str) -> dict[str, Any]:
        present = [cid for cid in ids if cid in cells]
        statuses = [_status(cells[cid]) for cid in present]
        prev, cur = cells.get(previous_id), cells.get(current_id)
        p, c = _status(prev), _status(cur)
        flags: list[str] = []
        if prev and cur and prev.get("version") != cur.get("version"): flags.append("modified")
        executed = [s for s in statuses if s in EXECUTED]
        flips = sum(1 for a, b in zip(executed, executed[1:]) if {a, b} == {"Passed", "Failed"})
        seen_fail = seen_pass_after_fail = False
        for status in executed:
            if status == "Failed" and seen_pass_after_fail: flags.append("reopened"); break
            if status == "Failed": seen_fail = True
            elif status == "Passed" and seen_fail: seen_pass_after_fail = True
        if cur and not prev:
            earlier = [cid for cid in present if ids.index(cid) < ids.index(current_id)]
            category = "returned" if earlier else "new_case"
        elif prev and not cur:
            category = "removed"
        elif not prev and not cur:
            category = "elsewhere"
        elif c in {"Not Run", "In Progress"}:
            category = "not_executed"
        elif c == "Failed":
            category = "regression" if p == "Passed" else "repeated_failure" if p == "Failed" else "new_failure"
        elif c == "Passed":
            if p == "Failed": category = "fixed"
            elif len(executed) >= 2 and all(s in {"Passed", "Not Applicable"} for s in executed) and "Passed" in executed[:-1]: category = "consistently_passing"
            else: category = "passing"
        elif c == "Blocked":
            category = "blocked"
        else:  # Not Applicable
            category = "passing"
        return {"category": category, "flags": flags, "flips": flips, "previous_status": p if prev else "", "current_status": c if cur else ""}

    @staticmethod
    def _cycle_stats(cycle: dict[str, Any], rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
        counts = {s: 0 for s in ("Passed", "Failed", "Blocked", "Not Applicable", "Not Run", "In Progress")}
        for row in rows.values():
            cell = row["cells"].get(cycle["id"])
            if cell: counts[cell["status"]] = counts.get(cell["status"], 0) + 1
        total = sum(counts.values()); executed = sum(counts[s] for s in EXECUTED)
        applicable = executed - counts["Not Applicable"]
        return {"cycle_id": cycle["id"], "name": cycle.get("name", ""), "total": total, "executed": executed, "counts": counts,
                "completion": round(executed * 100 / total, 1) if total else 0.0,
                "pass_rate": round(counts["Passed"] * 100 / applicable, 1) if applicable else 0.0}

    def export_xlsx(self, data: dict[str, Any], temp_dir: Path) -> bytes:
        cycles = data["cycles"]
        columns: list[str] = []
        for cycle in cycles:  # distinct header per cycle even when names repeat
            name = cycle["name"] or "Cycle"; candidate = name; n = 2
            while candidate in columns: candidate = f"{name} ({n})"; n += 1
            columns.append(candidate)
        headers = ("Test Case ID", "Title", "Module", "Category", "Definition changed", "Reopened", *columns)
        rows = []
        for row in data["rows"]:
            record = {"Test Case ID": row["case_key"], "Title": row["title"], "Module": row["module_path"], "Category": CATEGORIES[row["category"]],
                      "Definition changed": "Yes" if "modified" in row["flags"] else "", "Reopened": "Yes" if "reopened" in row["flags"] else ""}
            for cycle, column in zip(cycles, columns):
                cell = row["cells"].get(cycle["id"])
                record[column] = f"{cell['status']} (v{cell['version']})" if cell else "—"
            rows.append(record)
        with tempfile.NamedTemporaryFile(suffix=".xlsx", dir=temp_dir, delete=False) as handle:
            path = Path(handle.name)
        try:
            XlsxCodec.write(path, headers, rows)
            return path.read_bytes()
        finally:
            path.unlink(missing_ok=True)

    # ------------------------------------------------------------- histories
    def case_history(self, project_id: str, case_id: str, execution: Any) -> dict[str, Any]:
        """One test case in every cycle of the project: result, version, people, evidence and attempts."""
        case = self.store.get("test_cases", case_id)
        if not case or case.get("project_id") != project_id: raise KeyError("Test case not found")
        names = {u["id"]: u.get("name", "") for u in self.store.list("users", page_size=1_000_000, include_archived=True)["items"]}
        cycles = {c["id"]: c for c in self.store.list("test_cycles", filters={"project_id": project_id}, page_size=1_000_000, include_archived=True)["items"]}
        runs = [r for r in self.store.list("test_runs", filters={"test_case_id": case_id}, page_size=1_000_000, include_archived=True)["items"] if r.get("cycle_id") in cycles]
        order = lambda r: (str(cycles[r["cycle_id"]].get("start_date") or "") or str(cycles[r["cycle_id"]].get("created_at") or "")[:10], str(cycles[r["cycle_id"]].get("created_at") or ""))
        entries = []
        for run in sorted(runs, key=order, reverse=True):
            cycle = cycles[run["cycle_id"]]
            attempts = execution.history(run["id"])
            entries.append({
                "run_id": run["id"], "cycle_id": cycle["id"], "cycle_name": cycle.get("name", ""), "cycle_status": cycle.get("status", ""),
                "cycle_date": str(cycle.get("start_date") or "") or str(cycle.get("created_at") or "")[:10], "testing_type": cycle.get("testing_type", ""),
                "environment": run.get("environment") or cycle.get("environment", ""), "build": run.get("build") or cycle.get("build", ""),
                "status": run.get("status") or "Not Run", "version": str(run.get("test_case_version") or ""), "title": run.get("case_title") or case.get("title", ""),
                "module_path": run.get("module_path", ""), "assignee_name": names.get(run.get("assignee_id", ""), ""), "executed_by_name": names.get(run.get("executed_by", ""), ""),
                "executed_at": run.get("executed_at", ""), "actual_result": run.get("actual_result", ""), "notes": run.get("execution_notes", ""),
                "defect_ids": run.get("defect_ids") or [], "attempt_count": run.get("attempt_count") or "0", "archived": run.get("archived") is True,
                "attempts": attempts, "evidence": [a for attempt in attempts for a in attempt.get("attachments", [])],
            })
        executed = [e for e in entries if e["status"] in EXECUTED]
        return {"case": {k: case.get(k, "") for k in ("id", "case_key", "title", "version", "status", "priority")}, "cycles": entries,
                "summary": {"cycles": len(entries), "executed": len(executed), "passed": sum(e["status"] == "Passed" for e in executed),
                            "failed": sum(e["status"] == "Failed" for e in executed), "blocked": sum(e["status"] == "Blocked" for e in executed),
                            "defects": sorted({d for e in entries for d in (e["defect_ids"] if isinstance(e["defect_ids"], list) else [])})}}

    def cycle_timeline(self, cycle_id: str, limit: int = 500) -> dict[str, Any]:
        """Everything recorded in a cycle, newest first: results (every attempt) and assignment changes."""
        cycle = self.store.get("test_cycles", cycle_id)
        if not cycle: raise KeyError("Cycle not found")
        names = {u["id"]: u.get("name", "") for u in self.store.list("users", page_size=1_000_000, include_archived=True)["items"]}
        runs = {r["id"]: r for r in self.store.list("test_runs", filters={"cycle_id": cycle_id}, page_size=1_000_000, include_archived=True)["items"]}
        cases = {c["id"]: c for c in self.store.list("test_cases", filters={"project_id": cycle.get("project_id", "")}, page_size=1_000_000, include_archived=True)["items"]}
        for run in runs.values():  # runs created before snapshots: take ID/title from the repository
            case = cases.get(run.get("test_case_id", ""), {})
            run.setdefault("case_key", ""); run.setdefault("case_title", "")
            if not run["case_key"]: run["case_key"] = case.get("case_key", "")
            if not run["case_title"]: run["case_title"] = case.get("title", "")
        events = []
        for entry in self.store.list("execution_history", filters={"cycle_id": cycle_id}, page_size=1_000_000)["items"]:
            run = runs.get(entry.get("test_run_id"), {})
            events.append({"kind": "result", "at": entry.get("executed_at", ""), "run_id": entry.get("test_run_id", ""), "case_id": run.get("test_case_id", ""),
                           "case_key": run.get("case_key") or entry.get("case_key", ""), "title": run.get("case_title", ""), "status": entry.get("status", ""),
                           "previous_status": entry.get("previous_status", ""), "attempt": entry.get("attempt", ""), "actual_result": entry.get("actual_result", ""),
                           "notes": entry.get("notes", ""), "defect_ids": entry.get("defect_ids") or [], "by": names.get(entry.get("executed_by", ""), "") or "System",
                           "source": entry.get("source", "")})
        for entry in self.store.list("assignment_history", filters={"cycle_id": cycle_id}, page_size=1_000_000)["items"]:
            run = runs.get(entry.get("test_run_id"), {})
            events.append({"kind": "assignment", "at": entry.get("created_at", ""), "run_id": entry.get("test_run_id", ""), "case_id": run.get("test_case_id", ""),
                           "case_key": entry.get("case_key") or run.get("case_key", ""), "title": run.get("case_title", ""),
                           "from": names.get(entry.get("from_assignee_id", ""), "") or "Unassigned", "to": names.get(entry.get("to_assignee_id", ""), "") or "Unassigned",
                           "reason": entry.get("reason", ""), "by": names.get(entry.get("actor_id", ""), "") or "System"})
        # Results recorded before execution history existed: show the run's current result once.
        logged = {e["run_id"] for e in events if e["kind"] == "result"}
        for run in runs.values():
            if run["id"] not in logged and (run.get("status") or "Not Run") in EXECUTED:
                events.append({"kind": "result", "at": run.get("executed_at") or run.get("completed_at") or run.get("updated_at", ""), "run_id": run["id"], "case_id": run.get("test_case_id", ""),
                               "case_key": run.get("case_key", ""), "title": run.get("case_title", ""), "status": run.get("status", ""), "previous_status": "", "attempt": "",
                               "actual_result": run.get("actual_result", ""), "notes": run.get("execution_notes", ""), "defect_ids": run.get("defect_ids") or [],
                               "by": names.get(run.get("executed_by", ""), "") or "—", "source": "earlier"})
        events.sort(key=lambda e: str(e["at"]), reverse=True)
        stats = self._cycle_stats(cycle, {r["test_case_id"]: {"cells": {cycle_id: {"status": r.get("status") or "Not Run"}}} for r in runs.values() if r.get("archived") is not True})
        return {"cycle": {k: cycle.get(k, "") for k in ("id", "name", "status", "testing_type", "environment", "build", "start_date", "end_date", "created_at", "description")},
                "stats": stats, "events": events[:limit], "total_events": len(events)}
