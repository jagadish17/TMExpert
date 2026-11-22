"""Test planning: plans (release/sprint context), reusable suites, readiness and coverage.

A test plan represents one release or sprint testing effort.  Cycles belong to a
plan (``test_cycles.plan_id``).  A plan's *effective* result for each test case is
its latest executed result across the plan's cycles, which is what release
readiness is judged on.  Exit criteria are stored per plan and editable.
"""
from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from .storage import ExcelStorageService, XlsxCodec

PLAN_STATUSES = ("Draft", "Active", "Completed")
EXECUTED = {"Passed", "Failed", "Blocked", "Not Applicable"}
DEFAULT_CRITERIA = {"min_execution_pct": 100, "min_pass_rate": 95, "max_open_critical": 0, "max_open_high": 0, "max_blocked": 0}
CRITERIA_LABELS = {
    "min_execution_pct": "Planned test cases executed (%)", "min_pass_rate": "Pass rate of executed (%)",
    "max_open_critical": "Open Critical defects (max)", "max_open_high": "Open High defects (max)", "max_blocked": "Blocked test cases (max)",
}


def _ids(value: Any) -> list[str]:
    if value in (None, ""): return []
    if not isinstance(value, list): raise ValueError("Selections must be lists of IDs")
    return [str(v) for v in dict.fromkeys(value) if str(v)]


def _date(label: str, value: Any) -> str:
    value = str(value or "")
    if value:
        try: date.fromisoformat(value)
        except ValueError as exc: raise ValueError(f"{label} must be a valid date (YYYY-MM-DD)") from exc
    return value


