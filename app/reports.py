"""Dashboard metrics and Excel reports, computed from the workbooks on request.

Scope rules (shown in the UI):
* Cycles: the chosen cycles, else the cycles that start (or were created) inside the
  date range, else every active (not Completed) cycle.
* Module: a module path and everything below it, using each run's snapshot path.
* Member: the test case's current assignee.
* Date range also bounds the "executions per day" activity and the trend window.
"""
from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .comparison import ComparisonService
from .storage import ExcelStorageService, XlsxCodec, utcnow

EXECUTED = {"Passed", "Failed", "Blocked", "Not Applicable"}
OPEN = {"Not Run", "In Progress"}
STATUSES = ("Passed", "Failed", "Blocked", "Not Applicable", "In Progress", "Not Run")


def _cycle_date(cycle: dict[str, Any]) -> str:
    return str(cycle.get("start_date") or "") or str(cycle.get("created_at") or "")[:10]


def _in_module(path: str, module: str) -> bool:
    return not module or path == module or path.startswith(module.rstrip("/") + "/")


def _counts(runs: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {s: 0 for s in STATUSES}
    for run in runs: counts[run.get("status") or "Not Run"] = counts.get(run.get("status") or "Not Run", 0) + 1
    total = len(runs); executed = sum(counts[s] for s in EXECUTED); applicable = executed - counts["Not Applicable"]
    return {"total": total, "executed": executed, "open": counts["Not Run"] + counts["In Progress"], "counts": counts,
            "completion": round(executed * 100 / total, 1) if total else 0.0,
            "pass_rate": round(counts["Passed"] * 100 / applicable, 1) if applicable else 0.0}


class ReportService:
    def __init__(self, store: ExcelStorageService, workflow: Any):
        self.store = store
        self.workflow = workflow
        self.comparison = ComparisonService(store)

    # ------------------------------------------------------------------ scope
    def _scope(self, project_id: str, f: dict[str, Any]) -> dict[str, Any]:
        if not self.store.get("projects", project_id): raise ValueError("Select a valid project")
        date_from, date_to = str(f.get("date_from") or ""), str(f.get("date_to") or "")
        for value in (date_from, date_to):
            if value:
                try: date.fromisoformat(value)
                except ValueError as exc: raise ValueError("Dates must be YYYY-MM-DD") from exc
        if date_from and date_to and date_to < date_from: raise ValueError("The end date cannot be before the start date")
        cycles = self.store.list("test_cycles", filters={"project_id": project_id}, page_size=1_000_000)["items"]
        cycle_ids = [c for c in str(f.get("cycle_ids") or "").split(",") if c]
        if cycle_ids:
            known = {c["id"] for c in cycles}
            if any(c not in known for c in cycle_ids): raise ValueError("Choose cycles from this project")
            scoped = [c for c in cycles if c["id"] in cycle_ids]; basis = "selected"
        elif f.get("cycle_scope") == "all":
            scoped = cycles; basis = "all"
        elif date_from or date_to:
            scoped = [c for c in cycles if (not date_from or _cycle_date(c) >= date_from) and (not date_to or _cycle_date(c) <= date_to)]; basis = "dates"
        else:
            scoped = [c for c in cycles if c.get("status") != "Completed"]; basis = "active"
        ids = {c["id"] for c in scoped}
        module, member = str(f.get("module") or ""), str(f.get("member") or "")
        runs = [r for r in self.store.list("test_runs", filters={"project_id": project_id}, page_size=1_000_000)["items"]
                if r["cycle_id"] in ids and _in_module(str(r.get("module_path") or ""), module)
                and (not member or (member == "__none" and not r.get("assignee_id")) or r.get("assignee_id") == member)]
        return {"cycles": scoped, "all_cycles": cycles, "runs": runs, "basis": basis, "module": module, "member": member,
                "date_from": date_from, "date_to": date_to}

    # -------------------------------------------------------------- dashboard
    def dashboard(self, project_id: str, f: dict[str, Any]) -> dict[str, Any]:
        scope = self._scope(project_id, f)
        runs, cycles = scope["runs"], scope["cycles"]
        names = {u["id"]: u.get("name", "") for u in self.store.list("users", page_size=1_000_000, include_archived=True)["items"]}
        cases = self.store.list("test_cases", filters={"project_id": project_id}, page_size=1_000_000)["items"]
        folders = self.workflow._folders(project_id)
        approval = {s: 0 for s in ("Draft", "In Review", "Approved", "Rejected", "Deprecated")}
        for case in cases: approval[case.get("status") or "Draft"] = approval.get(case.get("status") or "Draft", 0) + 1
        all_cycles = scope["all_cycles"]
        execution = _counts(runs)

        # Module progress: every ancestor path of each run's snapshot module.
        modules: dict[str, dict[str, Any]] = {}
        for run in runs:
            parts = [p for p in str(run.get("module_path") or "").split("/") if p] or ["(no module)"]
            for depth in range(1, len(parts) + 1):
                path = "/" + "/".join(parts[:depth])
                modules.setdefault(path, {"path": path, "name": parts[depth - 1], "depth": depth, "runs": []})["runs"].append(run)
        module_rows = [{k: v for k, v in m.items() if k != "runs"} | _counts(m["runs"]) for m in sorted(modules.values(), key=lambda m: m["path"])]

        # Member progress in scope, plus open workload across all active cycles of the project.
        active_ids = {c["id"] for c in all_cycles if c.get("status") != "Completed"}
        project_runs = self.store.list("test_runs", filters={"project_id": project_id}, page_size=1_000_000)["items"]
        by_member: dict[str, list[dict[str, Any]]] = {}
        for run in runs: by_member.setdefault(run.get("assignee_id") or "", []).append(run)
        member_rows = []
        for member_id, member_runs in by_member.items():
            member_rows.append({"id": member_id, "name": names.get(member_id, "Former member") if member_id else "Unassigned", **_counts(member_runs),
                                "workload": sum(1 for r in project_runs if r.get("cycle_id") in active_ids and r.get("assignee_id") == member_id and (r.get("status") or "Not Run") in OPEN) if member_id else None})
        member_rows.sort(key=lambda m: (m["id"] == "", -m["total"], m["name"].casefold()))

        trend = self._trend(project_id, scope)
        latest = trend[-1] if trend else None
        failures = sorted((r for r in runs if r.get("status") == "Failed"), key=lambda r: str(r.get("executed_at") or ""), reverse=True)[:10]
        cycle_names = {c["id"]: c.get("name", "") for c in all_cycles}
        return {
            "scope": {"basis": scope["basis"], "cycles": [{"id": c["id"], "name": c.get("name", ""), "status": c.get("status", ""), "date": _cycle_date(c)} for c in sorted(cycles, key=_cycle_date)],
                      "module": scope["module"], "member": scope["member"], "date_from": scope["date_from"], "date_to": scope["date_to"]},
            "repository": {"test_cases": len(cases), "approval": approval, "modules": len(folders),
                           "modules_pending": sum(1 for f in folders.values() if f.get("approval_status") not in ("", None, "Approved"))},
            "cycles": {"active": sum(1 for c in all_cycles if c.get("status") != "Completed"), "completed": sum(1 for c in all_cycles if c.get("status") == "Completed"),
                       "not_started": sum(1 for c in all_cycles if c.get("status") in ("", None, "Not Started")), "in_scope": len(cycles)},
            "execution": execution, "modules": module_rows, "members": member_rows, "trend": trend,
            "latest": {"cycle": latest["name"], "new_failures": latest["new_failures"], "regressions": latest["regressions"], "reopened": latest["reopened"], "fixed": latest["fixed"]} if latest else None,
            "activity": self._activity(project_id, scope),
            "recent_failures": [{"run_id": r["id"], "case_key": r.get("case_key", ""), "title": r.get("case_title", ""), "module_path": r.get("module_path", ""),
                                 "cycle": cycle_names.get(r["cycle_id"], ""), "assignee": names.get(r.get("assignee_id", ""), ""), "executed_at": r.get("executed_at", ""),
                                 "defect_ids": r.get("defect_ids") or [], "actual_result": str(r.get("actual_result") or "")[:200]} for r in failures],
            "generated_at": utcnow(),
        }

    def _trend(self, project_id: str, scope: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
        """Regressions, new failures, reopened and fixed per cycle versus the cycle before it."""
        runs = self.store.list("test_runs", filters={"project_id": project_id}, page_size=1_000_000)["items"]
        with_results = {r["cycle_id"] for r in runs if (r.get("status") or "Not Run") in EXECUTED}
        pool = scope["cycles"] if scope["basis"] in ("selected", "dates") else scope["all_cycles"]
        ordered = sorted((c for c in pool if c["id"] in with_results), key=lambda c: (_cycle_date(c), str(c.get("created_at") or "")))[-limit:]
        trend: list[dict[str, Any]] = []
        for index, cycle in enumerate(ordered):
            point = {"cycle_id": cycle["id"], "name": cycle.get("name", ""), "date": _cycle_date(cycle), "regressions": 0, "new_failures": 0, "reopened": 0, "fixed": 0}
            mine = [r for r in runs if r["cycle_id"] == cycle["id"] and _in_module(str(r.get("module_path") or ""), scope["module"])
                    and (not scope["member"] or r.get("assignee_id") == scope["member"] or (scope["member"] == "__none" and not r.get("assignee_id")))]
            stats = _counts(mine); point.update(pass_rate=stats["pass_rate"], executed=stats["executed"], total=stats["total"])
            if index:
                window = [c["id"] for c in ordered[: index + 1]]
                data = self.comparison.compare(project_id, window, ordered[index - 1]["id"], cycle["id"])
                keep = {r["test_case_id"] for r in mine}
                for row in data["rows"]:
                    if row["test_case_id"] not in keep: continue
                    point["regressions"] += row["category"] == "regression"
                    point["new_failures"] += row["category"] == "new_failure"
                    point["fixed"] += row["category"] == "fixed"
                    point["reopened"] += "reopened" in row["flags"] and row.get("current_status") == "Failed"
            trend.append(point)
        return trend

    def _activity(self, project_id: str, scope: dict[str, Any]) -> list[dict[str, Any]]:
        end = date.fromisoformat(scope["date_to"]) if scope["date_to"] else date.today()
        start = date.fromisoformat(scope["date_from"]) if scope["date_from"] else end - timedelta(days=13)
        if (end - start).days > 92: start = end - timedelta(days=92)
        days = {(start + timedelta(days=i)).isoformat(): 0 for i in range((end - start).days + 1)}
        cycle_ids = {c["id"] for c in scope["cycles"]}
        run_ids = {r["id"] for r in scope["runs"]}
        for entry in self.store.list("execution_history", filters={"project_id": project_id}, page_size=1_000_000)["items"]:
            day = str(entry.get("executed_at") or "")[:10]
            if day in days and entry.get("cycle_id") in cycle_ids and entry.get("test_run_id") in run_ids and entry.get("status") in EXECUTED:
                days[day] += 1
        return [{"date": d, "count": n} for d, n in days.items()]

    # ---------------------------------------------------------------- reports
    def report_rows(self, kind: str, project_id: str, f: dict[str, Any]) -> tuple[tuple[str, ...], list[dict[str, Any]], str]:
        names = {u["id"]: u.get("name", "") for u in self.store.list("users", page_size=1_000_000, include_archived=True)["items"]}
        if kind == "execution":
            scope = self._scope(project_id, f)
            cycles = {c["id"]: c.get("name", "") for c in scope["all_cycles"]}
            headers = ("Cycle", "Test Case ID", "Title", "Module", "Priority", "Version", "Assigned To", "Status", "Executed By", "Executed At", "Attempts", "Actual Result", "Notes", "Defects")
            rows = [{"Cycle": cycles.get(r["cycle_id"], ""), "Test Case ID": r.get("case_key", ""), "Title": r.get("case_title", ""), "Module": r.get("module_path", ""),
                     "Priority": r.get("priority", ""), "Version": r.get("test_case_version", ""), "Assigned To": names.get(r.get("assignee_id", ""), ""),
                     "Status": r.get("status") or "Not Run", "Executed By": names.get(r.get("executed_by", ""), ""), "Executed At": r.get("executed_at", ""),
                     "Attempts": r.get("attempt_count") or "0", "Actual Result": r.get("actual_result", ""), "Notes": r.get("execution_notes", ""),
                     "Defects": ", ".join(r.get("defect_ids") or []) if isinstance(r.get("defect_ids"), list) else r.get("defect_ids", "")}
                    for r in sorted(scope["runs"], key=lambda r: (cycles.get(r["cycle_id"], ""), str(r.get("module_path", "")), str(r.get("case_key", ""))))]
            return headers, rows, "TMExpert_Execution_Report.xlsx"
        data = self.dashboard(project_id, f)
        if kind == "modules":
            headers = ("Module", "Test Cases", "Executed", "Completion %", "Passed", "Failed", "Blocked", "Not Applicable", "Not Run", "In Progress", "Pass Rate %")
            rows = [{"Module": m["path"], "Test Cases": m["total"], "Executed": m["executed"], "Completion %": m["completion"], "Passed": m["counts"]["Passed"],
                     "Failed": m["counts"]["Failed"], "Blocked": m["counts"]["Blocked"], "Not Applicable": m["counts"]["Not Applicable"],
                     "Not Run": m["counts"]["Not Run"], "In Progress": m["counts"]["In Progress"], "Pass Rate %": m["pass_rate"]} for m in data["modules"]]
            return headers, rows, "TMExpert_Module_Summary.xlsx"
        if kind == "members":
            headers = ("Member", "Assigned", "Executed", "Completion %", "Passed", "Failed", "Blocked", "Open", "Pass Rate %", "Open Across Active Cycles")
            rows = [{"Member": m["name"], "Assigned": m["total"], "Executed": m["executed"], "Completion %": m["completion"], "Passed": m["counts"]["Passed"],
                     "Failed": m["counts"]["Failed"], "Blocked": m["counts"]["Blocked"], "Open": m["open"], "Pass Rate %": m["pass_rate"],
                     "Open Across Active Cycles": "" if m["workload"] is None else m["workload"]} for m in data["members"]]
            return headers, rows, "TMExpert_Member_Summary.xlsx"
        if kind == "defects":
            scope = self._scope(project_id, f)
            cycles = {c["id"]: c.get("name", "") for c in scope["all_cycles"]}
            headers = ("Defect", "Test Case ID", "Title", "Module", "Cycle", "Current Status", "Assigned To", "Executed At")
            rows = []
            for r in scope["runs"]:
                defects = r.get("defect_ids") or []
                for defect in defects if isinstance(defects, list) else [d.strip() for d in str(defects).split(",") if d.strip()]:
                    rows.append({"Defect": defect, "Test Case ID": r.get("case_key", ""), "Title": r.get("case_title", ""), "Module": r.get("module_path", ""),
                                 "Cycle": cycles.get(r["cycle_id"], ""), "Current Status": r.get("status") or "Not Run", "Assigned To": names.get(r.get("assignee_id", ""), ""), "Executed At": r.get("executed_at", "")})
            rows.sort(key=lambda r: (r["Defect"], r["Cycle"], r["Test Case ID"]))
            return headers, rows, "TMExpert_Defects_Report.xlsx"
        raise ValueError("Unknown report")

    def export(self, kind: str, project_id: str, f: dict[str, Any], temp_dir: Path) -> tuple[bytes, str]:
        headers, rows, filename = self.report_rows(kind, project_id, f)
        with tempfile.NamedTemporaryFile(suffix=".xlsx", dir=temp_dir, delete=False) as handle:
            path = Path(handle.name)
        try:
            XlsxCodec.write(path, headers, rows)
            return path.read_bytes(), filename
        finally:
            path.unlink(missing_ok=True)
