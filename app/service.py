"""Test-management domain operations above the Excel storage boundary."""
from __future__ import annotations
import csv
import hashlib
import io
import tempfile
import uuid
from pathlib import Path
from typing import Any
from .auth import ADMIN, hash_password
from .storage import ExcelStorageService, VALID_LAST_STATUSES, XlsxCodec, utcnow
from .workflow import RepositoryWorkflow, normalize_title

FORMULA_START = ("=", "+", "-", "@", chr(9), chr(13))  # spreadsheet apps treat these as formulas


class TMExpertService:
    CASE_IMPORT_COLUMNS = ("Scenario Name", "Expected Result", "Folder Path", "Objective", "Preconditions", "Priority", "Test Type", "Tags", "Requirement Keys", "Last Status", "Step Number", "Action", "Step Expected Result", "Test Data")

    def __init__(self, store: ExcelStorageService):
        self.store = store
        self.workflow = RepositoryWorkflow(store)
        from .excel_import import CaseImporter
        self.importer = CaseImporter(store, self)

    @staticmethod
    def _validate_step_numbers(steps: list[dict[str, Any]]) -> None:
        seen: set[int] = set()
        for index, step in enumerate(steps, 1):
            try:
                number = int(str(step.get("step_number") or index))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Step {index} number must be a positive integer") from exc
            if number < 1:
                raise ValueError(f"Step {index} number must be a positive integer")
            if number in seen:
                raise ValueError(f"Step number {number} is duplicated")
            seen.add(number)
            step["step_number"] = str(number)

    def bootstrap(self) -> None:
        if self.store.list("users")["total"]:
            self.workflow.repair_folder_paths()
            return
        admin = self.store.create("users", {"name": "Avery Chen", "email": "avery@tmexpert.local", "role": ADMIN, "password_hash": hash_password("change-me"), "status": "Active", "must_change_password": True})
        project = self.store.create("projects", {"name": "Learning Management System", "key": "LMS", "description": "Quality workspace for the LMS release.", "application": "TMExpert Demo", "product_version": "2.4", "owner_id": admin["id"], "member_ids": [admin["id"]], "status": "Active"})
        root = self.store.create("folders", {"project_id": project["id"], "parent_id": "", "name": "User Management", "path": "/User Management", "approval_status": "Approved"})
        login = self.store.create("folders", {"project_id": project["id"], "parent_id": root["id"], "name": "Login", "path": "/User Management/Login", "approval_status": "Approved"})
        requirement = self.store.create("requirements", {"project_id": project["id"], "parent_id": "", "key": "LMS-101", "title": "Authenticated users can sign in", "description": "Users must access their account with valid credentials.", "type": "Functional", "priority": "High", "version": "1", "owner_id": admin["id"], "status": "Approved", "source_reference": "Product requirements"})
        case = self.create_test_case({"project_id": project["id"], "folder_id": login["id"], "title": "Sign in with valid credentials", "objective": "Verify a registered user can access the LMS.", "priority": "High", "test_type": "Manual", "requirement_ids": [requirement["id"]], "tags": ["smoke", "authentication"], "owner_id": admin["id"], "reviewer_id": admin["id"], "status": "Approved", "steps": [{"action": "Open the sign-in page", "expected_result": "The sign-in form is displayed", "test_data": ""}, {"action": "Submit a valid email and password", "expected_result": "The user dashboard opens", "test_data": "qa.user@tmexpert.local"}]}, admin["id"])
        self.store.create("approvals", {"test_case_id": case["id"], "version": "1", "reviewer_id": admin["id"], "decision": "Approved", "comment": "Baseline coverage approved."})
    def create_test_case(self, data: dict[str, Any], actor_id: str) -> dict[str, Any]:
        data = data.copy()
        steps = data.pop("steps", [])
        if not isinstance(steps, list) or not steps:
            raise ValueError("A test case must have at least one step")
        for index, step in enumerate(steps, 1):
            if not isinstance(step, dict) or not str(step.get("action", "")).strip() or not str(step.get("expected_result", "")).strip():
                raise ValueError(f"Step {index} requires an action and expected result")
        self._validate_step_numbers(steps)
        if not data.get("owner_id") and self.store.get("users", actor_id): data["owner_id"] = actor_id
        data.setdefault("status", "Draft"); data.setdefault("version", "1"); data.setdefault("automation_status", "Not Automated")
        data.setdefault("expected_result", ""); data.setdefault("last_status", "Not Run")
        case = self.store.create("test_cases", data, actor_id)
        self.store.batch_create("test_steps", [
            {"test_case_id": case["id"], "version": case["version"], "step_number": str(step.get("step_number") or index), "action": str(step["action"]).strip(), "expected_result": str(step["expected_result"]).strip(), "test_data": str(step.get("test_data", "")).strip()}
            for index, step in enumerate(steps, 1)
        ], actor_id)
        changed_by = actor_id if self.store.get("users", actor_id) else ""
        self.store.create("test_case_versions", {"test_case_id": case["id"], "version": case["version"], "snapshot": case, "comment": "Initial version", "changed_by": changed_by, "created_at": utcnow()}, actor_id)
        return case

    def create_project(self, data: dict[str, Any], actor_id: str) -> dict[str, Any]:
        """Create an isolated workspace with a usable repository root."""
        owner_id = data.get("owner_id") or (actor_id if self.store.get("users", actor_id) else "")
        if not owner_id:
            users = self.store.list("users", page_size=1)["items"]
            if not users:
                raise ValueError("Create a user before creating a project")
            owner_id = users[0]["id"]
        project = self.store.create("projects", {**data, "owner_id": owner_id, "member_ids": data.get("member_ids") or [owner_id], "status": data.get("status") or "Active"}, actor_id)
        self.store.create("folders", {"project_id": project["id"], "parent_id": "", "name": "Test Repository", "path": "/Test Repository", "approval_status": "Approved"}, actor_id)
        return project

    def create_test_run(self, data: dict[str, Any], actor_id: str) -> dict[str, Any]:
        test_case = self.store.get("test_cases", str(data.get("test_case_id", "")))
        cycle = self.store.get("test_cycles", str(data.get("cycle_id", "")))
        project_id = str(data.get("project_id", ""))
        if not test_case or not cycle:
            raise ValueError("Select an existing test case and execution cycle")
        if test_case.get("project_id") != project_id or cycle.get("project_id") != project_id:
            raise ValueError("The test case, cycle, and run must belong to the same project")
        if str(data.get("test_case_version", test_case.get("version", ""))) != str(test_case.get("version", "")):
            raise ValueError("The selected test case version is no longer current")
        run = self.store.create("test_runs", {
            **data,
            "test_case_version": test_case["version"],
            "status": data.get("status") or "Not Run",
        }, actor_id)
        self.store.update("test_cases", test_case["id"], {"last_status": run["status"]}, actor_id)
        return run

    def move_folder(self, folder_id: str, new_parent_id: str, actor_id: str) -> dict[str, Any]:
        folder = self.store.get("folders", folder_id)
        if not folder:
            raise KeyError("Folder not found")
        new_parent_id = str(new_parent_id or "")
        if new_parent_id == folder_id:
            raise ValueError("Cannot move a folder into itself")
        if new_parent_id:
            parent = self.store.get("folders", new_parent_id)
            if not parent:
                raise ValueError("Target folder does not exist")
            if parent.get("project_id") != folder.get("project_id"):
                raise ValueError("Folders must belong to the same project")
            current = parent
            visited: set[str] = set()
            while current:
                current_id = str(current.get("id", ""))
                if current_id == folder_id:
                    raise ValueError("Cannot move a folder into its own subfolder")
                if current_id in visited:
                    break
                visited.add(current_id)
                parent_id = str(current.get("parent_id", "") or "")
                current = self.store.get("folders", parent_id) if parent_id else None
        if new_parent_id == str(folder.get("parent_id") or ""):
            return folder
        parent = self.store.get("folders", new_parent_id) if new_parent_id else None
        parent_path = str(parent.get("path") or "").rstrip("/") if parent else ""
        new_path = f"{parent_path}/{folder['name']}" if parent_path else f"/{folder['name']}"
        old_path = str(folder.get("path") or "")
        project_folders = self.store.list("folders", filters={"project_id": folder["project_id"]}, page_size=10000)["items"]
        children: dict[str, list[dict[str, Any]]] = {}
        for item in project_folders:
            children.setdefault(str(item.get("parent_id") or ""), []).append(item)
        subtree: list[dict[str, Any]] = []
        stack = [folder]
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            current_id = str(current.get("id", ""))
            if current_id in seen:
                continue
            seen.add(current_id)
            subtree.append(current)
            stack.extend(children.get(current_id, []))
        subtree.sort(key=lambda item: str(item.get("path") or "").count("/"))
        moved: dict[str, Any] = folder
        for item in subtree:
            item_id = str(item.get("id", ""))
            current_path = str(item.get("path") or "")
            if item_id == folder_id:
                updated_path = new_path
            elif old_path and current_path.startswith(old_path):
                updated_path = new_path + current_path[len(old_path):]
            else:
                updated_path = current_path
            changes: dict[str, Any] = {"path": updated_path}
            if item_id == folder_id:
                changes["parent_id"] = new_parent_id
            moved = self.store.update("folders", item_id, changes, actor_id)
        return moved

    def delete_folder(self, folder_id: str, actor_id: str) -> None:
        folder = self.store.get("folders", folder_id)
        if not folder:
            raise KeyError("Folder not found")
        project_id = folder.get("project_id", "")
        child_count = self.store.list("folders", filters={"project_id": project_id, "parent_id": folder_id}, page_size=1)["total"]
        case_count = self.store.list("test_cases", filters={"project_id": project_id, "folder_id": folder_id}, page_size=1)["total"]
        if child_count or case_count:
            details = []
            if child_count:
                details.append(f"{child_count} subfolder{'s' if child_count != 1 else ''}")
            if case_count:
                details.append(f"{case_count} test case{'s' if case_count != 1 else ''}")
            raise ValueError(f"Cannot delete folder with {', '.join(details)}. Move or delete them first.")
        self.store.delete("folders", folder_id, actor_id)

    def update_test_case_last_status(self, test_case_id: str, last_status: str, actor_id: str) -> dict[str, Any]:
        if last_status not in VALID_LAST_STATUSES:
            raise ValueError(f"Last status must be one of: {', '.join(sorted(VALID_LAST_STATUSES))}")
        if not self.store.get("test_cases", test_case_id):
            raise KeyError("Test case not found")
        return self.store.update("test_cases", test_case_id, {"last_status": last_status}, actor_id)

    def update_test_case_approval_status(self, test_case_id: str, status_value: str, actor_id: str, comment: str = "") -> dict[str, Any]:
        """Move a test case through the approval lifecycle (see app.workflow)."""
        return self.workflow.transition_case(test_case_id, status_value, comment, actor_id)

    def approve_test_case(self, record_id: str, reviewer_id: str, decision: str, comment: str, actor_id: str) -> dict[str, Any]:
        if decision not in {"Approved", "Rejected", "Changes Requested"}: raise ValueError("Invalid approval decision")
        target = "Approved" if decision == "Approved" else "Rejected"
        return self.workflow.transition_case(record_id, target, comment, reviewer_id or actor_id)

    def update_test_case(self, record_id: str, changes: dict[str, Any], actor_id: str, comment: str = "Updated") -> dict[str, Any]:
        existing = self.store.get("test_cases", record_id)
        if not existing: raise KeyError("Test case not found")
        if existing.get("archived") is True: raise ValueError("Restore the test case before editing it")
        changes = {k: v for k, v in changes.items() if k not in {"status", "version", "case_key", "project_id", "last_status", "created_by", "updated_by", "created_at", "updated_at", "archived"}}
        submitted_steps = changes.pop("steps", None)
        current_steps = self.store.list("test_steps", filters={"test_case_id": record_id}, page_size=1_000_000)["items"]
        current_steps = sorted((step for step in current_steps if str(step.get("version", "")) == str(existing.get("version", ""))), key=lambda step: int(step.get("step_number") or 0))
        steps = submitted_steps if submitted_steps is not None else current_steps
        if not isinstance(steps, list):
            raise ValueError("Test case steps must be a list")
        for index, step in enumerate(steps, 1):
            if not isinstance(step, dict) or not str(step.get("action", "")).strip() or not str(step.get("expected_result", "")).strip():
                raise ValueError(f"Step {index} requires an action and expected result")
        if not steps and current_steps:
            raise ValueError("A test case must have at least one step")
        self._validate_step_numbers(steps)
        if "title" in changes and not str(changes["title"]).strip():
            raise ValueError("Title is required")
        if "folder_id" in changes:
            folder = self.store.get("folders", str(changes["folder_id"]), include_archived=False)
            if not folder or folder.get("project_id") != existing.get("project_id"): raise ValueError("Choose a module in this project")
        # Requirement links and automation details are metadata: they never create a version or reset approval.
        metadata = {k: changes.pop(k) for k in list(changes) if k in {"requirement_ids", "automation_status", "automation_reference"}}
        if "requirement_ids" in metadata:
            ids = metadata["requirement_ids"] if isinstance(metadata["requirement_ids"], list) else []
            metadata["requirement_ids"] = [str(i) for i in dict.fromkeys(ids) if str(i)]
        if "automation_status" in metadata and metadata["automation_status"] not in {"Not Automated", "Automated", "In Progress", "Not Applicable"}:
            raise ValueError("Automation status must be Not Automated, In Progress, Automated, or Not Applicable")
        if "automation_reference" in metadata: metadata["automation_reference"] = str(metadata["automation_reference"] or "").strip()[:300]
        metadata = {k: v for k, v in metadata.items() if existing.get(k) != v}
        step_key = lambda items: [(str(s.get("action", "")).strip(), str(s.get("expected_result", "")).strip(), str(s.get("test_data", "")).strip()) for s in items]
        field_changes = {k: v for k, v in changes.items() if existing.get(k) != v}
        steps_changed = step_key(steps) != step_key(current_steps)
        if metadata:  # saved only after all validation above has passed
            existing = self.store.update("test_cases", record_id, metadata, actor_id)
        if not field_changes and not steps_changed:
            return existing  # no content change: no new version, approval untouched
        next_version = str(int(existing.get("version", "0") or 0) + 1)
        field_changes["version"] = next_version
        if self.workflow.approvals_enabled and existing.get("status") in {"Approved", "Rejected"}:
            field_changes["status"] = "Draft"  # revised content must be approved again
        updated = self.store.update("test_cases", record_id, field_changes, actor_id)
        if steps:
            self.store.batch_create("test_steps", [
                {
                    "test_case_id": record_id, "version": next_version,
                    "step_number": str(step.get("step_number") or index),
                    "action": str(step.get("action", "")).strip(),
                    "expected_result": str(step.get("expected_result", "")).strip(),
                    "test_data": str(step.get("test_data", "")).strip(),
                }
                for index, step in enumerate(steps, 1)
            ], actor_id)
        changed_by = actor_id if self.store.get("users", actor_id) else ""
        self.store.create("test_case_versions", {"test_case_id": record_id, "version": updated["version"], "snapshot": updated, "comment": comment, "changed_by": changed_by, "created_at": utcnow()}, actor_id)
        return updated
    def dashboard(self, project_id: str = "") -> dict[str, Any]:
        filt = {"project_id": project_id} if project_id else {}
        cases = self.store.list("test_cases", filters=filt, page_size=10000)["items"]
        requirements = self.store.list("requirements", filters=filt, page_size=10000)["items"]
        runs = self.store.list("test_runs", filters=filt, page_size=10000)["items"]
        defects = self.store.list("defects", filters=filt, page_size=10000)["items"]
        return {"test_cases": len(cases), "approved": sum(c.get("status") == "Approved" for c in cases), "in_review": sum(c.get("status") == "In Review" for c in cases), "requirements": len(requirements), "covered_requirements": len({rid for c in cases for rid in (c.get("requirement_ids") or [])}), "runs": len(runs), "passed": sum(r.get("status") == "Passed" for r in runs), "failed": sum(r.get("status") == "Failed" for r in runs), "open_defects": sum(d.get("status") not in {"Closed", "Resolved"} for d in defects), "recent_cases": sorted(cases, key=lambda x: x.get("updated_at", ""), reverse=True)[:6]}
    def export_cases_csv(self, project_id: str) -> bytes:
        if not project_id or not self.store.get("projects", project_id):
            raise ValueError("Select a valid project before exporting test cases")
        rows = self.store.list("test_cases", filters={"project_id": project_id}, page_size=10000)["items"]; output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["id", "title", "objective", "priority", "test_type", "status", "version", "tags"]); writer.writeheader()
        def safe(value: Any) -> str:  # stop spreadsheet apps from treating text as a formula
            text = ", ".join(value) if isinstance(value, list) else str(value or "")
            return "'" + text if text[:1] in FORMULA_START else text
        writer.writerows({key: safe(row.get(key, "")) for key in writer.fieldnames} for row in rows)
        return output.getvalue().encode()

    def export_cases_xlsx(self, project_id: str, template: bool = False, include_ids: bool = False) -> bytes:
        if not template and (not project_id or not self.store.get("projects", project_id)):
            raise ValueError("Select a valid project before exporting test cases")
        rows: list[dict[str, Any]] = []
        if not template:
            folders = {folder["id"]: folder.get("path", folder.get("name", "")) for folder in self.store.list("folders", filters={"project_id": project_id}, page_size=10000)["items"]}
            requirements = {requirement["id"]: requirement["key"] for requirement in self.store.list("requirements", filters={"project_id": project_id}, page_size=10000)["items"]}
            cases = self.store.list("test_cases", filters={"project_id": project_id}, page_size=10000)["items"]
            steps = self.store.list("test_steps", page_size=10000)["items"]
            case_by_id = {case["id"]: case for case in cases}
            steps_by_case: dict[str, list[dict[str, Any]]] = {}
            for step in steps:
                case = case_by_id.get(step["test_case_id"])
                if case and str(step.get("version", "")) == str(case.get("version", "")):
                    steps_by_case.setdefault(step["test_case_id"], []).append(step)
            for case in cases:
                matching_steps = sorted(steps_by_case.get(case["id"], []), key=lambda item: int(item.get("step_number") or 0))
                for step in matching_steps or [{}]:
                    rows.append({
                        "Scenario Name": case.get("title", ""),
                        "Expected Result": case.get("expected_result", ""),
                        "Folder Path": folders.get(case.get("folder_id", ""), ""),
                        "Objective": case.get("objective", ""),
                        "Preconditions": case.get("preconditions", ""),
                        "Priority": case.get("priority", ""),
                        "Test Type": case.get("test_type", ""),
                        "Tags": ", ".join(case.get("tags", [])) if isinstance(case.get("tags"), list) else case.get("tags", ""),
                        "Requirement Keys": ", ".join(requirements[item] for item in case.get("requirement_ids", []) if item in requirements) if isinstance(case.get("requirement_ids"), list) else case.get("requirement_ids", ""),
                        "Last Status": case.get("last_status", "Not Run"),
                        "Step Number": step.get("step_number", ""),
                        "Action": step.get("action", ""),
                        "Step Expected Result": step.get("expected_result", ""),
                        "Test Data": step.get("test_data", ""),
                        "Test Case ID": case.get("case_key", ""),
                    })
        with tempfile.NamedTemporaryFile(suffix=".xlsx", dir=self.store.root / "temp", delete=False) as temp:
            path = Path(temp.name)
        try:
            # The standard layout is unchanged; "Test Case ID" is appended only when asked for.
            XlsxCodec.write(path, self.CASE_IMPORT_COLUMNS + (("Test Case ID",) if include_ids and not template else ()), rows)
            return path.read_bytes()
        finally:
            path.unlink(missing_ok=True)

    def import_test_cases_xlsx(self, workbook: bytes, project_id: str, actor_id: str, target_folder_id: str = "", allow_duplicates: bool = False,
                               create_missing_modules: bool = False, sheet: str = "", filename: str = "") -> dict[str, Any]:
        """Import test cases from the Excel template (see app.excel_import). Returns the original result shape."""
        result = self.importer.commit(workbook, project_id, {"target_folder_id": target_folder_id, "allow_duplicates": allow_duplicates,
                                                             "create_missing_modules": create_missing_modules, "sheet": sheet}, actor_id, filename)
        if not result["created"]:
            errors = [{"row": e["row"], "error": e["message"], **({"duplicate": True} if e.get("duplicate") else {})} for e in result["plan"]["errors"]]
            errors = errors or [{"row": 1, "error": "No test cases were found in the workbook"}]
            return {"created": 0, "errors": errors, "duplicates_only": all(e.get("duplicate") for e in errors), "message": "Nothing imported; correct the reported rows and try again."}
        out: dict[str, Any] = {"created": result["created"], "errors": []}
        if result["requirements_created"]: out["requirements_created"] = result["requirements_created"]
        return out

    def execution_detail(self, run_id: str) -> dict[str, Any]:
        run = self.store.get("test_runs", run_id)
        if not run:
            raise KeyError("Test run not found")
        case = self.store.get("test_cases", run["test_case_id"])
        if not case:
            raise ValueError("The test run references a missing test case")
        versions = self.store.list("test_case_versions", filters={"test_case_id": case["id"]}, page_size=10000)["items"]
        snapshot = next((item.get("snapshot") for item in versions if str(item.get("version", "")) == str(run.get("test_case_version", ""))), None)
        if isinstance(snapshot, dict):
            case = snapshot
        steps = self.store.list("test_steps", filters={"test_case_id": case["id"]}, page_size=10000)["items"]
        version_steps = [step for step in steps if str(step.get("version", "")) == str(run.get("test_case_version", ""))]
        if version_steps:
            steps = version_steps
        steps.sort(key=lambda step: int(step.get("step_number") or 0))
        results = self.store.list("execution_results", filters={"test_run_id": run_id}, page_size=10000)["items"]
        results_by_step = {result.get("step_id"): result for result in results}
        return {
            "run": run,
            "test_case": case,
            "steps": [
                {"step": step, "result": results_by_step.get(step["id"], {})}
                for step in steps
            ],
        }

    def write_step_results(self, run_id: str, submitted: list[dict[str, Any]], actor_id: str) -> str:
        """Validate and store step results (current state); return the status the steps imply."""
        detail = self.execution_detail(run_id)
        step_ids = {item["step"]["id"] for item in detail["steps"]}
        if not isinstance(submitted, list) or not submitted:
            raise ValueError("Record a result for at least one test step")
        seen: set[str] = set()
        for result in submitted:
            if not isinstance(result, dict): raise ValueError("Each step result must be an object")
            step_id = str(result.get("step_id", ""))
            if step_id not in step_ids or step_id in seen:
                raise ValueError("Execution results must reference each run step at most once")
            seen.add(step_id)
            if result.get("status") not in {"Not Run", "In Progress", "Passed", "Failed", "Blocked", "Not Applicable"}:
                raise ValueError(f"Invalid execution status for step {step_id}")
            if result["status"] in {"Passed", "Failed"} and not str(result.get("actual_result", "")).strip():
                raise ValueError(f"Actual Result is required when a step is {result['status']}")
            if len(str(result.get("actual_result", ""))) > 2000 or len(str(result.get("comment", ""))) > 2000:
                raise ValueError("Step results must be 2000 characters or fewer")
        existing = {item["result"].get("step_id"): item["result"] for item in detail["steps"] if item["result"]}
        updates: dict[str, dict[str, Any]] = {}; creates: list[dict[str, Any]] = []
        for result in submitted:
            data = {"status": result["status"], "actual_result": str(result.get("actual_result", "")).strip(), "comment": str(result.get("comment", "")).strip()}
            prior = existing.get(result["step_id"])
            if prior:
                if any(prior.get(k, "") != v for k, v in data.items()): updates[prior["id"]] = data
            else:
                step = next(item["step"] for item in detail["steps"] if item["step"]["id"] == result["step_id"])
                creates.append({"test_run_id": run_id, "step_id": step["id"], "step_number": step["step_number"], **data})
        if updates: self.store.bulk_update("execution_results", updates, actor_id)
        if creates: self.store.batch_create("execution_results", creates, actor_id)
        updated_results = {r.get("step_id"): r for r in self.store.list("execution_results", filters={"test_run_id": run_id}, page_size=1_000_000)["items"]}
        statuses = [updated_results.get(item["step"]["id"], {}).get("status", "Not Run") for item in detail["steps"]]
        return self.derive_run_status(statuses)

    @staticmethod
    def derive_run_status(statuses: list[str]) -> str:
        if any(status == "Failed" for status in statuses): return "Failed"
        if any(status == "Blocked" for status in statuses): return "Blocked"
        if statuses and all(status == "Not Applicable" for status in statuses): return "Not Applicable"
        if statuses and all(status in {"Passed", "Not Applicable"} for status in statuses): return "Passed"
        if any(status != "Not Run" for status in statuses): return "In Progress"
        return "Not Run"

    def save_execution_results(self, run_id: str, submitted: list[dict[str, Any]], actor_id: str) -> dict[str, Any]:
        run_status = self.write_step_results(run_id, submitted, actor_id)
        detail = self.execution_detail(run_id)
        changes: dict[str, Any] = {"status": run_status}
        if run_status != "Not Run":
            changes["started_at"] = detail["run"].get("started_at") or utcnow()
        if run_status not in {"Not Run", "In Progress"}:
            changes["completed_at"] = utcnow()
        else:
            changes["completed_at"] = ""
        self.store.update("test_runs", run_id, changes, actor_id)
        self.store.update("test_cases", detail["test_case"]["id"], {"last_status": run_status}, actor_id)
        return self.execution_detail(run_id)

    def update_test_run_status(self, run_id: str, status: str, actor_id: str) -> dict[str, Any]:
        if status not in VALID_LAST_STATUSES:
            raise ValueError(f"Run status must be one of: {', '.join(sorted(VALID_LAST_STATUSES))}")
        run = self.store.get("test_runs", run_id)
        if not run:
            raise KeyError("Test run not found")
        changes: dict[str, Any] = {"status": status}
        if status != "Not Run":
            changes["started_at"] = run.get("started_at") or utcnow()
        else:
            changes["started_at"] = run.get("started_at") or ""
        if status not in {"Not Run", "In Progress"}:
            changes["completed_at"] = utcnow()
        else:
            changes["completed_at"] = ""
        updated_run = self.store.update("test_runs", run_id, changes, actor_id)
        if run.get("test_case_id"):
            self.store.update("test_cases", run["test_case_id"], {"last_status": status}, actor_id)
        return updated_run
