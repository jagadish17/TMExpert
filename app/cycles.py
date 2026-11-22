"""Test cycle creation: eligibility, module selection, preview and snapshots.

A cycle holds one test run per test case.  Each run is pinned to the case's
version at the time it was added (steps per version and Test_Case_Versions are
append-only) and keeps the case ID, title, module path, priority and type as
they were then, so later repository changes never alter an existing cycle.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from .storage import ExcelStorageService, utcnow
from .workflow import RepositoryWorkflow

TESTING_TYPES = ("Regression", "Sanity", "Smoke", "Functional", "Retesting", "Integration", "UAT", "Custom")
CYCLE_STATUSES = ("Not Started", "In Progress", "Completed")
EXECUTED = {"Passed", "Failed", "Blocked", "Not Applicable"}
RUN_STATUSES = ("Not Run", "In Progress", "Passed", "Failed", "Blocked", "Not Applicable")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _ids(value: Any) -> list[str]:
    if value in (None, ""): return []
    if not isinstance(value, list): raise ValueError("Selections must be lists of IDs")
    return [str(item) for item in dict.fromkeys(value) if str(item)]


class CycleService:
    def __init__(self, store: ExcelStorageService, workflow: RepositoryWorkflow):
        self.store = store
        self.workflow = workflow

    # ------------------------------------------------------------ eligibility
    def _folders(self, project_id: str) -> dict[str, dict[str, Any]]:
        return self.workflow._folders(project_id)
    def _subtree(self, folder_id: str, folders: dict[str, dict[str, Any]]) -> set[str]:
        children: dict[str, list[str]] = {}
        for folder in folders.values(): children.setdefault(str(folder.get("parent_id") or ""), []).append(folder["id"])
        found: set[str] = set(); stack = [folder_id]
        while stack:
            current = stack.pop()
            if current in found: continue
            found.add(current); stack.extend(children.get(current, []))
        return found
    def ineligible_reason(self, case: dict[str, Any], folders: dict[str, dict[str, Any]], include_unapproved: bool) -> str:
        status = case.get("status") or "Draft"
        if case.get("archived") is True: return "Archived"
        if status == "Deprecated": return "Deprecated"
        if status == "Rejected" and self.workflow.approvals_enabled: return "Rejected"
        if str(case.get("folder_id", "")) not in folders: return "Module archived"
        if include_unapproved or not self.workflow.approvals_enabled: return ""
        if status != "Approved": return "Not approved"
        if not self.workflow.folder_chain_approved(str(case.get("folder_id", "")), folders): return "Module not approved"
        return ""
    def candidates(self, project_id: str, include_unapproved: bool = False) -> dict[str, Any]:
        if not self.store.get("projects", project_id): raise ValueError("Select a valid project")
        folders = self._folders(project_id)
        cases = self.store.list("test_cases", filters={"project_id": project_id}, page_size=1_000_000)["items"]
        eligible, excluded = [], {}
        for case in cases:
            reason = self.ineligible_reason(case, folders, include_unapproved)
            if reason: excluded[reason] = excluded.get(reason, 0) + 1; continue
            eligible.append({key: case.get(key, "") for key in ("id", "case_key", "title", "priority", "test_type", "folder_id", "version", "status")})
        return {
            "folders": [{key: f.get(key, "") for key in ("id", "parent_id", "name", "path", "approval_status")} for f in folders.values()],
            "cases": eligible, "excluded_counts": excluded, "include_unapproved": include_unapproved,
        }

    # -------------------------------------------------------------- selection
    def resolve(self, project_id: str, selection: dict[str, Any]) -> dict[str, Any]:
        """Turn a module/case selection into the exact list of test cases to add."""
        include_unapproved = selection.get("include_unapproved") is True
        folders = self._folders(project_id)
        module_ids = _ids(selection.get("module_ids"))
        include_ids = _ids(selection.get("include_case_ids"))
        suite_ids = _ids(selection.get("suite_ids"))
        for suite_id in suite_ids:  # a reusable suite contributes its modules and hand-picked cases
            suite = self.store.get("test_suites", suite_id, include_archived=False)
            if not suite or suite.get("project_id") != project_id or suite.get("kind") != "reusable":
                raise ValueError("One or more selected suites no longer exist")
            module_ids += [m for m in _ids(suite.get("module_ids") or []) if m in folders and m not in module_ids]
            include_ids += [c for c in _ids(suite.get("case_ids") or []) if c not in include_ids]
        exclude_ids = set(_ids(selection.get("exclude_case_ids")))
        unknown = [m for m in module_ids if m not in folders]
        if unknown: raise ValueError("One or more selected modules no longer exist")
        covered: set[str] = set()
        for module_id in module_ids: covered |= self._subtree(module_id, folders)
        cases = {c["id"]: c for c in self.store.list("test_cases", filters={"project_id": project_id}, page_size=1_000_000)["items"]}
        chosen: list[dict[str, Any]] = []; skipped: list[dict[str, Any]] = []; rule_excluded: dict[str, int] = {}
        seen: set[str] = set()
        for case in sorted(cases.values(), key=lambda c: (folders.get(c.get("folder_id", ""), {}).get("path", ""), str(c.get("case_key", "")))):
            if case["id"] in exclude_ids or case.get("folder_id") not in covered: continue
            reason = self.ineligible_reason(case, folders, include_unapproved)
            if reason: rule_excluded[reason] = rule_excluded.get(reason, 0) + 1; continue
            chosen.append(case); seen.add(case["id"])
        for case_id in include_ids:
            if case_id in seen or case_id in exclude_ids: continue
            case = cases.get(case_id) or self.store.get("test_cases", case_id)
            if not case or case.get("project_id") != project_id:
                skipped.append({"id": case_id, "case_key": "", "title": "", "reason": "Not in this project"}); continue
            reason = self.ineligible_reason(case, folders, include_unapproved)
            if reason:
                skipped.append({"id": case_id, "case_key": case.get("case_key", ""), "title": case.get("title", ""), "reason": reason}); continue
            chosen.append(case); seen.add(case_id)
        return {"cases": chosen, "skipped": skipped, "rule_excluded": rule_excluded, "module_ids": module_ids, "folders": folders, "suite_ids": suite_ids}

    def _preview_rows(self, cases: list[dict[str, Any]], folders: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"id": c["id"], "case_key": c.get("case_key", ""), "title": c.get("title", ""), "priority": c.get("priority", ""),
                 "test_type": c.get("test_type", ""), "version": c.get("version", ""), "status": c.get("status", ""),
                 "module_path": folders.get(c.get("folder_id", ""), {}).get("path", "")} for c in cases]
    def preview(self, project_id: str, selection: dict[str, Any], cycle_id: str = "") -> dict[str, Any]:
        resolved = self.resolve(project_id, selection)
        already: list[dict[str, Any]] = []
        cases = resolved["cases"]
        if cycle_id:
            in_cycle = self._cycle_case_ids(cycle_id)
            already = [c for c in cases if c["id"] in in_cycle]
            cases = [c for c in cases if c["id"] not in in_cycle]
        modules: dict[str, int] = {}
        for row in self._preview_rows(cases, resolved["folders"]): modules[row["module_path"]] = modules.get(row["module_path"], 0) + 1
        return {"cases": self._preview_rows(cases, resolved["folders"]), "total": len(cases), "skipped": resolved["skipped"],
                "rule_excluded": resolved["rule_excluded"], "modules": [{"path": p, "count": n} for p, n in sorted(modules.items())],
                "already_in_cycle": [{"case_key": c.get("case_key", ""), "title": c.get("title", "")} for c in already]}

    # --------------------------------------------------------------- creation
    def _validate_info(self, data: dict[str, Any], project_id: str, cycle_id: str = "") -> dict[str, Any]:
        name = str(data.get("name", "")).strip()
        if not name: raise ValueError("Cycle name is required")
        if len(name) > 150: raise ValueError("Cycle name must be 150 characters or fewer")
        if any(c["name"].casefold() == name.casefold() and c["id"] != cycle_id for c in self.store.list("test_cycles", filters={"project_id": project_id}, page_size=1_000_000)["items"]):
            raise ValueError(f"A cycle named “{name}” already exists in this project")
        testing_type = str(data.get("testing_type", "")).strip()
        if testing_type == "Custom":
            testing_type = str(data.get("custom_type", "")).strip()
            if not testing_type: raise ValueError("Enter a name for the custom testing type")
        elif testing_type not in TESTING_TYPES:
            raise ValueError("Choose a testing type")
        start, target = str(data.get("start_date", "") or ""), str(data.get("target_date", "") or "")
        for label, value in (("Start date", start), ("Target completion date", target)):
            if value:
                if not _DATE.match(value): raise ValueError(f"{label} must be a date")
                try: date.fromisoformat(value)
                except ValueError as exc: raise ValueError(f"{label} must be a valid date") from exc
        if start and target and target < start: raise ValueError("Target completion date cannot be before the start date")
        return {"name": name, "testing_type": testing_type, "description": str(data.get("description", "") or "").strip(),
                "environment": str(data.get("environment", "") or "").strip(), "build": str(data.get("build", "") or "").strip(),
                "start_date": start, "end_date": target}
    def _cycle_case_ids(self, cycle_id: str) -> set[str]:
        return {r["test_case_id"] for r in self.store.list("test_runs", filters={"cycle_id": cycle_id}, page_size=1_000_000)["items"]}
    def _run_rows(self, cycle: dict[str, Any], cases: list[dict[str, Any]], folders: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return [{
            "project_id": cycle["project_id"], "cycle_id": cycle["id"], "test_case_id": c["id"], "test_case_version": c.get("version", ""),
            "tester_id": "", "environment": cycle.get("environment", ""), "build": cycle.get("build", ""), "status": "Not Run",
            "started_at": "", "completed_at": "", "actual_result": "",
            "case_key": c.get("case_key", ""), "case_title": c.get("title", ""), "module_id": c.get("folder_id", ""),
            "module_path": folders.get(c.get("folder_id", ""), {}).get("path", ""), "priority": c.get("priority", ""), "test_type": c.get("test_type", ""),
        } for c in cases]
    def create_cycle(self, data: dict[str, Any], actor_id: str) -> dict[str, Any]:
        project_id = str(data.get("project_id", ""))
        if not self.store.get("projects", project_id): raise ValueError("Select a valid project")
        info = self._validate_info(data, project_id)
        resolved = self.resolve(project_id, data)
        if not resolved["cases"]: raise ValueError("Select at least one eligible test case for the cycle")
        plan_id = self._check_plan(project_id, data.get("plan_id"))
        source_cycle_id = str(data.get("source_cycle_id", "") or "")
        if source_cycle_id and (self.store.get("test_cycles", source_cycle_id) or {}).get("project_id") != project_id:
            raise ValueError("The source cycle must be in this project")
        cycle = self.store.create("test_cycles", {**info, "project_id": project_id, "suite_id": "", "plan_id": plan_id, "source_cycle_id": source_cycle_id,
                                                  "suite_ids": resolved["suite_ids"] or self._suite_refs(project_id, data.get("suite_refs")), "status": "Not Started",
                                                  "owner_id": actor_id if self.store.get("users", actor_id) else "", "module_ids": resolved["module_ids"]}, actor_id)
        try:
            runs = self.store.batch_create("test_runs", self._run_rows(cycle, resolved["cases"], resolved["folders"]), actor_id)
        except Exception:
            self.store.update("test_cycles", cycle["id"], {"archived": True}, actor_id)  # never leave an empty half-created cycle
            raise
        return {"cycle": cycle, "added": len(runs), "skipped": resolved["skipped"]}
    def add_cases(self, cycle_id: str, selection: dict[str, Any], actor_id: str) -> dict[str, Any]:
        cycle = self.store.get("test_cycles", cycle_id, include_archived=False)
        if not cycle: raise KeyError("Cycle not found")
        if cycle.get("status") == "Completed": raise ValueError("Reopen the cycle before adding test cases")
        resolved = self.resolve(cycle["project_id"], selection)
        in_cycle = self._cycle_case_ids(cycle_id)
        new = [c for c in resolved["cases"] if c["id"] not in in_cycle]
        already = len(resolved["cases"]) - len(new)
        if not new: raise ValueError("All selected test cases are already in this cycle" if already else "Select at least one eligible test case")
        runs = self.store.batch_create("test_runs", self._run_rows(cycle, new, resolved["folders"]), actor_id)
        modules = list(dict.fromkeys(list(cycle.get("module_ids") or []) + resolved["module_ids"]))
        if modules != list(cycle.get("module_ids") or []): self.store.update("test_cycles", cycle_id, {"module_ids": modules}, actor_id)
        return {"added": len(runs), "already_in_cycle": already, "skipped": resolved["skipped"]}
    def remove_run(self, run_id: str, actor_id: str) -> None:
        run = self.store.get("test_runs", run_id, include_archived=False)
        if not run: raise KeyError("Test run not found")
        if run.get("status") != "Not Run" or self.store.list("execution_results", filters={"test_run_id": run_id}, page_size=1)["total"]:
            raise ValueError("Only test cases that have not been executed can be removed from a cycle")
        self.store.delete("test_runs", run_id, actor_id)
    def update_cycle(self, cycle_id: str, data: dict[str, Any], actor_id: str) -> dict[str, Any]:
        cycle = self.store.get("test_cycles", cycle_id, include_archived=False)
        if not cycle: raise KeyError("Cycle not found")
        merged = {**cycle, **{k: v for k, v in data.items() if k in {"name", "testing_type", "custom_type", "description", "environment", "build", "start_date", "target_date"}}}
        if "target_date" not in data: merged["target_date"] = cycle.get("end_date", "")
        if "testing_type" not in data and cycle.get("testing_type") not in TESTING_TYPES:
            merged["testing_type"], merged["custom_type"] = "Custom", cycle.get("testing_type", "")
        changes = self._validate_info(merged, cycle["project_id"], cycle_id)
        if "plan_id" in data: changes["plan_id"] = self._check_plan(cycle["project_id"], data.get("plan_id"))
        if "status" in data:
            if data["status"] not in CYCLE_STATUSES: raise ValueError("Cycle status must be Not Started, In Progress, or Completed")
            changes["status"] = data["status"]
        return self.store.update("test_cycles", cycle_id, changes, actor_id)

    def _suite_refs(self, project_id: str, refs: Any) -> list[str]:
        """Suites a selection was pre-filled from (recorded for traceability only)."""
        try: ids = _ids(refs)
        except ValueError: return []
        return [i for i in ids if (self.store.get("test_suites", i) or {}).get("project_id") == project_id]
    def _check_plan(self, project_id: str, plan_id: Any) -> str:
        plan_id = str(plan_id or "")
        if not plan_id: return ""
        plan = self.store.get("test_plans", plan_id, include_archived=False)
        if not plan or plan.get("project_id") != project_id: raise ValueError("Choose a test plan in this project")
        return plan_id

    # ----------------------------------------------------------------- retest
    def _unique_name(self, project_id: str, base: str) -> str:
        taken = {c["name"].casefold() for c in self.store.list("test_cycles", filters={"project_id": project_id}, page_size=1_000_000, include_archived=True)["items"]}
        name, n = base[:150], 2
        while name.casefold() in taken: name = f"{base[:140]} ({n})"; n += 1
        return name

    def create_retest(self, project_id: str, source_runs: list[dict[str, Any]], data: dict[str, Any], actor_id: str, source_cycle: dict[str, Any] | None = None) -> dict[str, Any]:
        """A Retesting cycle for the given executions; each case keeps its tester."""
        if not source_runs: raise ValueError("There are no failed or blocked test cases to retest")
        base = source_cycle or {}
        name = str(data.get("name") or "").strip() or self._unique_name(project_id, f"{base.get('name') or 'Defect'} – Retest")
        info = {"project_id": project_id, "name": name, "testing_type": "Retesting", "description": str(data.get("description") or (f"Retest of {base['name']}" if base else "Retest after defect fixes")),
                "environment": str(data.get("environment", base.get("environment", "")) or ""), "build": str(data.get("build", base.get("build", "")) or ""),
                "start_date": str(data.get("start_date") or ""), "target_date": str(data.get("target_date") or ""),
                "plan_id": data.get("plan_id", base.get("plan_id", "")), "source_cycle_id": base.get("id", ""),
                "include_case_ids": list(dict.fromkeys(r["test_case_id"] for r in source_runs)), "include_unapproved": data.get("include_unapproved") is True}
        result = self.create_cycle(info, actor_id)
        previous = {r["test_case_id"]: r.get("assignee_id", "") for r in source_runs if r.get("assignee_id")}
        new_runs = self.store.list("test_runs", filters={"cycle_id": result["cycle"]["id"]}, page_size=1_000_000)["items"]
        now = utcnow()
        keep = {r["id"]: {"assignee_id": previous[r["test_case_id"]], "assignment_source": "manual", "assigned_at": now, "assigned_by": actor_id if self.store.get("users", actor_id) else ""}
                for r in new_runs if r["test_case_id"] in previous and self.store.get("users", previous[r["test_case_id"]], include_archived=False)}
        if keep:
            self.store.bulk_update("test_runs", keep, actor_id, action="Assign")
            self.store.batch_create("assignment_history", [{"project_id": project_id, "cycle_id": result["cycle"]["id"], "test_run_id": rid, "case_key": next(r for r in new_runs if r["id"] == rid).get("case_key", ""),
                                                             "module_path": next(r for r in new_runs if r["id"] == rid).get("module_path", ""), "from_assignee_id": "", "to_assignee_id": change["assignee_id"],
                                                             "source": "manual", "reason": "Kept from the original execution", "actor_id": actor_id, "created_at": now} for rid, change in keep.items()], actor_id)
        return {**result, "assigned": len(keep)}

    def retest_cycle(self, cycle_id: str, data: dict[str, Any], actor_id: str) -> dict[str, Any]:
        cycle = self.store.get("test_cycles", cycle_id, include_archived=False)
        if not cycle: raise KeyError("Cycle not found")
        statuses = [s for s in (data.get("statuses") or ["Failed", "Blocked"]) if s in RUN_STATUSES]
        runs = [r for r in self.store.list("test_runs", filters={"cycle_id": cycle_id}, page_size=1_000_000)["items"] if (r.get("status") or "Not Run") in statuses]
        if data.get("run_ids"): runs = [r for r in runs if r["id"] in set(_ids(data["run_ids"]))]
        if not runs: raise ValueError(f"This cycle has no {' or '.join(s.lower() for s in statuses)} test cases to retest")
        return self.create_retest(cycle["project_id"], runs, data, actor_id, cycle)

    # ---------------------------------------------------------------- summary
    def summary(self, cycle_id: str) -> dict[str, Any]:
        cycle = self.store.get("test_cycles", cycle_id)
        if not cycle: raise KeyError("Cycle not found")
        runs = self.store.list("test_runs", filters={"cycle_id": cycle_id}, page_size=1_000_000)["items"]
        counts = {status: 0 for status in RUN_STATUSES}
        modules: dict[str, dict[str, Any]] = {}
        for run in runs:
            status = run.get("status") or "Not Run"
            counts[status] = counts.get(status, 0) + 1
            path = run.get("module_path") or "—"
            entry = modules.setdefault(path, {"path": path, "total": 0, "executed": 0, "passed": 0, "failed": 0, "blocked": 0})
            entry["total"] += 1
            if status in EXECUTED: entry["executed"] += 1
            if status == "Passed": entry["passed"] += 1
            if status == "Failed": entry["failed"] += 1
            if status == "Blocked": entry["blocked"] += 1
        total = len(runs); executed = sum(counts[s] for s in EXECUTED)
        applicable = executed - counts["Not Applicable"]
        return {"cycle": cycle, "total": total, "counts": counts, "executed": executed,
                "completion": round(executed * 100 / total, 1) if total else 0.0,
                "pass_rate": round(counts["Passed"] * 100 / applicable, 1) if applicable else 0.0,
                "modules": sorted(modules.values(), key=lambda m: m["path"])}
