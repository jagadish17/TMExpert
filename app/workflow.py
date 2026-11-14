"""Repository workflow: approval lifecycle, team visibility, duplicates and bulk work.

Every lifecycle step appends a row to history/Approvals.xlsx (append-only) and
updates the status column of the test case or module.  Statuses keep their
original stored values; "In Review" is shown as "Pending Approval" in the UI.
"""
from __future__ import annotations

import re
from typing import Any

from .storage import ExcelStorageService, utcnow

PENDING = "In Review"
CASE_STATUSES = ("Draft", PENDING, "Approved", "Rejected", "Deprecated")
PRIORITIES = ("Critical", "High", "Medium", "Low")

# target status -> (allowed current statuses, decision recorded in Approvals.xlsx)
CASE_TRANSITIONS: dict[str, tuple[set[str], str]] = {
    PENDING: ({"Draft", "Rejected"}, "Submitted"),
    "Approved": ({"Draft", PENDING, "Rejected", "Approved"}, "Approved"),
    "Rejected": ({PENDING}, "Rejected"),
    "Draft": ({PENDING, "Approved", "Rejected", "Deprecated"}, "Withdrawn"),
    "Deprecated": ({"Draft", PENDING, "Approved", "Rejected"}, "Deprecated"),
}
FOLDER_TRANSITIONS: dict[str, tuple[set[str], str]] = {
    PENDING: ({"Draft", "Rejected"}, "Submitted"),
    "Approved": ({"Draft", PENDING, "Rejected", "Approved"}, "Approved"),
    "Rejected": ({PENDING, "Draft"}, "Rejected"),
    "Draft": ({PENDING, "Approved", "Rejected"}, "Withdrawn"),
}
STATUS_LABELS = {PENDING: "Pending Approval"}
# Fields that define what a tester executes; a change creates a new version.
CONTENT_FIELDS = ("title", "objective", "description", "preconditions", "expected_result", "priority", "test_type", "tags", "folder_id")
FIELD_LABELS = {"title": "Title", "objective": "Objective", "description": "Description", "preconditions": "Preconditions",
                "expected_result": "Expected result", "priority": "Priority", "test_type": "Test type", "tags": "Tags", "folder_id": "Module"}


def normalize_title(title: Any) -> str:
    return re.sub(r"[\s\W_]+", " ", str(title or "")).strip().casefold()


def label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