class PlanningService:
    def __init__(self, store: ExcelStorageService, cycles: Any, defects: Any):
        self.store = store
        self.cycles = cycles
        self.defects = defects

    # ------------------------------------------------------------------ plans
    def criteria(self, plan: dict[str, Any]) -> dict[str, float]:
        stored = plan.get("exit_criteria")
        if isinstance(stored, str) and stored.strip():
            try: stored = json.loads(stored)
            except ValueError: stored = {}
        stored = stored if isinstance(stored, dict) else {}
        return {key: float(stored.get(key, default)) for key, default in DEFAULT_CRITERIA.items()}

    def _validate_plan(self, project_id: str, data: dict[str, Any], plan_id: str = "") -> dict[str, Any]:
        name = str(data.get("name", "")).strip()
        if not name: raise ValueError("Plan name is required")
        if len(name) > 150: raise ValueError("Plan name must be 150 characters or fewer")
        if any(p["name"].casefold() == name.casefold() and p["id"] != plan_id for p in self.store.list("test_plans", filters={"project_id": project_id}, page_size=1_000_000)["items"]):
            raise ValueError(f"A plan named “{name}” already exists in this project")
        start, end = _date("Start date", data.get("start_date")), _date("End date", data.get("end_date"))
        if start and end and end < start: raise ValueError("End date cannot be before the start date")
        status = str(data.get("status") or "Draft")
        if status not in PLAN_STATUSES: raise ValueError(f"Plan status must be one of: {', '.join(PLAN_STATUSES)}")
        out = {"name": name, "release": str(data.get("release", "") or "").strip()[:80], "build": str(data.get("build", "") or "").strip()[:80],
               "description": str(data.get("description", "") or "").strip()[:4000], "scope": str(data.get("scope", "") or "").strip()[:4000],
               "start_date": start, "end_date": end, "status": status}
        if "exit_criteria" in data:
            raw = data["exit_criteria"] or {}
            if not isinstance(raw, dict): raise ValueError("Exit criteria must be an object")
            criteria = {}
            for key, default in DEFAULT_CRITERIA.items():
                try: value = float(raw.get(key, default))
                except (TypeError, ValueError) as exc: raise ValueError(f"{CRITERIA_LABELS[key]} must be a number") from exc
                if value < 0 or (key.startswith("min_") and value > 100): raise ValueError(f"{CRITERIA_LABELS[key]} is out of range")
                criteria[key] = value
            out["exit_criteria"] = criteria
        return out

    def create_plan(self, data: dict[str, Any], actor_id: str) -> dict[str, Any]:
        project_id = str(data.get("project_id", ""))
        if not self.store.get("projects", project_id): raise ValueError("Select a valid project")
        fields = self._validate_plan(project_id, data)
        fields.setdefault("exit_criteria", dict(DEFAULT_CRITERIA))
        return self.store.create("test_plans", {**fields, "project_id": project_id, "owner_id": actor_id if self.store.get("users", actor_id) else ""}, actor_id)

    def update_plan(self, plan_id: str, data: dict[str, Any], actor_id: str) -> dict[str, Any]:
        plan = self.store.get("test_plans", plan_id, include_archived=False)
        if not plan: raise KeyError("Test plan not found")
        merged = {**plan, **data}
        return self.store.update("test_plans", plan_id, self._validate_plan(plan["project_id"], merged, plan_id), actor_id)

    def _plan_cycles(self, plan_id: str) -> list[dict[str, Any]]:
        cycles = self.store.list("test_cycles", filters={"plan_id": plan_id}, page_size=1_000_000)["items"]
        return sorted(cycles, key=lambda c: (str(c.get("start_date") or "") or str(c.get("created_at") or "")[:10], str(c.get("created_at") or "")))

    def _effective(self, runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Latest executed result per test case (falls back to an open run)."""
        best: dict[str, dict[str, Any]] = {}
        for run in runs:
            case_id = run.get("test_case_id", "")
            current = best.get(case_id)
            executed = (run.get("status") or "Not Run") in EXECUTED
            if current is None: best[case_id] = run; continue
            cur_exec = (current.get("status") or "Not Run") in EXECUTED
            # Runs arrive in cycle order, so on equal timestamps the later cycle wins.
            if executed and (not cur_exec or str(run.get("executed_at") or "") >= str(current.get("executed_at") or "")): best[case_id] = run
            elif not executed and not cur_exec and (run.get("status") == "In Progress"): best[case_id] = run
        return best

    def plan_report(self, plan_id: str) -> dict[str, Any]:
        plan = self.store.get("test_plans", plan_id)
        if not plan: raise KeyError("Test plan not found")
        names = {u["id"]: u.get("name", "") for u in self.store.list("users", page_size=1_000_000, include_archived=True)["items"]}
        cycles = self._plan_cycles(plan_id)
        cycle_names = {c["id"]: c.get("name", "") for c in cycles}
        runs = [r for c in cycles for r in self.store.list("test_runs", filters={"cycle_id": c["id"]}, page_size=1_000_000)["items"]]
        effective = self._effective(runs)
        counts = {s: 0 for s in ("Passed", "Failed", "Blocked", "Not Applicable", "In Progress", "Not Run")}
        for run in effective.values(): counts[run.get("status") or "Not Run"] = counts.get(run.get("status") or "Not Run", 0) + 1
        planned = len(effective); executed = sum(counts[s] for s in EXECUTED); applicable = executed - counts["Not Applicable"]
        execution_pct = round(executed * 100 / planned, 1) if planned else 0.0
        pass_rate = round(counts["Passed"] * 100 / applicable, 1) if applicable else 0.0
        # Per cycle
        cycle_rows = []
        for cycle in cycles:
            mine = [r for r in runs if r["cycle_id"] == cycle["id"]]
            c_counts = {s: sum(1 for r in mine if (r.get("status") or "Not Run") == s) for s in counts}
            c_exec = sum(c_counts[s] for s in EXECUTED); c_app = c_exec - c_counts["Not Applicable"]
            cycle_rows.append({"id": cycle["id"], "name": cycle.get("name", ""), "testing_type": cycle.get("testing_type", ""), "status": cycle.get("status", ""),
                               "build": cycle.get("build", ""), "environment": cycle.get("environment", ""), "start_date": cycle.get("start_date", ""), "source_cycle_id": cycle.get("source_cycle_id", ""),
                               "total": len(mine), "executed": c_exec, "counts": c_counts, "completion": round(c_exec * 100 / len(mine), 1) if mine else 0.0,
                               "pass_rate": round(c_counts["Passed"] * 100 / c_app, 1) if c_app else 0.0})
        # Testers (all runs in the plan's cycles)
        testers: dict[str, dict[str, Any]] = {}
        for run in runs:
            key = run.get("assignee_id") or ""
            row = testers.setdefault(key, {"id": key, "name": names.get(key, "Former member") if key else "Unassigned", "total": 0, "executed": 0, "passed": 0, "failed": 0, "blocked": 0})
            status = run.get("status") or "Not Run"
            row["total"] += 1; row["executed"] += status in EXECUTED
            row["passed"] += status == "Passed"; row["failed"] += status == "Failed"; row["blocked"] += status == "Blocked"
        # Modules (effective results)
        modules: dict[str, dict[str, Any]] = {}
        for run in effective.values():
            path = run.get("module_path") or "—"
            row = modules.setdefault(path, {"path": path, "total": 0, "executed": 0, "passed": 0, "failed": 0, "blocked": 0})
            status = run.get("status") or "Not Run"
            row["total"] += 1; row["executed"] += status in EXECUTED
            row["passed"] += status == "Passed"; row["failed"] += status == "Failed"; row["blocked"] += status == "Blocked"
        # Defects linked to the plan's executions
        linked = self.defects.defects_for_runs(plan["project_id"], runs)
        open_defects = [d for d in linked["defects"] if d["is_open"]]
        severity = {s: sum(1 for d in open_defects if d.get("severity") == s) for s in ("Critical", "High", "Medium", "Low")}
        criteria = self.criteria(plan)
        checks = [
            {"key": "min_execution_pct", "label": CRITERIA_LABELS["min_execution_pct"], "target": f"≥ {criteria['min_execution_pct']:g}%", "actual": f"{execution_pct:g}%", "ok": execution_pct >= criteria["min_execution_pct"]},
            {"key": "min_pass_rate", "label": CRITERIA_LABELS["min_pass_rate"], "target": f"≥ {criteria['min_pass_rate']:g}%", "actual": f"{pass_rate:g}%" if applicable else "—", "ok": bool(applicable) and pass_rate >= criteria["min_pass_rate"]},
            {"key": "max_open_critical", "label": CRITERIA_LABELS["max_open_critical"], "target": f"≤ {criteria['max_open_critical']:g}", "actual": str(severity["Critical"]), "ok": severity["Critical"] <= criteria["max_open_critical"]},
            {"key": "max_open_high", "label": CRITERIA_LABELS["max_open_high"], "target": f"≤ {criteria['max_open_high']:g}", "actual": str(severity["High"]), "ok": severity["High"] <= criteria["max_open_high"]},
            {"key": "max_blocked", "label": CRITERIA_LABELS["max_blocked"], "target": f"≤ {criteria['max_blocked']:g}", "actual": str(counts["Blocked"]), "ok": counts["Blocked"] <= criteria["max_blocked"]},
        ]
        verdict = "No test cases" if not planned else "Not started" if not executed else "Ready" if all(c["ok"] for c in checks) else "Not ready"
        case_rows = [{"test_case_id": cid, "case_key": r.get("case_key", ""), "title": r.get("case_title", ""), "module_path": r.get("module_path", ""),
                      "status": r.get("status") or "Not Run", "cycle_id": r["cycle_id"], "cycle_name": cycle_names.get(r["cycle_id"], ""), "run_id": r["id"],
                      "executed_by": names.get(r.get("executed_by", ""), ""), "executed_at": r.get("executed_at", ""), "assignee": names.get(r.get("assignee_id", ""), ""),
                      "defect_ids": r.get("defect_ids") or []} for cid, r in effective.items()]
        case_rows.sort(key=lambda r: (str(r["module_path"]), str(r["case_key"])))
        return {"plan": {**{k: plan.get(k, "") for k in ("id", "project_id", "name", "release", "build", "description", "scope", "start_date", "end_date", "status", "created_at")}, "exit_criteria": criteria},
                "cycles": cycle_rows, "planned": planned, "executed": executed, "counts": counts, "execution_pct": execution_pct, "pass_rate": pass_rate,
                "testers": sorted(testers.values(), key=lambda t: (t["id"] == "", -t["total"])), "modules": sorted(modules.values(), key=lambda m: m["path"]),
                "defects": {"open": len(open_defects), "by_severity": severity, "items": linked["defects"], "unregistered": linked["unregistered"]},
                "readiness": {"verdict": verdict, "checks": checks}, "cases": case_rows, "criteria_labels": CRITERIA_LABELS}

    def list_plans(self, project_id: str) -> list[dict[str, Any]]:
        out = []
        for plan in self.store.list("test_plans", filters={"project_id": project_id}, page_size=1_000_000)["items"]:
            report = self.plan_report(plan["id"])
            out.append({**report["plan"], "cycles": len(report["cycles"]), "planned": report["planned"], "executed": report["executed"], "counts": report["counts"],
                        "execution_pct": report["execution_pct"], "pass_rate": report["pass_rate"], "open_defects": report["defects"]["open"], "verdict": report["readiness"]["verdict"]})
        return sorted(out, key=lambda p: (p["status"] == "Completed", str(p.get("start_date") or p.get("created_at") or "")), reverse=False)

    def export_plan(self, plan_id: str, temp_dir: Path) -> tuple[bytes, str]:
        report = self.plan_report(plan_id)
        headers = ("Test Case ID", "Title", "Module", "Latest Status", "Latest Cycle", "Executed By", "Executed At", "Assigned To", "Defects")
        rows = [{"Test Case ID": r["case_key"], "Title": r["title"], "Module": r["module_path"], "Latest Status": r["status"], "Latest Cycle": r["cycle_name"],
                 "Executed By": r["executed_by"], "Executed At": r["executed_at"], "Assigned To": r["assignee"],
                 "Defects": ", ".join(r["defect_ids"]) if isinstance(r["defect_ids"], list) else str(r["defect_ids"])} for r in report["cases"]]
        with tempfile.NamedTemporaryFile(suffix=".xlsx", dir=temp_dir, delete=False) as handle: path = Path(handle.name)
        try:
            XlsxCodec.write(path, headers, rows)
            safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in report["plan"]["name"])[:60] or "Plan"
            return path.read_bytes(), f"TMExpert_Plan_{safe}.xlsx"
        finally:
            path.unlink(missing_ok=True)

    # ----------------------------------------------------------------- suites
    def _validate_suite(self, project_id: str, data: dict[str, Any], suite_id: str = "") -> dict[str, Any]:
        name = str(data.get("name", "")).strip()
        if not name: raise ValueError("Suite name is required")
        if len(name) > 150: raise ValueError("Suite name must be 150 characters or fewer")
        if any(s["name"].casefold() == name.casefold() and s["id"] != suite_id and s.get("kind") == "reusable"
               for s in self.store.list("test_suites", filters={"project_id": project_id}, page_size=1_000_000)["items"]):
            raise ValueError(f"A suite named “{name}” already exists in this project")
        folders = {f["id"] for f in self.store.list("folders", filters={"project_id": project_id}, page_size=1_000_000)["items"]}
        module_ids = _ids(data.get("module_ids")); case_ids = _ids(data.get("case_ids"))
        if any(m not in folders for m in module_ids): raise ValueError("One or more modules are not in this project")
        for case_id in case_ids:
            case = self.store.get("test_cases", case_id)
            if not case or case.get("project_id") != project_id: raise ValueError("One or more test cases are not in this project")
        if not module_ids and not case_ids: raise ValueError("Choose at least one module or test case for the suite")
        return {"name": name, "description": str(data.get("description", "") or "").strip()[:2000], "module_ids": module_ids, "case_ids": case_ids, "include_submodules": True}

    def create_suite(self, data: dict[str, Any], actor_id: str) -> dict[str, Any]:
        project_id = str(data.get("project_id", ""))
        if not self.store.get("projects", project_id): raise ValueError("Select a valid project")
        fields = self._validate_suite(project_id, data)
        return self.store.create("test_suites", {**fields, "project_id": project_id, "plan_id": "", "kind": "reusable", "status": "Active",
                                                 "owner_id": actor_id if self.store.get("users", actor_id) else ""}, actor_id)

    def update_suite(self, suite_id: str, data: dict[str, Any], actor_id: str) -> dict[str, Any]:
        suite = self.store.get("test_suites", suite_id, include_archived=False)
        if not suite or suite.get("kind") != "reusable": raise KeyError("Suite not found")
        merged = {**suite, **{k: v for k, v in data.items() if k in {"name", "description", "module_ids", "case_ids"}}}
        return self.store.update("test_suites", suite_id, self._validate_suite(suite["project_id"], merged, suite_id), actor_id)

    def list_suites(self, project_id: str) -> list[dict[str, Any]]:
        folders = {f["id"]: f.get("path", "") for f in self.store.list("folders", filters={"project_id": project_id}, page_size=1_000_000)["items"]}
        out = []
        for suite in self.store.list("test_suites", filters={"project_id": project_id, "kind": "reusable"}, page_size=1_000_000)["items"]:
            resolved = self.cycles.resolve(project_id, {"suite_ids": [suite["id"]]})
            uses = [c for c in self.store.list("test_cycles", filters={"project_id": project_id}, page_size=1_000_000)["items"] if suite["id"] in (c.get("suite_ids") or [])]
            out.append({**{k: suite.get(k, "") for k in ("id", "name", "description", "created_at", "updated_at")},
                        "module_ids": suite.get("module_ids") or [], "case_ids": suite.get("case_ids") or [],
                        "modules": [folders.get(m, "(archived module)") for m in (suite.get("module_ids") or [])],
                        "eligible": len(resolved["cases"]), "excluded": resolved["rule_excluded"], "missing": len(resolved["skipped"]),
                        "cycles": [{"id": c["id"], "name": c.get("name", "")} for c in uses]})
        return sorted(out, key=lambda s: s["name"].casefold())

    # ------------------------------------------------------------ requirements
    def coverage(self, project_id: str) -> dict[str, Any]:
        requirements = self.store.list("requirements", filters={"project_id": project_id}, page_size=1_000_000)["items"]
        cases = self.store.list("test_cases", filters={"project_id": project_id}, page_size=1_000_000)["items"]
        runs = self.store.list("test_runs", filters={"project_id": project_id}, page_size=1_000_000)["items"]
        latest = self._effective(runs)
        rows = []
        for req in sorted(requirements, key=lambda r: str(r.get("key", ""))):
            linked = [c for c in cases if req["id"] in (c.get("requirement_ids") or [])]
            statuses = [(latest.get(c["id"]) or {}).get("status") or "Not Run" for c in linked]
            if not linked: coverage = "Not covered"
            elif any(s in {"Failed", "Blocked"} for s in statuses): coverage = "Failing"
            elif all(s in {"Passed", "Not Applicable"} for s in statuses): coverage = "Passing"
            elif any(s in EXECUTED for s in statuses): coverage = "Partly tested"
            else: coverage = "Not run"
            rows.append({**{k: req.get(k, "") for k in ("id", "key", "title", "type", "priority", "status", "description")}, "coverage": coverage,
                         "cases": [{"id": c["id"], "case_key": c.get("case_key", ""), "title": c.get("title", ""), "status": c.get("status", ""), "latest": s} for c, s in zip(linked, statuses)]})
        summary = {k: sum(1 for r in rows if r["coverage"] == k) for k in ("Passing", "Failing", "Partly tested", "Not run", "Not covered")}
        return {"requirements": rows, "summary": summary, "total": len(rows)}
