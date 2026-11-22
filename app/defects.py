"""Defects as records, linked to the executions where they were found.

Executions keep the defect references they always had (``test_runs.defect_ids``,
a list of keys).  A reference matches a defect record by its TMExpert key (e.g.
LMS-DEF-0001) or by its external key (e.g. a Jira key), so existing free-text IDs
keep working and can be registered as records at any time without rewriting data.
"""
from __future__ import annotations

import re
from typing import Any

from .storage import ExcelStorageService, utcnow

DEFECT_STATUSES = ("Open", "In Progress", "Fixed", "Verified", "Closed", "Reopened", "Rejected")
OPEN_STATES = {"Open", "In Progress", "Reopened"}
SEVERITIES = ("Critical", "High", "Medium", "Low")
PRIORITIES = ("Critical", "High", "Medium", "Low")
EXECUTED = {"Passed", "Failed", "Blocked", "Not Applicable"}
_KEY = re.compile(r"^[^\s<>\"']{1,120}$")


def tokens(value: Any) -> list[str]:
    items = value if isinstance(value, list) else re.split(r"[,\s]+", str(value or ""))
    return [str(item).strip() for item in items if str(item).strip()]


class DefectService:
    def __init__(self, store: ExcelStorageService, cycles: Any):
        self.store = store
        self.cycles = cycles

    # ---------------------------------------------------------------- helpers
    def _names(self) -> dict[str, str]:
        return {u["id"]: u.get("name", "") for u in self.store.list("users", page_size=1_000_000, include_archived=True)["items"]}
    def _project_defects(self, project_id: str) -> list[dict[str, Any]]:
        return self.store.list("defects", filters={"project_id": project_id}, page_size=1_000_000)["items"]
    def _index(self, defects: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        for defect in defects:
            for key in (defect.get("key"), defect.get("external_key")):
                if key: index[str(key).casefold()] = defect
        return index
    def _next_key(self, project_id: str) -> str:
        project = self.store.get("projects", project_id) or {}
        prefix = f"{str(project.get('key') or 'TM').upper()}-DEF-"
        numbers = [int(d["key"][len(prefix):]) for d in self.store.list("defects", filters={"project_id": project_id}, page_size=1_000_000, include_archived=True)["items"]
                   if str(d.get("key", "")).upper().startswith(prefix) and str(d["key"][len(prefix):]).isdigit()]
        return f"{prefix}{max(numbers, default=0) + 1:04d}"
    def _clean(self, project_id: str, data: dict[str, Any], defect_id: str = "") -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "summary" in data or not defect_id:
            summary = str(data.get("summary", "")).strip()
            if not summary: raise ValueError("Defect summary is required")
            if len(summary) > 200: raise ValueError("Defect summary must be 200 characters or fewer")
            out["summary"] = summary
        if "description" in data: out["description"] = str(data.get("description") or "").strip()[:8000]
        for field, allowed, default in (("severity", SEVERITIES, "Medium"), ("priority", PRIORITIES, "Medium")):
            if field in data or not defect_id:
                value = str(data.get(field) or default)
                if value not in allowed: raise ValueError(f"{field.title()} must be one of: {', '.join(allowed)}")
                out[field] = value
        if "status" in data:
            if data["status"] not in DEFECT_STATUSES: raise ValueError(f"Status must be one of: {', '.join(DEFECT_STATUSES)}")
            out["status"] = data["status"]
        if "assignee_id" in data:
            assignee = str(data.get("assignee_id") or "")
            if assignee and not self.store.get("users", assignee, include_archived=False): raise ValueError("Choose an active team member")
            out["assignee_id"] = assignee
        for field in ("environment", "build", "fixed_in_build"):
            if field in data: out[field] = str(data.get(field) or "").strip()[:80]
        if "external_key" in data:
            key = str(data.get("external_key") or "").strip()
            if key and not _KEY.match(key): raise ValueError("External key may not contain spaces or quotes")
            if key and any(str(d.get("external_key") or "").casefold() == key.casefold() and d["id"] != defect_id for d in self._project_defects(project_id)):
                raise ValueError(f"Another defect already uses the external key {key}")
            out["external_key"] = key
        if "external_url" in data:
            url = str(data.get("external_url") or "").strip()
            if url and not re.match(r"^https?://\S+$", url): raise ValueError("External link must start with http:// or https://")
            out["external_url"] = url[:500]
        return out

    # ---------------------------------------------------------------- actions
    def create(self, project_id: str, data: dict[str, Any], actor_id: str, run_id: str = "") -> dict[str, Any]:
        if not self.store.get("projects", project_id): raise ValueError("Select a valid project")
        fields = self._clean(project_id, data)
        run = self.store.get("test_runs", run_id, include_archived=False) if run_id else None
        if run_id and (not run or run.get("project_id") != project_id): raise KeyError("Test run not found")
        cycle = self.store.get("test_cycles", run["cycle_id"]) if run else None
        defect = self.store.create("defects", {
            **fields, "project_id": project_id, "key": self._next_key(project_id), "status": "Open",
            "reporter_id": actor_id if self.store.get("users", actor_id) else "",
            "environment": fields.get("environment") or (run or {}).get("environment") or (cycle or {}).get("environment", ""),
            "build": fields.get("build") or (run or {}).get("build") or (cycle or {}).get("build", ""),
            "test_run_id": run["id"] if run else "", "test_case_id": run["test_case_id"] if run else "", "found_in_cycle_id": run["cycle_id"] if run else "",
        }, actor_id)
        if run: self._attach(run, defect["key"], actor_id)
        return defect

    def _attach(self, run: dict[str, Any], key: str, actor_id: str) -> None:
        current = tokens(run.get("defect_ids") or [])
        if key.casefold() not in {c.casefold() for c in current}:
            self.store.update("test_runs", run["id"], {"defect_ids": current + [key]}, actor_id, action="Link defect")

    def link(self, run_id: str, defect_id: str, actor_id: str) -> dict[str, Any]:
        run = self.store.get("test_runs", run_id, include_archived=False)
        defect = self.store.get("defects", defect_id, include_archived=False)
        if not run: raise KeyError("Test run not found")
        if not defect or defect.get("project_id") != run.get("project_id"): raise KeyError("Defect not found")
        self._attach(run, defect["key"], actor_id)
        return defect

    def update(self, defect_id: str, data: dict[str, Any], actor_id: str) -> dict[str, Any]:
        defect = self.store.get("defects", defect_id, include_archived=False)
        if not defect: raise KeyError("Defect not found")
        changes = self._clean(defect["project_id"], data, defect_id)
        new_status = changes.get("status", defect.get("status"))
        if new_status != defect.get("status"):
            now = utcnow()
            if new_status == "Fixed": changes["resolved_at"] = now
            if new_status in {"Verified", "Closed", "Rejected"}: changes["closed_at"] = now
            if new_status in OPEN_STATES: changes.update(resolved_at="", closed_at="")
        return self.store.update("defects", defect_id, changes, actor_id, action="Status change" if "status" in changes and len(changes) <= 3 else "Update")

    def register(self, project_id: str, reference: str, actor_id: str) -> dict[str, Any]:
        """Turn a free-text defect reference found on executions into a defect record."""
        reference = str(reference or "").strip()
        if not reference or not _KEY.match(reference): raise ValueError("Choose a defect reference to register")
        if reference.casefold() in self._index(self._project_defects(project_id)): raise ValueError(f"{reference} is already registered")
        runs = [r for r in self.store.list("test_runs", filters={"project_id": project_id}, page_size=1_000_000)["items"]
                if reference.casefold() in {t.casefold() for t in tokens(r.get("defect_ids") or [])}]
        if not runs: raise ValueError(f"No execution refers to {reference}")
        first = sorted(runs, key=lambda r: str(r.get("executed_at") or r.get("created_at") or ""))[0]
        is_url = bool(re.match(r"^https?://", reference))
        return self.store.create("defects", {
            "project_id": project_id, "key": self._next_key(project_id), "summary": f"{first.get('case_key', '')} failed (registered from {reference})"[:200],
            "description": first.get("actual_result", ""), "severity": "Medium", "priority": "Medium", "status": "Open",
            "external_key": "" if is_url else reference, "external_url": reference if is_url else "",
            "reporter_id": actor_id if self.store.get("users", actor_id) else "", "environment": first.get("environment", ""), "build": first.get("build", ""),
            "test_run_id": first["id"], "test_case_id": first.get("test_case_id", ""), "found_in_cycle_id": first.get("cycle_id", ""),
        }, actor_id)

    def retest(self, defect_id: str, data: dict[str, Any], actor_id: str) -> dict[str, Any]:
        defect = self.store.get("defects", defect_id, include_archived=False)
        if not defect: raise KeyError("Defect not found")
        linked = self._linked_runs(defect)
        latest: dict[str, dict[str, Any]] = {}
        for run in linked:  # the latest execution of each affected test case
            if str(run.get("created_at", "")) >= str(latest.get(run["test_case_id"], {}).get("created_at", "")): latest[run["test_case_id"]] = run
        if not latest: raise ValueError("No execution is linked to this defect")
        sources = {r["cycle_id"] for r in latest.values()}
        source_cycle = self.store.get("test_cycles", next(iter(sources))) if len(sources) == 1 else None
        name = str(data.get("name") or "").strip() or self.cycles._unique_name(defect["project_id"], f"Retest {defect['key']}")
        build = data.get("build", defect.get("fixed_in_build") or (source_cycle or {}).get("build", ""))
        return self.cycles.create_retest(defect["project_id"], list(latest.values()), {**data, "name": name, "build": build}, actor_id, source_cycle)

    # ---------------------------------------------------------------- reading
    def _linked_runs(self, defect: dict[str, Any]) -> list[dict[str, Any]]:
        keys = {str(k).casefold() for k in (defect.get("key"), defect.get("external_key")) if k}
        return [r for r in self.store.list("test_runs", filters={"project_id": defect["project_id"]}, page_size=1_000_000, include_archived=True)["items"]
                if keys & {t.casefold() for t in tokens(r.get("defect_ids") or [])}]

    def _retest_state(self, defect: dict[str, Any], runs: list[dict[str, Any]]) -> str:
        if defect.get("status") not in {"Fixed", "Verified", "Closed"}: return ""
        since = str(defect.get("resolved_at") or "")
        linked_ids = {x["id"] for x in runs}
        cases = {x.get("test_case_id") for x in runs}
        # A retest is a new execution of an affected case after the fix (e.g. in a retest cycle),
        # or a re-run of the original execution strictly after the fix.
        later = [r for r in self.store.list("test_runs", filters={"project_id": defect["project_id"]}, page_size=1_000_000)["items"]
                 if r.get("test_case_id") in cases and (r.get("status") or "Not Run") in EXECUTED
                 and (str(r.get("executed_at") or "") > since if r["id"] in linked_ids else str(r.get("executed_at") or "") >= since)]
        if not later: return "Awaiting retest"
        return "Retest passed" if all(r.get("status") in {"Passed", "Not Applicable"} for r in later) else "Retest failed"

    def describe(self, defect: dict[str, Any], names: dict[str, str] | None = None, cycles: dict[str, str] | None = None) -> dict[str, Any]:
        names = names or self._names()
        cycles = cycles or {c["id"]: c.get("name", "") for c in self.store.list("test_cycles", filters={"project_id": defect["project_id"]}, page_size=1_000_000, include_archived=True)["items"]}
        runs = self._linked_runs(defect)
        return {**{k: defect.get(k, "") for k in ("id", "key", "summary", "description", "severity", "priority", "status", "environment", "build", "fixed_in_build",
                                                    "external_key", "external_url", "created_at", "updated_at", "resolved_at", "closed_at", "found_in_cycle_id", "assignee_id")},
                "assignee_name": names.get(defect.get("assignee_id", ""), ""), "reporter_name": names.get(defect.get("reporter_id", ""), ""),
                "found_in_cycle": cycles.get(defect.get("found_in_cycle_id", ""), ""), "is_open": defect.get("status") in OPEN_STATES,
                "retest": self._retest_state(defect, runs),
                "executions": [{"run_id": r["id"], "case_key": r.get("case_key", ""), "title": r.get("case_title", ""), "cycle_id": r["cycle_id"], "cycle": cycles.get(r["cycle_id"], ""),
                                "status": r.get("status") or "Not Run", "executed_at": r.get("executed_at", "")} for r in sorted(runs, key=lambda r: str(r.get("created_at", "")))]}

    def defects_for_runs(self, project_id: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
        index = self._index(self._project_defects(project_id))
        found: dict[str, dict[str, Any]] = {}; unregistered: set[str] = set()
        for run in runs:
            for token in tokens(run.get("defect_ids") or []):
                defect = index.get(token.casefold())
                if defect: found[defect["id"]] = defect
                else: unregistered.add(token)
        names = self._names()
        return {"defects": [self.describe(d, names) for d in found.values()], "unregistered": sorted(unregistered, key=str.casefold)}

    def list(self, project_id: str, filters: dict[str, Any]) -> dict[str, Any]:
        names = self._names()
        cycles = {c["id"]: c.get("name", "") for c in self.store.list("test_cycles", filters={"project_id": project_id}, page_size=1_000_000, include_archived=True)["items"]}
        defects = [self.describe(d, names, cycles) for d in self._project_defects(project_id)]
        status, severity, needle = filters.get("status") or "", filters.get("severity") or "", str(filters.get("q") or "").strip().casefold()
        if status == "__open": defects = [d for d in defects if d["is_open"]]
        elif status: defects = [d for d in defects if d["status"] == status]
        if severity: defects = [d for d in defects if d["severity"] == severity]
        if needle: defects = [d for d in defects if needle in f"{d['key']} {d['external_key']} {d['summary']}".casefold()]
        defects.sort(key=lambda d: (not d["is_open"], SEVERITIES.index(d["severity"]) if d["severity"] in SEVERITIES else 9, str(d["key"])))
        everything = self._project_defects(project_id)
        index = self._index(everything)
        references: dict[str, int] = {}
        for run in self.store.list("test_runs", filters={"project_id": project_id}, page_size=1_000_000)["items"]:
            for token in tokens(run.get("defect_ids") or []):
                if token.casefold() not in index: references[token] = references.get(token, 0) + 1
        return {"items": defects, "unregistered": [{"reference": k, "executions": v} for k, v in sorted(references.items(), key=lambda kv: kv[0].casefold())],
                "summary": {"open": sum(1 for d in everything if d.get("status") in OPEN_STATES), "fixed": sum(1 for d in everything if d.get("status") == "Fixed"),
                            "total": len(everything)},
                "statuses": list(DEFECT_STATUSES), "severities": list(SEVERITIES), "priorities": list(PRIORITIES)}
