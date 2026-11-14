"""Excel import of test cases: template recognition, preview, validation and commit.

The import format is unchanged (see TMExpertService.CASE_IMPORT_COLUMNS and the
column aliases below).  ``plan`` reads and validates a workbook without writing
anything; ``commit`` writes a valid plan after backing up every affected workbook,
keeps the uploaded file, and records the import in history/Import_History.xlsx.
Optional extras are opt-in only: a "Test Case ID" column, creating missing
modules, and allowing duplicates.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import uuid
from typing import Any

from .storage import ExcelStorageService, VALID_LAST_STATUSES, XlsxCodec, _retry_io, utcnow
from .workflow import normalize_title

STANDARD_COLUMNS = ("Scenario Name", "Expected Result", "Folder Path", "Objective", "Preconditions", "Priority", "Test Type", "Tags",
                    "Requirement Keys", "Last Status", "Step Number", "Action", "Step Expected Result", "Test Data")
ALIASES = {
    "scenario name": "title", "scenario": "title", "test case": "title", "title": "title", "case title": "title",
    "folder path": "folder", "folder": "folder", "module": "folder", "module path": "folder",
    "objective": "objective", "preconditions": "preconditions",
    "priority": "priority", "test type": "test_type", "type": "test_type",
    "tags": "tags", "requirement keys": "requirements", "requirements": "requirements",
    "expected result": "expected_result", "scenario expected result": "expected_result",
    "last status": "last_status",
    "step number": "step_number", "step #": "step_number",
    "action": "action", "step expected result": "step_expected_result",
    "expected outcome": "step_expected_result", "test data": "test_data",
    "test case id": "case_key", "case id": "case_key", "tc id": "case_key",
}
FIELD_LABELS = {"title": "Scenario Name", "folder": "Folder Path", "objective": "Objective", "preconditions": "Preconditions", "priority": "Priority",
                "test_type": "Test Type", "tags": "Tags", "requirements": "Requirement Keys", "expected_result": "Expected Result", "last_status": "Last Status",
                "step_number": "Step Number", "action": "Action", "step_expected_result": "Step Expected Result", "test_data": "Test Data", "case_key": "Test Case ID"}
PRIORITIES = {"Critical", "High", "Medium", "Low"}
EXCEL_ERROR = re.compile(r"^#(N/A|REF!|VALUE!|DIV/0!|NAME\?|NUM!|NULL!|SPILL!|CALC!|GETTING_DATA)$", re.IGNORECASE)
CASE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
MAX_TITLE = 200
MAX_BYTES = 10 * 1024 * 1024


def _norm_path(label: str) -> str:
    return "/" + "/".join(part.strip() for part in label.replace("\\", "/").split("/") if part.strip())


class CaseImporter:
    def __init__(self, store: ExcelStorageService, service: Any):
        self.store = store
        self.service = service

    # ----------------------------------------------------------------- reading
    def _choose_sheet(self, workbook: bytes, requested: str) -> tuple[str, list[str], list[str], list[dict[str, Any]]]:
        sheets = XlsxCodec.sheet_names(workbook)
        if requested:
            headers, rows = XlsxCodec.read_bytes(workbook, requested)
            return requested, sheets, headers, rows
        # Use the first sheet that has a test case title column (e.g. skip an "Instructions" sheet).
        for name in sheets:
            headers, rows = XlsxCodec.read_bytes(workbook, name)
            if any(ALIASES.get(str(h).strip().casefold()) == "title" for h in headers): return name, sheets, headers, rows
        if not sheets: return "", sheets, [], []
        headers, rows = XlsxCodec.read_bytes(workbook, sheets[0])
        return sheets[0], sheets, headers, rows

    @staticmethod
    def recognise(headers: list[str]) -> dict[str, Any]:
        matched, ignored, seen = [], [], set()
        for header in headers:
            label = str(header).strip()
            if not label: continue
            field = ALIASES.get(label.casefold())
            if field and field not in seen: matched.append({"column": label, "field": FIELD_LABELS[field]}); seen.add(field)
            elif field: ignored.append({"column": label, "reason": f"Duplicate of the {FIELD_LABELS[field]} column"})
            else: ignored.append({"column": label, "reason": "Not a TMExpert column; ignored"})
        standard = sum(1 for column in STANDARD_COLUMNS if column.casefold() in {str(h).strip().casefold() for h in headers})
        kind = "standard" if standard == len(STANDARD_COLUMNS) else "partial" if "title" in seen else "unrecognised"
        return {"matched": matched, "ignored": ignored, "missing_required": [] if "title" in seen else ["Scenario Name"],
                "template": kind, "standard_columns": standard, "standard_total": len(STANDARD_COLUMNS), "has_id_column": "case_key" in seen}

    # -------------------------------------------------------------------- plan
    def plan(self, workbook: bytes, project_id: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options or {}
        if not project_id or not self.store.get("projects", project_id): raise ValueError("Select a valid project before importing test cases")
        if not workbook or len(workbook) > MAX_BYTES: raise ValueError("The Excel file must be smaller than 10 MB")
        target_folder = None
        if options.get("target_folder_id"):
            target_folder = self.store.get("folders", str(options["target_folder_id"]), include_archived=False)
            if not target_folder or target_folder.get("project_id") != project_id: raise ValueError("Selected folder does not belong to this project")
        allow_duplicates = options.get("allow_duplicates") is True
        create_modules = options.get("create_missing_modules") is True
        sheet, sheets, headers, rows = self._choose_sheet(workbook, str(options.get("sheet") or ""))
        recognition = self.recognise(headers)
        errors: list[dict[str, Any]] = []; warnings: list[dict[str, Any]] = []
        base = {"sheet": sheet, "sheets": sheets, "recognition": recognition, "rows_read": len(rows), "sha256": hashlib.sha256(workbook).hexdigest(),
                "options": {"target_folder_id": target_folder["id"] if target_folder else "", "target_folder_path": target_folder.get("path", "") if target_folder else "",
                            "allow_duplicates": allow_duplicates, "create_missing_modules": create_modules}}
        if not headers:
            return {**base, "cases": [], "errors": [{"row": 1, "column": "", "message": "The sheet has no header row"}], "warnings": [], "can_import": False, "summary": self._summary([], [{"row": 1}], [], [], [])}
        if recognition["missing_required"]:
            return {**base, "cases": [], "errors": [{"row": 1, "column": "Scenario Name", "message": "The sheet has no Scenario Name column. Download the Excel template to see the expected columns."}],
                    "warnings": [], "can_import": False, "summary": self._summary([], [{"row": 1}], [], [], [])}
        for item in recognition["ignored"]:
            warnings.append({"row": 1, "column": item["column"], "message": item["reason"]})

        folders = self.store.list("folders", filters={"project_id": project_id}, page_size=1_000_000)["items"]
        root_folders = [f for f in folders if not f.get("parent_id")]
        requirements = {r["key"].casefold(): r["id"] for r in self.store.list("requirements", filters={"project_id": project_id}, page_size=1_000_000)["items"]}
        existing_cases = self.store.list("test_cases", filters={"project_id": project_id}, page_size=1_000_000, include_archived=True)["items"]
        existing_keys = {str(c.get("case_key") or "").casefold(): c for c in existing_cases if c.get("case_key")}
        active_index = {(normalize_title(c.get("title")), c.get("folder_id")): c for c in existing_cases if c.get("archived") is not True}
        folder_paths = {f["id"]: f.get("path", "") for f in folders}

        cases: dict[tuple[str, str], dict[str, Any]] = {}
        last_key: tuple[str, str] | None = None
        for row_number, row in enumerate(rows, 2):
            values: dict[str, Any] = {}
            for header, value in row.items():
                field = ALIASES.get(str(header).strip().casefold())
                if field and field not in values: values[field] = value
            for field, value in values.items():
                if isinstance(value, str) and EXCEL_ERROR.match(value.strip()):
                    errors.append({"row": row_number, "column": FIELD_LABELS[field], "message": f"Contains the Excel error value {value.strip()}"})
            title = str(values.get("title", "") or "").strip()
            folder_label = str(values.get("folder", "") or "").strip()
            if not title:
                if last_key is None:
                    errors.append({"row": row_number, "column": "Scenario Name", "message": "Scenario Name is required on the first row of each test case"}); continue
                case = cases[last_key]
            else:
                key = (title.casefold(), "" if target_folder else folder_label.casefold())
                if not folder_label and last_key and last_key[0] == title.casefold(): key = last_key
                last_key = key
                if key not in cases:
                    cases[key] = {"row": row_number, "title": title, "folder_label": folder_label,
                                  "case_key": str(values.get("case_key", "") or "").strip(),
                                  "objective": str(values.get("objective", "") or "").strip(), "expected_result": str(values.get("expected_result", "") or "").strip(),
                                  "preconditions": str(values.get("preconditions", "") or "").strip(),
                                  "priority": str(values.get("priority", "") or "Medium").strip().title(), "priority_defaulted": not str(values.get("priority", "") or "").strip(),
                                  "test_type": str(values.get("test_type", "") or "Manual").strip().title(),
                                  "tags": [t.strip() for t in str(values.get("tags", "") or "").split(",") if t.strip()],
                                  "last_status": str(values.get("last_status", "") or "Not Run").strip().title(),
                                  "requirement_keys": [], "steps": [], "messages": [], "rows": []}
                case = cases[key]
            case["rows"].append(row_number)
            action = str(values.get("action", "") or "").strip()
            step_expected = str(values.get("step_expected_result", "") or values.get("expected_result", "") or "").strip()
            if action or values.get("step_expected_result"):
                if not action or not step_expected:
                    errors.append({"row": row_number, "column": "Action" if not action else "Step Expected Result", "message": "Action and Step Expected Result must both be provided"})
                else:
                    case["steps"].append({"step_number": str(values.get("step_number", "") or len(case["steps"]) + 1).strip(), "action": action,
                                          "expected_result": step_expected, "test_data": str(values.get("test_data", "") or "").strip(), "row": row_number})
            if not case.get("expected_result") and values.get("expected_result"): case["expected_result"] = str(values["expected_result"]).strip()
            for raw in str(values.get("requirements", "") or "").split(","):
                if raw.strip() and raw.strip().casefold() not in {k.casefold() for k in case["requirement_keys"]}: case["requirement_keys"].append(raw.strip())

        new_modules: dict[str, str] = {}   # normalised path -> display path
        file_keys: dict[str, int] = {}
        planned: list[dict[str, Any]] = []
        for case in cases.values():
            row = case["row"]; problems: list[str] = []; hard: list[bool] = []
            def fail(column: str, message: str, duplicate: bool = False) -> None:
                errors.append({"row": row, "column": column, "message": message, **({"duplicate": True} if duplicate else {})}); problems.append(message)
                if not duplicate: hard.append(True)
            if len(case["title"]) > MAX_TITLE: fail("Scenario Name", f"Scenario Name is longer than {MAX_TITLE} characters")
            if case["priority"] not in PRIORITIES and not EXCEL_ERROR.match(case["priority"]):  # an Excel error is already reported for the cell
                fail("Priority", "Priority must be Critical, High, Medium, or Low")
            if case["last_status"] not in VALID_LAST_STATUSES and not EXCEL_ERROR.match(case["last_status"]): fail("Last Status", "Last Status must be Not Run, In Progress, Passed, Failed, Blocked, or Not Applicable")
            if case["steps"]:
                try: self.service._validate_step_numbers([dict(s) for s in case["steps"]])
                except ValueError as exc: fail("Step Number", str(exc))
            if case["case_key"]:
                folded = case["case_key"].casefold()
                if not CASE_KEY.match(case["case_key"]): fail("Test Case ID", "Test Case ID may use letters, numbers, '.', '_' and '-' (up to 40 characters)")
                elif folded in file_keys: fail("Test Case ID", f"Test Case ID {case['case_key']} is also used on row {file_keys[folded]}")
                elif folded in existing_keys: fail("Test Case ID", f"Test Case ID {case['case_key']} already exists in the repository. Imports create new test cases; edit existing ones in the repository.")
                file_keys.setdefault(folded, row)
            # Module
            folder_id, module_path, module_new = "", "", False
            if target_folder:
                folder_id, module_path = target_folder["id"], target_folder.get("path", "")
            else:
                label = case["folder_label"]
                match = [f for f in folders if label and f.get("path", "").casefold() == _norm_path(label).casefold()]
                if not match and label:
                    wanted = [p.casefold() for p in label.strip("/").split("/") if p.strip()]
                    match = [f for f in folders if wanted and [p.casefold() for p in str(f.get("path", "")).strip("/").split("/")][-len(wanted):] == wanted]
                if not match and label:
                    match = [f for f in folders if f.get("name", "").casefold() == label.strip("/").casefold()]
                if not label and root_folders:
                    preferred = next((f for f in root_folders if f.get("name", "").casefold() == "test repository"), None)
                    match = [preferred or sorted(root_folders, key=lambda f: f.get("name", "").casefold())[0]]
                if len(match) == 1:
                    folder_id, module_path = match[0]["id"], match[0].get("path", "")
                elif len(match) > 1:
                    fail("Folder Path", f"“{label}” matches {len(match)} modules: {', '.join(sorted(f.get('path', '') for f in match)[:3])}. Use the full path.")
                elif label and create_modules:
                    module_path, module_new = _norm_path(label), True
                    new_modules[module_path.casefold()] = module_path
                else:
                    options_list = [f.get("path", "") for f in folders]
                    close = difflib.get_close_matches(_norm_path(label), options_list, n=1, cutoff=0.6) if label else []
                    hint = (f" Did you mean {close[0]}? If it is a new module, tick “Create modules that don't exist yet”." if close
                            else " Tick “Create modules that don't exist yet” to create it.")
                    fail("Folder Path", f"No module matches “{label}”.{hint}" if label else "The project has no module to import into")
            duplicate_of = None
            if folder_id and not module_new:
                duplicate_of = active_index.get((normalize_title(case["title"]), folder_id))
                if duplicate_of and not allow_duplicates:
                    fail("Scenario Name", f"“{case['title']}” already exists in {folder_paths.get(folder_id, 'this module')} as {duplicate_of.get('case_key') or 'an existing case'}", duplicate=True)
            new_reqs = [k for k in case["requirement_keys"] if k.casefold() not in requirements]
            # Problems found while reading any of this case's rows (e.g. Excel error values) count too.
            row_problems = [e["message"] for e in errors if e["row"] in case["rows"] and not e.get("duplicate") and e["message"] not in problems]
            problems.extend(row_problems)
            status = "error" if hard or row_problems else "duplicate" if duplicate_of and not allow_duplicates else "ready"
            planned.append({"row": row, "title": case["title"], "case_key": case["case_key"], "module_path": module_path, "folder_id": folder_id, "module_new": module_new,
                            "priority": case["priority"], "priority_defaulted": case["priority_defaulted"], "test_type": case["test_type"], "tags": case["tags"],
                            "last_status": case["last_status"], "objective": case["objective"], "expected_result": case["expected_result"], "preconditions": case["preconditions"],
                            "requirement_keys": case["requirement_keys"], "new_requirement_keys": new_reqs, "steps": case["steps"],
                            "duplicate_of": duplicate_of.get("case_key", "") if duplicate_of else "", "status": status, "messages": problems})
        if not planned and not errors:
            errors.append({"row": 1, "column": "", "message": "No test cases were found in the sheet"})
        new_requirements = sorted({k.casefold(): k for c in planned for k in c["new_requirement_keys"]}.values(), key=str.casefold)
        if new_requirements: warnings.append({"row": 0, "column": "Requirement Keys", "message": f"{len(new_requirements)} new requirement key(s) will be created: {', '.join(new_requirements[:8])}{'…' if len(new_requirements) > 8 else ''}"})
        if new_modules:
            warnings.append({"row": 0, "column": "Folder Path", "message": f"{len(new_modules)} new module(s) will be created as Draft: {', '.join(sorted(new_modules.values())[:8])}"})
            existing_paths = [f.get("path", "") for f in folders]
            for path in sorted(new_modules.values()):
                near = difflib.get_close_matches(path, existing_paths, n=1, cutoff=0.9)
                if near: warnings.append({"row": 0, "column": "Folder Path", "message": f"New module {path} is almost the same as existing {near[0]} — check for a typo"})
        no_steps = sum(1 for c in planned if not c["steps"])
        if no_steps: warnings.append({"row": 0, "column": "Action", "message": f"{no_steps} test case(s) have no steps"})
        defaulted = sum(1 for c in planned if c["priority_defaulted"])
        if defaulted: warnings.append({"row": 0, "column": "Priority", "message": f"{defaulted} test case(s) have no priority and will be Medium"})
        errors.sort(key=lambda e: (e["row"], e["column"]))
        return {**base, "cases": planned, "errors": errors, "warnings": warnings, "new_modules": sorted(new_modules.values()), "new_requirements": new_requirements,
                "can_import": bool(planned) and not errors, "summary": self._summary(planned, errors, warnings, new_requirements, list(new_modules))}

    @staticmethod
    def _summary(planned, errors, warnings, new_requirements, new_modules) -> dict[str, int]:
        return {"cases": len(planned), "ready": sum(1 for c in planned if c["status"] == "ready"), "steps": sum(len(c["steps"]) for c in planned),
                "duplicates": sum(1 for c in planned if c.get("duplicate_of")), "errors": len(errors), "warnings": len(warnings),
                "new_requirements": len(new_requirements), "new_modules": len(new_modules)}

    @staticmethod
    def public(plan: dict[str, Any], limit: int = 500) -> dict[str, Any]:
        """The preview sent to the browser (steps summarised, long lists capped)."""
        cases = [{k: v for k, v in c.items() if k not in {"steps", "objective", "expected_result", "preconditions", "folder_id"}} | {"steps": len(c["steps"])} for c in plan["cases"][:limit]]
        return {**{k: v for k, v in plan.items() if k != "cases"}, "cases": cases, "cases_truncated": max(0, len(plan["cases"]) - limit)}

    # ------------------------------------------------------------------ commit
    def commit(self, workbook: bytes, project_id: str, options: dict[str, Any], actor_id: str, filename: str = "") -> dict[str, Any]:
        plan = self.plan(workbook, project_id, options)
        name = os.path.basename(str(filename or "upload.xlsx"))[:150] or "upload.xlsx"
        if not plan["can_import"]:
            self._history(project_id, plan, name, actor_id, "Rejected", {}, "")
            return {"created": 0, "plan": plan}
        now = utcnow()
        self.store.backup_now(("folders", "requirements", "test_cases", "test_steps", "test_case_versions"))
        history_id = str(uuid.uuid4())
        stored = f"{history_id}.xlsx"
        target = self.store.root / "imports" / stored
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".part")
        temporary.write_bytes(workbook); _retry_io(lambda: os.replace(temporary, target))

        # 1. Missing modules (top-down), as Draft.
        modules_created = 0
        folder_by_path = {f.get("path", "").casefold(): f["id"] for f in self.store.list("folders", filters={"project_id": project_id}, page_size=1_000_000)["items"]}
        for path in plan.get("new_modules", []):
            parent_id, built = "", ""
            for part in [p for p in path.split("/") if p]:
                built = f"{built}/{part}"
                if built.casefold() not in folder_by_path:
                    folder = self.service.workflow.create_folder({"project_id": project_id, "parent_id": parent_id, "name": part}, actor_id)
                    folder_by_path[built.casefold()] = folder["id"]; modules_created += 1
                parent_id = folder_by_path[built.casefold()]
        # 2. Requirement keys that do not exist yet.
        requirement_ids = {r["key"].casefold(): r["id"] for r in self.store.list("requirements", filters={"project_id": project_id}, page_size=1_000_000)["items"]}
        new_reqs = [k for k in plan.get("new_requirements", []) if k.casefold() not in requirement_ids]
        if new_reqs:
            for r in self.store.batch_create("requirements", [{"project_id": project_id, "parent_id": "", "key": k, "title": k, "description": "", "type": "Functional",
                                                               "priority": "Medium", "version": "1", "owner_id": "", "status": "Draft", "source_reference": "Imported from Excel test cases"} for k in new_reqs], actor_id):
                requirement_ids[r["key"].casefold()] = r["id"]
        # 3. Test cases, steps and their first version.
        records, step_records = [], []
        for case in plan["cases"]:
            case_id = str(uuid.uuid4())
            folder_id = case["folder_id"] or folder_by_path[case["module_path"].casefold()]
            record = {"id": case_id, "project_id": project_id, "folder_id": folder_id, "title": case["title"], "objective": case["objective"],
                      "expected_result": case["expected_result"], "preconditions": case["preconditions"], "priority": case["priority"], "test_type": case["test_type"],
                      "tags": case["tags"], "owner_id": "", "reviewer_id": "", "status": "Draft", "version": "1", "automation_status": "Not Automated",
                      "last_status": case["last_status"], "created_at": now, "updated_at": now}
            if case["case_key"]: record["case_key"] = case["case_key"]
            ids = [requirement_ids[k.casefold()] for k in case["requirement_keys"] if k.casefold() in requirement_ids]
            if ids: record["requirement_ids"] = ids
            records.append(record)
            step_records += [{"test_case_id": case_id, "version": "1", "step_number": s["step_number"] or str(i), "action": s["action"],
                              "expected_result": s["expected_result"], "test_data": s["test_data"]} for i, s in enumerate(case["steps"], 1)]
        created = self.store.batch_create("test_cases", records, actor_id)
        if step_records: self.store.batch_create("test_steps", step_records, actor_id)
        changed_by = actor_id if self.store.get("users", actor_id) else ""
        self.store.batch_create("test_case_versions", [{"test_case_id": c["id"], "version": "1", "snapshot": c, "comment": f"Imported from Excel ({name})",
                                                        "changed_by": changed_by, "created_at": now} for c in created], actor_id)
        counts = {"cases_created": len(created), "steps_created": len(step_records), "requirements_created": len(new_reqs), "modules_created": modules_created}
        self._history(project_id, plan, name, actor_id, "Imported", counts, stored, history_id)
        return {"created": len(created), "plan": plan, **counts, "history_id": history_id, "case_ids": [c["id"] for c in created]}

    def _history(self, project_id: str, plan: dict[str, Any], filename: str, actor_id: str, status: str, counts: dict[str, int], stored: str, history_id: str = "") -> None:
        self.store.create("import_history", {
            "id": history_id or str(uuid.uuid4()), "project_id": project_id, "filename": filename, "sha256": plan["sha256"], "sheet": plan["sheet"],
            "rows_read": str(plan["rows_read"]), "cases_created": str(counts.get("cases_created", 0)), "steps_created": str(counts.get("steps_created", 0)),
            "requirements_created": str(counts.get("requirements_created", 0)), "modules_created": str(counts.get("modules_created", 0)),
            "duplicates_allowed": plan["options"]["allow_duplicates"], "status": status, "error_count": str(len(plan["errors"])),
            "target_folder_id": plan["options"]["target_folder_id"], "stored_file": stored,
            "imported_by": actor_id if self.store.get("users", actor_id) else "", "imported_at": utcnow(),
        }, actor_id)

    def history(self, project_id: str) -> list[dict[str, Any]]:
        names = {u["id"]: u.get("name", "") for u in self.store.list("users", page_size=1_000_000, include_archived=True)["items"]}
        rows = self.store.list("import_history", filters={"project_id": project_id}, page_size=1_000_000)["items"]
        ordered = [r for _, r in sorted(enumerate(rows), key=lambda pair: (str(pair[1].get("imported_at", "")), pair[0]), reverse=True)]  # newest first, ties by write order
        return [{**r, "imported_by_name": names.get(r.get("imported_by", ""), "") or "System", "has_file": bool(r.get("stored_file"))} for r in ordered]

    def stored_file(self, history_id: str) -> tuple[bytes, str]:
        record = self.store.get("import_history", history_id)
        if not record or not record.get("stored_file"): raise KeyError("Import file not found")
        if not re.fullmatch(r"[0-9a-f-]{36}\.xlsx", str(record["stored_file"])): raise ValueError("Import file reference is invalid")
        data = _retry_io((self.store.root / "imports" / record["stored_file"]).read_bytes)
        if hashlib.sha256(data).hexdigest() != record.get("sha256"): raise ValueError("The stored import file has been altered")
        return data, record.get("filename") or "import.xlsx"
