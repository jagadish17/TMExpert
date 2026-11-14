"""Module-wise and individual assignment of test cases inside a cycle.

Rules
-----
* A module default (Cycle_Assignments.xlsx) applies to every test case below
  that module in the cycle's own snapshot tree; the deepest default wins.
* A test case assigned individually is marked ``manual`` and is never changed
  by module defaults.  "Use module default" removes that override.
* Module defaults only move test cases that have not started (Not Run), so
  nobody's in-progress or finished work is silently handed to someone else.
* Assignment changes never touch execution results.  Every change is appended
  to history/Assignment_History.xlsx (and the audit history).
"""
from __future__ import annotations

from typing import Any

from .storage import ExcelStorageService, utcnow

OPEN_STATUSES = {"Not Run", "In Progress"}
MANUAL, MODULE = "manual", "module"


def covers(module_path: str, run_path: str) -> bool:
    return run_path == module_path or run_path.startswith(module_path.rstrip("/") + "/")


class AssignmentConflict(ValueError):
    """Raised when a test case was reassigned by someone else in the meantime."""
    def __init__(self, message: str, current: dict[str, str]):
        super().__init__(message); self.current = current


class AssignmentService:
    def __init__(self, store: ExcelStorageService):
        self.store = store

    # -------------------------------------------------------------- helpers
    def _cycle(self, cycle_id: str) -> dict[str, Any]:
        cycle = self.store.get("test_cycles", cycle_id, include_archived=False)
        if not cycle: raise KeyError("Cycle not found")
        return cycle
    def _runs(self, cycle_id: str) -> list[dict[str, Any]]:
        return self.store.list("test_runs", filters={"cycle_id": cycle_id}, page_size=1_000_000)["items"]
    def _defaults(self, cycle_id: str) -> dict[str, dict[str, Any]]:
        return {row["module_path"]: row for row in self.store.list("cycle_assignments", filters={"cycle_id": cycle_id}, page_size=1_000_000)["items"]}
    def _active_user(self, user_id: str) -> dict[str, Any]:
        user = self.store.get("users", user_id, include_archived=False)
        if not user or user.get("status") != "Active": raise ValueError("Choose an active team member")
        return user
    @staticmethod
    def _module_default_for(run_path: str, defaults: dict[str, dict[str, Any]]) -> tuple[str, str]:
        """(module_path, assignee_id) of the deepest module default covering a test case."""
        best = ""
        for path in defaults:
            if covers(path, run_path) and len(path) > len(best): best = path
        return (best, defaults[best]["assignee_id"]) if best else ("", "")
    def _apply(self, cycle: dict[str, Any], changes: dict[str, tuple[str, str]], actor_id: str, reason: str) -> list[dict[str, Any]]:
        """Persist (assignee, source) changes for runs and record the history."""
        if not changes: return []
        runs = {r["id"]: r for r in self._runs(cycle["id"])}
        now = utcnow(); actor = actor_id if self.store.get("users", actor_id) else ""
        updates: dict[str, dict[str, Any]] = {}; history: list[dict[str, Any]] = []
        for run_id, (assignee, source) in changes.items():
            run = runs[run_id]
            if run.get("assignee_id", "") == assignee and run.get("assignment_source", "") == source: continue
            updates[run_id] = {"assignee_id": assignee, "assignment_source": source, "assigned_at": now, "assigned_by": actor}
            history.append({"project_id": cycle["project_id"], "cycle_id": cycle["id"], "test_run_id": run_id, "case_key": run.get("case_key", ""),
                            "module_path": run.get("module_path", ""), "from_assignee_id": run.get("assignee_id", ""), "to_assignee_id": assignee,
                            "source": source or "unassigned", "reason": reason, "actor_id": actor_id, "created_at": now})
        if not updates: return []
        updated = self.store.bulk_update("test_runs", updates, actor_id, action="Assign")
        self.store.batch_create("assignment_history", history, actor_id)
        return updated

    # --------------------------------------------------------- module level
    def set_module_default(self, cycle_id: str, module_path: Any, assignee_id: Any, actor_id: str) -> dict[str, Any]:
        cycle = self._cycle(cycle_id)
        module_path = str(module_path or "").strip()
        runs = self._runs(cycle_id)
        in_module = [r for r in runs if covers(module_path, r.get("module_path", ""))] if module_path else []
        if not in_module: raise ValueError("This module has no test cases in the cycle")
        assignee_id = str(assignee_id or "")
        if assignee_id: self._active_user(assignee_id)
        defaults = self._defaults(cycle_id)
        existing = defaults.get(module_path)
        if assignee_id and existing:
            self.store.update("cycle_assignments", existing["id"], {"assignee_id": assignee_id}, actor_id)
        elif assignee_id:
            module_id = next((r.get("module_id", "") for r in in_module if r.get("module_path") == module_path), "")
            self.store.create("cycle_assignments", {"project_id": cycle["project_id"], "cycle_id": cycle_id, "module_path": module_path, "module_id": module_id, "assignee_id": assignee_id}, actor_id)
        elif existing:
            self.store.delete("cycle_assignments", existing["id"], actor_id)
        defaults = self._defaults(cycle_id)
        changes: dict[str, tuple[str, str]] = {}
        kept_manual = kept_started = 0
        for run in in_module:
            if run.get("assignment_source") == MANUAL: kept_manual += 1; continue
            if (run.get("status") or "Not Run") != "Not Run": kept_started += 1; continue
            _, target = self._module_default_for(run.get("module_path", ""), defaults)
            changes[run["id"]] = (target, MODULE if target else "")
        updated = self._apply(cycle, changes, actor_id, f"Module default for {module_path}")
        return {"module_path": module_path, "assignee_id": assignee_id, "updated": len(updated), "kept_manual": kept_manual, "kept_started": kept_started}

    # ------------------------------------------------------- individual level
    def assign_runs(self, cycle_id: str, run_ids: Any, assignee_id: Any, actor_id: str, expected: dict[str, str] | None = None, use_module_default: bool = False) -> dict[str, Any]:
        cycle = self._cycle(cycle_id)
        if not isinstance(run_ids, list) or not run_ids: raise ValueError("Select at least one test case")
        if len(run_ids) > 1000: raise ValueError("Assign at most 1000 test cases at once")
        runs = {r["id"]: r for r in self._runs(cycle_id)}
        missing = [str(i) for i in run_ids if str(i) not in runs]
        if missing: raise KeyError("Test case not found in this cycle")
        expected = expected or {}
        stale = {rid: runs[rid].get("assignee_id", "") for rid, before in expected.items() if rid in runs and str(before or "") != runs[rid].get("assignee_id", "")}
        if stale:
            raise AssignmentConflict("Someone else changed this assignment a moment ago. The board has been refreshed.", stale)
        defaults = self._defaults(cycle_id)
        changes: dict[str, tuple[str, str]] = {}
        if use_module_default:
            for rid in map(str, run_ids):
                _, target = self._module_default_for(runs[rid].get("module_path", ""), defaults)
                changes[rid] = (target, MODULE if target else "")
            reason = "Reset to module default"
        else:
            assignee_id = str(assignee_id or "")
            if assignee_id: self._active_user(assignee_id)
            changes = {str(rid): (assignee_id, MANUAL) for rid in run_ids}
            reason = "Assigned individually" if assignee_id else "Left unassigned individually"
        updated = self._apply(cycle, changes, actor_id, reason)
        return {"updated": len(updated), "items": updated}

    # ---------------------------------------------------------------- reading
    def overview(self, cycle_id: str) -> dict[str, Any]:
        cycle = self._cycle(cycle_id)
        runs = self._runs(cycle_id)
        defaults = self._defaults(cycle_id)
        users = self.store.list("users", page_size=1_000_000)["items"]
        names = {u["id"]: u.get("name", "") for u in users}
        # Module tree from the cycle's own snapshot paths (every ancestor path is a node).
        nodes: dict[str, dict[str, Any]] = {}
        for run in runs:
            parts = [p for p in str(run.get("module_path") or "").split("/") if p]
            for depth in range(1, len(parts) + 1):
                path = "/" + "/".join(parts[:depth])
                node = nodes.setdefault(path, {"path": path, "name": parts[depth - 1], "depth": depth, "total": 0, "unassigned": 0, "manual": 0, "by_assignee": {}})
                node["total"] += 1
                assignee = run.get("assignee_id", "")
                if not assignee: node["unassigned"] += 1
                else: node["by_assignee"][assignee] = node["by_assignee"].get(assignee, 0) + 1
                if run.get("assignment_source") == MANUAL: node["manual"] += 1
        for path, node in nodes.items():
            own = defaults.get(path)
            inherited_path, inherited = self._module_default_for(path, {p: d for p, d in defaults.items() if p != path})
            node["default_assignee_id"] = own["assignee_id"] if own else ""
            node["inherited_assignee_id"] = "" if own else inherited
            node["inherited_from"] = "" if own else inherited_path
        # Workload: open (Not Run / In Progress) test cases in this cycle and across open cycles of the project.
        open_cycles = {c["id"] for c in self.store.list("test_cycles", filters={"project_id": cycle["project_id"]}, page_size=1_000_000)["items"] if c.get("status") != "Completed"}
        project_runs = self.store.list("test_runs", filters={"project_id": cycle["project_id"]}, page_size=1_000_000)["items"]
        members = []
        for user in sorted((u for u in users if u.get("status") == "Active"), key=lambda u: str(u.get("name", "")).casefold()):
            mine = [r for r in runs if r.get("assignee_id") == user["id"]]
            members.append({"id": user["id"], "name": user.get("name", ""), "role": user.get("role", ""),
                            "cycle_total": len(mine), "cycle_open": sum(1 for r in mine if (r.get("status") or "Not Run") in OPEN_STATUSES),
                            "open_workload": sum(1 for r in project_runs if r.get("assignee_id") == user["id"] and r.get("cycle_id") in open_cycles and (r.get("status") or "Not Run") in OPEN_STATUSES)})
        return {
            "cycle": cycle, "modules": [nodes[p] for p in sorted(nodes)], "members": members, "names": names,
            "total": len(runs), "unassigned": sum(1 for r in runs if not r.get("assignee_id")),
            "runs": [{k: r.get(k, "") for k in ("id", "case_key", "case_title", "module_path", "priority", "status", "assignee_id", "assignment_source", "test_case_version")} for r in runs],
        }
    def history(self, cycle_id: str, limit: int = 100) -> list[dict[str, Any]]:
        self._cycle(cycle_id)
        names = {u["id"]: u.get("name", "") for u in self.store.list("users", page_size=1_000_000, include_archived=True)["items"]}
        rows = self.store.list("assignment_history", filters={"cycle_id": cycle_id}, page_size=1_000_000)["items"]
        ordered = [r for _, r in sorted(enumerate(rows), key=lambda pair: (pair[1].get("created_at", ""), pair[0]), reverse=True)][:limit]
        return [{**r, "from_name": names.get(r.get("from_assignee_id", ""), "Unassigned") or "Unassigned", "to_name": names.get(r.get("to_assignee_id", ""), "Unassigned") or "Unassigned",
                 "actor_name": names.get(r.get("actor_id", ""), "System") or "System"} for r in ordered]
