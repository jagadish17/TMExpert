"""Readable, filterable view of the audit history (all parts, newest first)."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .storage import ExcelStorageService, XlsxCodec, as_text

NOISE = {"updated_at", "updated_by", "created_at", "created_by", "version", "hash"}
ENTITY_LABELS = {
    "test_cases": "Test case", "test_steps": "Test steps", "test_case_versions": "Test case version", "folders": "Module", "approvals": "Approval",
    "test_cycles": "Test cycle", "test_runs": "Test case in cycle", "cycle_assignments": "Module assignment", "assignment_history": "Assignment",
    "execution_history": "Execution", "execution_results": "Step result", "attachments": "Attachment", "users": "User", "projects": "Project",
    "requirements": "Requirement", "test_plans": "Test plan", "test_suites": "Test suite", "import_history": "Excel import", "defects": "Defect",
}


def _short(value: Any, limit: int = 160) -> str:
    text = as_text(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class AuditLogService:
    def __init__(self, store: ExcelStorageService):
        self.store = store

    def _label(self, entry: dict[str, Any]) -> str:
        value = entry.get("new_value") if isinstance(entry.get("new_value"), dict) and entry.get("new_value") else entry.get("previous_value")
        value = value if isinstance(value, dict) else {}
        for key in ("case_key", "name", "path", "title", "key", "email", "filename"):
            if value.get(key): return str(value[key])
        return str(entry.get("record_id", ""))

    def _related(self, entry: dict[str, Any], cycles: dict[str, str], cases: dict[str, str]) -> str:
        value = entry.get("new_value") if isinstance(entry.get("new_value"), dict) and entry.get("new_value") else entry.get("previous_value")
        value = value if isinstance(value, dict) else {}
        parts = []
        if value.get("cycle_id") in cycles: parts.append(f"Cycle {cycles[value['cycle_id']]}")
        if value.get("test_case_id") in cases: parts.append(cases[value["test_case_id"]])
        if value.get("module_path"): parts.append(str(value["module_path"]))
        return " · ".join(parts)

    @staticmethod
    def changes(entry: dict[str, Any]) -> list[dict[str, str]]:
        before = entry.get("previous_value") if isinstance(entry.get("previous_value"), dict) else {}
        after = entry.get("new_value") if isinstance(entry.get("new_value"), dict) else {}
        if not before:  # creation or event: list the meaningful values set
            return [{"field": k, "before": "", "after": _short(v)} for k, v in after.items() if k not in NOISE | {"id", "project_id", "snapshot", "step_results"} and v not in ("", None, [], {}, False)][:12]
        return [{"field": k, "before": _short(before.get(k, "")), "after": _short(after.get(k, ""))}
                for k in dict.fromkeys([*before, *after]) if k not in NOISE and as_text(before.get(k, "")) != as_text(after.get(k, ""))][:20]

    def query(self, project_id: str, f: dict[str, Any]) -> dict[str, Any]:
        names = {u["id"]: u.get("name", "") for u in self.store.list("users", page_size=1_000_000, include_archived=True)["items"]}
        cycles = {c["id"]: c.get("name", "") for c in self.store.list("test_cycles", page_size=1_000_000, include_archived=True)["items"]}
        cases = {c["id"]: c.get("case_key", "") or c.get("title", "") for c in self.store.list("test_cases", page_size=1_000_000, include_archived=True)["items"]}
        rows = self.store._load("audit")[2]
        entity, action, actor = f.get("entity") or "", f.get("action") or "", f.get("actor") or ""
        date_from, date_to, needle = str(f.get("date_from") or ""), str(f.get("date_to") or ""), str(f.get("q") or "").strip().casefold()
        matched = []
        for index in range(len(rows) - 1, -1, -1):  # newest first
            row = rows[index]
            if project_id and row.get("project_id") not in ("", None, project_id): continue
            if entity and row.get("entity") != entity: continue
            if action and row.get("action") != action: continue
            if actor and row.get("actor_id") != actor: continue
            day = str(row.get("created_at", ""))[:10]
            if date_from and day < date_from: continue
            if date_to and day > date_to: continue
            if needle and needle not in (as_text(row.get("record_id")) + " " + as_text(row.get("previous_value")) + " " + as_text(row.get("new_value"))).casefold(): continue
            matched.append(row)
        page = max(1, int(f.get("page") or 1)); size = max(1, min(int(f.get("page_size") or 50), 500))
        chosen = matched[(page - 1) * size: page * size]
        items = [{"id": r.get("id"), "created_at": r.get("created_at"), "actor_id": r.get("actor_id"), "actor_name": names.get(r.get("actor_id", ""), "") or ("System" if r.get("actor_id") in ("system", "", None) else "Unknown user"),
                  "action": r.get("action"), "entity": r.get("entity"), "entity_label": ENTITY_LABELS.get(r.get("entity", ""), r.get("entity", "")), "record_id": r.get("record_id"),
                  "label": self._label(r), "related": self._related(r, cycles, cases), "changes": self.changes(r), "tracked": bool(r.get("hash"))} for r in chosen]
        return {"items": items, "total": len(matched), "page": page, "page_size": size,
                "facets": {"entities": sorted({r.get("entity", "") for r in rows} - {""}), "actions": sorted({r.get("action", "") for r in rows} - {""}),
                           "actors": sorted(({"id": a, "name": names.get(a, "") or ("System" if a == "system" else a)} for a in {r.get("actor_id", "") for r in rows} if a), key=lambda a: a["name"].casefold())},
                "labels": ENTITY_LABELS}

    def export(self, project_id: str, f: dict[str, Any], temp_dir: Path) -> bytes:
        data = self.query(project_id, {**f, "page": 1, "page_size": 500})
        total = data["total"]; items = list(data["items"]); page = 2
        while len(items) < min(total, 50_000):
            more = self.query(project_id, {**f, "page": page, "page_size": 500})["items"]
            if not more: break
            items += more; page += 1
        headers = ("When", "User", "Action", "Area", "Record", "Related", "Changes", "Record ID", "Audit entry ID")
        rows = [{"When": i["created_at"], "User": i["actor_name"], "Action": i["action"], "Area": i["entity_label"], "Record": i["label"], "Related": i["related"],
                 "Changes": "; ".join(f"{c['field']}: {c['before']} → {c['after']}" if c["before"] else f"{c['field']}: {c['after']}" for c in i["changes"]),
                 "Record ID": i["record_id"], "Audit entry ID": i["id"]} for i in items]
        with tempfile.NamedTemporaryFile(suffix=".xlsx", dir=temp_dir, delete=False) as handle:
            path = Path(handle.name)
        try:
            XlsxCodec.write(path, headers, rows)
            return path.read_bytes()
        finally:
            path.unlink(missing_ok=True)