class RepositoryWorkflow:
    def __init__(self, store: ExcelStorageService):
        self.store = store

    # ------------------------------------------------------------- lookups
    def _folders(self, project_id: str = "") -> dict[str, dict[str, Any]]:
        filters = {"project_id": project_id} if project_id else {}
        return {f["id"]: f for f in self.store.list("folders", filters=filters, page_size=1_000_000)["items"]}
    def _user_names(self) -> dict[str, str]:
        names = {u["id"]: u.get("name", "") for u in self.store.list("users", page_size=1_000_000, include_archived=True)["items"]}
        names["system"] = "System"
        return names
    @staticmethod
    def folder_approved(folder: dict[str, Any] | None) -> bool:
        return bool(folder) and folder.get("approval_status") in ("", "Approved", None)
    def folder_chain_approved(self, folder_id: str, folders: dict[str, dict[str, Any]]) -> bool:
        seen: set[str] = set()
        current = folders.get(str(folder_id))
        if not current: return False
        while current:
            if current["id"] in seen or not self.folder_approved(current): return False
            seen.add(current["id"])
            parent_id = str(current.get("parent_id") or "")
            if not parent_id: return True
            current = folders.get(parent_id)
            if current is None: return False  # parent archived or missing
        return True
    def case_visible_to_team(self, case: dict[str, Any] | None, folders: dict[str, dict[str, Any]] | None = None) -> bool:
        if not case or case.get("archived") is True or case.get("status") != "Approved": return False
        folders = folders if folders is not None else self._folders(str(case.get("project_id", "")))
        return self.folder_chain_approved(str(case.get("folder_id", "")), folders)
    def visible_case(self, case_id: str) -> bool:
        return self.case_visible_to_team(self.store.get("test_cases", str(case_id or ""), include_archived=False))
    def list_team_cases(self, filters: dict[str, Any], query: str, page: int, page_size: int) -> dict[str, Any]:
        rows = self.store.list("test_cases", filters=filters, query=query, page_size=1_000_000)["items"]
        folders = self._folders(str(filters.get("project_id", "")))
        visible = [row for row in rows if self.case_visible_to_team(row, folders)]
        start = max(0, (page - 1) * page_size)
        return {"items": visible[start:start + page_size], "total": len(visible), "page": page, "page_size": page_size}
    def list_team_folders(self, filters: dict[str, Any], page: int, page_size: int) -> dict[str, Any]:
        folders = self._folders(str(filters.get("project_id", "")))
        visible = [f for f in folders.values() if self.folder_chain_approved(f["id"], folders) and all(str(f.get(k, "")) == str(v) for k, v in filters.items() if v not in (None, ""))]
        start = max(0, (page - 1) * page_size)
        return {"items": visible[start:start + page_size], "total": len(visible), "page": page, "page_size": page_size}

    # -------------------------------------------------------------- folders
    def _sibling_name_taken(self, project_id: str, parent_id: str, name: str, exclude_id: str = "") -> bool:
        return any(f["id"] != exclude_id and str(f.get("parent_id") or "") == parent_id and f.get("name", "").casefold() == name.casefold()
                   for f in self._folders(project_id).values())
    def create_folder(self, data: dict[str, Any], actor_id: str) -> dict[str, Any]:
        project_id = str(data.get("project_id", "")); parent_id = str(data.get("parent_id") or "")
        name = str(data.get("name", "")).strip()
        if not name: raise ValueError("Module name is required")
        if "/" in name: raise ValueError("Module names cannot contain '/'")
        if len(name) > 120: raise ValueError("Module names must be 120 characters or fewer")
        if not self.store.get("projects", project_id): raise ValueError("Select a valid project")
        parent = self.store.get("folders", parent_id, include_archived=False) if parent_id else None
        if parent_id and not parent: raise ValueError("The parent module does not exist")
        if self._sibling_name_taken(project_id, parent_id, name): raise ValueError(f"A module named “{name}” already exists here")
        path = f"{str(parent.get('path') or '').rstrip('/')}/{name}" if parent else f"/{name}"
        return self.store.create("folders", {"project_id": project_id, "parent_id": parent_id, "name": name, "path": path, "approval_status": "Draft"}, actor_id)
    def repair_folder_paths(self, actor_id: str = "system") -> int:
        """Recompute each module's stored path from its parent chain.

        Older versions could change a parent without rewriting the path.  The
        path column is what Excel import/export use, so it must match the tree.
        """
        folders = {f["id"]: f for f in self.store.list("folders", page_size=1_000_000, include_archived=True)["items"]}
        def actual(folder: dict[str, Any]) -> str:
            names: list[str] = []; seen: set[str] = set(); current: dict[str, Any] | None = folder
            while current and current["id"] not in seen:
                seen.add(current["id"]); names.append(str(current.get("name", "")))
                current = folders.get(str(current.get("parent_id") or ""))
            return "/" + "/".join(reversed(names))
        changes = {f["id"]: {"path": actual(f)} for f in folders.values() if str(f.get("path") or "") != actual(f)}
        if changes: self.store.bulk_update("folders", changes, actor_id, action="Repair path")
        return len(changes)
    def rename_folder(self, folder_id: str, name: Any, actor_id: str) -> dict[str, Any]:
        folder = self.store.get("folders", folder_id, include_archived=False)
        if not folder: raise KeyError("Module not found")
        name = str(name or "").strip()
        if not name or "/" in name: raise ValueError("Enter a module name without '/'")
        if name == folder["name"]: return folder
        if self._sibling_name_taken(folder["project_id"], str(folder.get("parent_id") or ""), name, folder_id):
            raise ValueError(f"A module named “{name}” already exists here")
        old_path = str(folder.get("path") or "")
        new_path = old_path[: old_path.rfind("/") + 1] + name if "/" in old_path else f"/{name}"
        changes: dict[str, dict[str, Any]] = {folder_id: {"name": name, "path": new_path}}
        for other in self._folders(folder["project_id"]).values():
            other_path = str(other.get("path") or "")
            if other["id"] != folder_id and old_path and other_path.startswith(old_path + "/"):
                changes[other["id"]] = {"path": new_path + other_path[len(old_path):]}
        return next(r for r in self.store.bulk_update("folders", changes, actor_id, action="Rename") if r["id"] == folder_id)
    def transition_folder(self, folder_id: str, target: str, comment: Any, actor_id: str) -> dict[str, Any]:
        folder = self.store.get("folders", folder_id, include_archived=False)
        if not folder: raise KeyError("Module not found")
        current = folder.get("approval_status") or "Approved"
        if target not in FOLDER_TRANSITIONS: raise ValueError("Unknown module status")
        allowed, decision = FOLDER_TRANSITIONS[target]
        if current not in allowed: raise ValueError(f"A module that is {label(current)} cannot move to {label(target)}")
        comment = str(comment or "").strip()
        if target == "Rejected" and len(comment) < 3: raise ValueError("Enter a reason for rejecting the module")
        reviewer = actor_id if self.store.get("users", actor_id) else ""
        self.store.create("approvals", {"folder_id": folder_id, "test_case_id": "", "version": "", "reviewer_id": reviewer, "decision": decision, "comment": comment}, actor_id)
        changes: dict[str, Any] = {"approval_status": target}
        if target in {"Approved", "Rejected"}: changes.update(reviewed_by=reviewer, reviewed_at=utcnow())
        return self.store.update("folders", folder_id, changes, actor_id)

    # ----------------------------------------------------------- test cases
    def _check_case_transition(self, case: dict[str, Any], target: str, comment: str) -> str:
        if target not in CASE_TRANSITIONS: raise ValueError("Unknown approval status")
        current = case.get("status") or "Draft"
        allowed, decision = CASE_TRANSITIONS[target]
        if current not in allowed:
            raise ValueError(f"{case.get('case_key') or 'This test case'} is {label(current)} and cannot move to {label(target)}")
        if target == "Rejected" and len(comment) < 3: raise ValueError("Enter a reason for the rejection")
        return "Restored" if current == "Deprecated" and target == "Draft" else decision
    def transition_case(self, case_id: str, target: str, comment: Any, actor_id: str) -> dict[str, Any]:
        result = self.transition_cases([case_id], target, comment, actor_id)
        if result["skipped"]: raise ValueError(result["skipped"][0]["reason"])
        return result["items"][0]
    def transition_cases(self, case_ids: list[str], target: str, comment: Any, actor_id: str) -> dict[str, Any]:
        comment = str(comment or "").strip()
        reviewer = actor_id if self.store.get("users", actor_id) else ""
        approvals: list[dict[str, Any]] = []; changes: dict[str, dict[str, Any]] = {}; skipped: list[dict[str, Any]] = []
        for case_id in dict.fromkeys(str(i) for i in case_ids):
            case = self.store.get("test_cases", case_id, include_archived=False)
            if not case:
                skipped.append({"id": case_id, "case_key": "", "reason": "Test case not found"}); continue
            try:
                decision = self._check_case_transition(case, target, comment)
            except ValueError as exc:
                skipped.append({"id": case_id, "case_key": case.get("case_key", ""), "reason": str(exc)}); continue
            approvals.append({"test_case_id": case_id, "folder_id": "", "version": case.get("version", ""), "reviewer_id": reviewer, "decision": decision, "comment": comment})
            update: dict[str, Any] = {"status": target}
            if target in {"Approved", "Rejected"}: update["reviewer_id"] = reviewer
            changes[case_id] = update
        items: list[dict[str, Any]] = []
        if changes:
            self.store.batch_create("approvals", approvals, actor_id)
            items = self.store.bulk_update("test_cases", changes, actor_id, action="Status change")
        return {"items": items, "updated": len(items), "skipped": skipped}
    def find_duplicates(self, project_id: str, title: Any, folder_id: str = "", exclude_id: str = "") -> list[dict[str, Any]]:
        wanted = normalize_title(title)
        if not wanted: return []
        folders = self._folders(project_id)
        return [
            {"id": c["id"], "case_key": c.get("case_key", ""), "title": c.get("title", ""), "folder_path": folders.get(c.get("folder_id", ""), {}).get("path", "")}
            for c in self.store.list("test_cases", filters={"project_id": project_id}, page_size=1_000_000)["items"]
            if c["id"] != exclude_id and normalize_title(c.get("title")) == wanted and (not folder_id or c.get("folder_id") == folder_id)
        ]
    def bulk(self, action: str, ids: Any, params: dict[str, Any], actor_id: str) -> dict[str, Any]:
        if not isinstance(ids, list) or not ids: raise ValueError("Select at least one test case")
        ids = [str(i) for i in dict.fromkeys(ids)]
        if len(ids) > 500: raise ValueError("Select at most 500 test cases at once")
        targets = {"submit": PENDING, "approve": "Approved", "reject": "Rejected", "withdraw": "Draft", "deprecate": "Deprecated", "restore": "Draft"}
        if action in targets:
            if action == "restore":
                ids_ok = [i for i in ids if (self.store.get("test_cases", i) or {}).get("status") == "Deprecated"]
                result = self.transition_cases(ids_ok, "Draft", params.get("comment"), actor_id)
                result["skipped"] += [{"id": i, "case_key": (self.store.get("test_cases", i) or {}).get("case_key", ""), "reason": "Only deprecated test cases can be restored"} for i in ids if i not in ids_ok]
                return result
            return self.transition_cases(ids, targets[action], params.get("comment"), actor_id)
        if action == "archive":
            result = self.store.delete_many("test_cases", ids, actor_id)
            return {"updated": result["deleted"], "skipped": [{"id": i, "case_key": "", "reason": "Test case not found"} for i in result["missing"]]}
        cases = {i: self.store.get("test_cases", i, include_archived=False) for i in ids}
        missing = [i for i, c in cases.items() if not c]
        if missing: raise KeyError("Test case not found")
        changes: dict[str, dict[str, Any]] = {}
        if action == "move":
            folder = self.store.get("folders", str(params.get("folder_id", "")), include_archived=False)
            if not folder: raise ValueError("Choose a module to move the test cases to")
            if any(c["project_id"] != folder["project_id"] for c in cases.values()): raise ValueError("Test cases can only move within their project")
            changes = {i: {"folder_id": folder["id"]} for i in ids}
        elif action == "set_priority":
            priority = str(params.get("priority", "")).title()
            if priority not in PRIORITIES: raise ValueError("Priority must be Critical, High, Medium, or Low")
            changes = {i: {"priority": priority} for i in ids}
        elif action in {"add_tags", "remove_tags"}:
            raw = params.get("tags", [])
            tags = [t.strip() for t in (raw if isinstance(raw, list) else str(raw).split(",")) if str(t).strip()]
            if not tags: raise ValueError("Enter at least one tag")
            folded = {t.casefold() for t in tags}
            for i, case in cases.items():
                current = [str(t) for t in (case.get("tags") or [])] if isinstance(case.get("tags"), list) else [t.strip() for t in str(case.get("tags") or "").split(",") if t.strip()]
                if action == "add_tags": new = current + [t for t in tags if t.casefold() not in {c.casefold() for c in current}]
                else: new = [t for t in current if t.casefold() not in folded]
                changes[i] = {"tags": new}
        else:
            raise ValueError("Unknown bulk action")
        items = self.store.bulk_update("test_cases", changes, actor_id, action="Bulk " + action.replace("_", " "))
        return {"items": items, "updated": len(items), "skipped": []}

    # -------------------------------------------------------------- reading
    def _steps(self, case_id: str, version: Any) -> list[dict[str, Any]]:
        steps = [s for s in self.store.list("test_steps", filters={"test_case_id": case_id}, page_size=1_000_000)["items"] if str(s.get("version", "")) == str(version)]
        return sorted(steps, key=lambda s: int(s.get("step_number") or 0))
    def _approval_rows(self, filters: dict[str, Any], names: dict[str, str]) -> list[dict[str, Any]]:
        rows = self.store.list("approvals", filters=filters, page_size=1_000_000)["items"]
        # Newest first; rows written in the same second keep their workbook (append) order.
        ordered = sorted(enumerate(rows), key=lambda pair: (str(pair[1].get("created_at", "")), pair[0]), reverse=True)
        return [{**r, "reviewer_name": names.get(r.get("reviewer_id", ""), "") or "—"} for _, r in ordered]
    def case_details(self, case_id: str) -> dict[str, Any]:
        case = self.store.get("test_cases", case_id)
        if not case: raise KeyError("Test case not found")
        names = self._user_names(); folders = self._folders(case["project_id"])
        versions = self.store.list("test_case_versions", filters={"test_case_id": case_id}, page_size=1_000_000)["items"]
        versions.sort(key=lambda v: int(v.get("version") or 0), reverse=True)
        approvals = self._approval_rows({"test_case_id": case_id}, names)
        latest_review = next((a for a in approvals if a.get("decision") in {"Approved", "Rejected", "Changes Requested"}), None)
        folder = folders.get(case.get("folder_id", ""), {})
        return {
            "case": case, "steps": self._steps(case_id, case.get("version")),
            "folder_path": folder.get("path", ""), "module_approved": self.folder_chain_approved(case.get("folder_id", ""), folders),
            "created_by_name": names.get(case.get("created_by", ""), "") or "—", "updated_by_name": names.get(case.get("updated_by", ""), "") or "—",
            "latest_review": latest_review, "approvals": approvals,
            "versions": [{"version": v.get("version"), "created_at": v.get("created_at"), "comment": v.get("comment", ""), "changed_by_name": names.get(v.get("changed_by", ""), "") or "System"} for v in versions],
            "allowed_transitions": [t for t, (allowed, _) in CASE_TRANSITIONS.items() if (case.get("status") or "Draft") in allowed and not (t == "Approved" and case.get("status") == "Approved")],
        }
    def version_detail(self, case_id: str, version: str) -> dict[str, Any]:
        versions = {str(v.get("version")): v for v in self.store.list("test_case_versions", filters={"test_case_id": case_id}, page_size=1_000_000)["items"]}
        if str(version) not in versions: raise KeyError("Version not found")
        snapshot = versions[str(version)].get("snapshot") or {}
        previous_number = max((int(v) for v in versions if v.isdigit() and int(v) < int(version or 0)), default=None)
        previous = (versions.get(str(previous_number)) or {}).get("snapshot") or {} if previous_number else {}
        steps = self._steps(case_id, version)
        previous_steps = self._steps(case_id, previous_number) if previous_number else []
        folders = self._folders(str(snapshot.get("project_id", "")))
        def show(field: str, value: Any) -> str:
            if field == "folder_id": return folders.get(str(value), {}).get("path", str(value or ""))
            if isinstance(value, list): return ", ".join(map(str, value))
            return str(value or "")
        changes = [] if not previous_number else [
            {"field": FIELD_LABELS[f], "before": show(f, previous.get(f)), "after": show(f, snapshot.get(f))}
            for f in CONTENT_FIELDS if show(f, previous.get(f)) != show(f, snapshot.get(f))
        ]
        def step_key(items): return [(s.get("action", ""), s.get("expected_result", ""), s.get("test_data", "")) for s in items]
        return {"version": str(version), "snapshot": snapshot, "steps": steps, "previous_version": str(previous_number or ""),
                "changes": changes, "steps_changed": bool(previous_number) and step_key(steps) != step_key(previous_steps),
                "comment": versions[str(version)].get("comment", "")}
    def approval_queue(self, project_id: str) -> dict[str, Any]:
        names = self._user_names(); folders = self._folders(project_id)
        cases = self.store.list("test_cases", filters={"project_id": project_id, "status": PENDING}, page_size=1_000_000)["items"]
        submitted: dict[str, dict[str, Any]] = {}
        for row in self._approval_rows({"decision": "Submitted"}, names):
            key = row.get("test_case_id") or row.get("folder_id")
            submitted.setdefault(key, row)
        pending_folders = [f for f in folders.values() if f.get("approval_status") in {PENDING, "Draft", "Rejected"}]
        case_ids = {c["id"] for c in self.store.list("test_cases", filters={"project_id": project_id}, page_size=1_000_000, include_archived=True)["items"]}
        recent = [r for r in self._approval_rows({}, names) if r.get("test_case_id") in case_ids or r.get("folder_id") in folders][:40]
        case_keys = {c["id"]: c.get("case_key", "") for c in self.store.list("test_cases", filters={"project_id": project_id}, page_size=1_000_000, include_archived=True)["items"]}
        return {
            "cases": [{**c, "folder_path": folders.get(c.get("folder_id", ""), {}).get("path", ""), "submitted_by": (submitted.get(c["id"]) or {}).get("reviewer_name", "—"), "submitted_at": (submitted.get(c["id"]) or {}).get("created_at", "")} for c in cases],
            "folders": [{**f, "case_count": sum(1 for c in self.store.list("test_cases", filters={"folder_id": f["id"]}, page_size=1_000_000)["items"])} for f in sorted(pending_folders, key=lambda f: f.get("path", ""))],
            "recent": [{**r, "case_key": case_keys.get(r.get("test_case_id", ""), ""), "folder_path": folders.get(r.get("folder_id", ""), {}).get("path", "")} for r in recent],
        }
