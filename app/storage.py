"""Excel-only persistence for TMExpert.

This module is intentionally the sole owner of internal workbook I/O.  It uses
the OOXML file format directly so the product remains deployable without a
database driver or a spreadsheet server.  Workbooks are a single `Records`
sheet with a typed header row and stable record IDs.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
import uuid
import zipfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET
from xml.parsers import expat
from xml.sax.saxutils import escape

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
ET.register_namespace("", NS)
ET.register_namespace("r", REL)

WORKBOOKS: dict[str, tuple[str, tuple[str, ...]]] = {
    "projects": ("core/Projects.xlsx", ("id", "name", "key", "description", "application", "product_version", "owner_id", "member_ids", "status", "created_at", "updated_at", "archived")),
    "users": ("core/Users.xlsx", ("id", "name", "email", "role", "password_hash", "status", "created_at", "updated_at", "archived", "must_change_password")),
    "folders": ("repository/Repository_Folders.xlsx", ("id", "project_id", "parent_id", "name", "path", "created_at", "updated_at", "archived", "approval_status", "reviewed_by", "reviewed_at", "created_by", "updated_by")),
    "test_cases": ("repository/Test_Cases.xlsx", ("id", "project_id", "folder_id", "title", "objective", "description", "preconditions", "priority", "test_type", "module", "feature", "requirement_ids", "tags", "owner_id", "reviewer_id", "status", "version", "automation_status", "automation_reference", "framework", "script_url", "created_at", "updated_at", "archived", "expected_result", "last_status", "case_key", "created_by", "updated_by")),
    "test_steps": ("repository/Test_Steps.xlsx", ("id", "test_case_id", "version", "step_number", "action", "expected_result", "test_data", "created_at", "updated_at", "archived")),
    "test_case_versions": ("repository/Test_Case_Versions.xlsx", ("id", "test_case_id", "version", "snapshot", "comment", "changed_by", "created_at")),
    "requirements": ("requirements/Requirements.xlsx", ("id", "project_id", "parent_id", "key", "title", "description", "type", "priority", "version", "owner_id", "status", "source_reference", "created_at", "updated_at", "archived")),
    "test_plans": ("execution/Test_Plans.xlsx", ("id", "project_id", "name", "description", "release", "build", "start_date", "end_date", "owner_id", "scope", "status", "created_at", "updated_at", "archived")),
    "test_suites": ("execution/Test_Suites.xlsx", ("id", "project_id", "plan_id", "name", "description", "owner_id", "status", "created_at", "updated_at", "archived")),
    "test_cycles": ("execution/Test_Cycles.xlsx", ("id", "project_id", "suite_id", "name", "environment", "build", "start_date", "end_date", "owner_id", "status", "created_at", "updated_at", "archived", "testing_type", "description", "module_ids", "created_by", "updated_by")),
    "test_runs": ("execution/Test_Runs.xlsx", ("id", "project_id", "cycle_id", "test_case_id", "test_case_version", "tester_id", "environment", "build", "status", "started_at", "completed_at", "actual_result", "created_at", "updated_at", "archived", "case_key", "case_title", "module_id", "module_path", "priority", "test_type", "created_by", "updated_by", "assignee_id", "assignment_source", "assigned_at", "assigned_by", "execution_notes", "defect_ids", "executed_by", "executed_at", "attempt_count")),
    "execution_history": ("execution/Execution_History.xlsx", ("id", "project_id", "cycle_id", "test_run_id", "case_key", "attempt", "status", "previous_status", "actual_result", "notes", "defect_ids", "attachment_ids", "step_results", "source", "executed_by", "executed_at")),
    "attachments": ("execution/Attachments.xlsx", ("id", "project_id", "owner_type", "owner_id", "filename", "content_type", "size", "sha256", "stored_name", "uploaded_by", "created_at", "archived")),
    "cycle_assignments": ("execution/Cycle_Assignments.xlsx", ("id", "project_id", "cycle_id", "module_path", "module_id", "assignee_id", "created_at", "updated_at", "archived", "created_by", "updated_by")),
    "import_history": ("history/Import_History.xlsx", ("id", "project_id", "filename", "sha256", "sheet", "rows_read", "cases_created", "steps_created", "requirements_created", "modules_created", "duplicates_allowed", "status", "error_count", "target_folder_id", "stored_file", "imported_by", "imported_at")),
    "assignment_history": ("history/Assignment_History.xlsx", ("id", "project_id", "cycle_id", "test_run_id", "case_key", "module_path", "from_assignee_id", "to_assignee_id", "source", "reason", "actor_id", "created_at")),
    "execution_results": ("execution/Execution_Results.xlsx", ("id", "test_run_id", "step_id", "step_number", "status", "actual_result", "evidence", "comment", "created_at", "updated_at")),
    "defects": ("execution/Defects.xlsx", ("id", "project_id", "key", "summary", "description", "severity", "priority", "status", "assignee_id", "reporter_id", "environment", "build", "evidence", "test_run_id", "test_case_id", "created_at", "updated_at", "archived")),
    "approvals": ("history/Approvals.xlsx", ("id", "test_case_id", "version", "reviewer_id", "decision", "comment", "created_at", "folder_id")),
    "audit": ("history/Audit_History.xlsx", ("id", "actor_id", "action", "entity", "record_id", "project_id", "previous_value", "new_value", "result", "created_at", "hash")),
}

REQUIRED = {
    "projects": ("name", "key", "owner_id"), "users": ("name", "email", "role"),
    "folders": ("project_id", "name"), "test_cases": ("project_id", "folder_id", "title", "priority", "status"),
    "test_steps": ("test_case_id", "step_number", "action", "expected_result"),
    "requirements": ("project_id", "key", "title", "status"), "test_plans": ("project_id", "name"),
    "test_suites": ("project_id", "plan_id", "name"), "test_cycles": ("project_id", "name"),
    "test_runs": ("project_id", "cycle_id", "test_case_id", "status"), "execution_results": ("test_run_id", "step_id", "step_number", "status"),
    "defects": ("project_id", "key", "summary", "status"),
}
REFERENCES = {
    "projects": {"owner_id": "users"}, "folders": {"project_id": "projects", "parent_id": "folders"},
    "test_cases": {"project_id": "projects", "folder_id": "folders", "owner_id": "users", "reviewer_id": "users"},
    "test_steps": {"test_case_id": "test_cases"}, "test_case_versions": {"test_case_id": "test_cases", "changed_by": "users"},
    "requirements": {"project_id": "projects", "parent_id": "requirements", "owner_id": "users"},
    "test_plans": {"project_id": "projects", "owner_id": "users"}, "test_suites": {"project_id": "projects", "plan_id": "test_plans", "owner_id": "users"},
    "test_cycles": {"project_id": "projects", "suite_id": "test_suites", "owner_id": "users"},
    "test_runs": {"project_id": "projects", "cycle_id": "test_cycles", "test_case_id": "test_cases", "tester_id": "users", "assignee_id": "users"},
    "cycle_assignments": {"project_id": "projects", "cycle_id": "test_cycles", "assignee_id": "users"},
    "assignment_history": {"cycle_id": "test_cycles", "test_run_id": "test_runs"},
    "execution_history": {"cycle_id": "test_cycles", "test_run_id": "test_runs", "executed_by": "users"},
    "execution_results": {"test_run_id": "test_runs", "step_id": "test_steps"},
    "defects": {"project_id": "projects", "assignee_id": "users", "reporter_id": "users", "test_run_id": "test_runs", "test_case_id": "test_cases"},
    "approvals": {"test_case_id": "test_cases", "reviewer_id": "users", "folder_id": "folders"},
}
VALID_STATUSES = {
    "test_cases": {"Draft", "In Review", "Approved", "Rejected", "Deprecated"},
    "test_runs": {"Not Run", "In Progress", "Passed", "Failed", "Blocked", "Not Applicable"},
    "execution_results": {"Not Run", "In Progress", "Passed", "Failed", "Blocked", "Not Applicable"},
}
VALID_LAST_STATUSES = {"Not Run", "In Progress", "Passed", "Failed", "Blocked", "Not Applicable"}
VALID_FOLDER_APPROVAL = {"Draft", "In Review", "Approved", "Rejected"}

# The first published Test_Cases layout, before expected_result/last_status.
LEGACY_TEST_CASE_HEADERS = WORKBOOKS["test_cases"][1][:24]
LEGACY_LEVEL2_TEST_CASE_HEADERS = WORKBOOKS["test_cases"][1][:26]
LEGACY_CHANGE_ME_HASH = hashlib.sha256(b"change-me").hexdigest()

# Columns appended after a workbook was first released.  Existing workbooks
# that lack only these columns are upgraded in place (after a backup) with the
# default shown; every other column and value is preserved.
ADDITIVE_COLUMNS: dict[str, dict[str, Any]] = {
    "test_cases": {"case_key": "", "created_by": lambda record: record.get("owner_id") or "", "updated_by": ""},
    "folders": {"approval_status": "Approved", "reviewed_by": "", "reviewed_at": "", "created_by": "", "updated_by": ""},
    "approvals": {"folder_id": ""},
    "audit": {"hash": ""},
    "test_cycles": {"testing_type": "", "description": "", "module_ids": "", "created_by": "", "updated_by": ""},
    "test_runs": {"case_key": "", "case_title": "", "module_id": "", "module_path": "", "priority": "", "test_type": "", "created_by": "", "updated_by": "", "assignee_id": "", "assignment_source": "", "assigned_at": "", "assigned_by": "", "execution_notes": "", "defect_ids": "", "executed_by": "", "executed_at": "", "attempt_count": "0"},
    "users": {"must_change_password": lambda record: record.get("password_hash") == LEGACY_CHANGE_ME_HASH},
}

# Field combinations that must be unique among non-archived records.
UNIQUE_TOGETHER: dict[str, tuple[tuple[str, ...], str]] = {
    "test_runs": (("cycle_id", "test_case_id"), "This test case is already in the cycle"),
    "cycle_assignments": (("cycle_id", "module_path"), "This module already has a default assignee in the cycle"),
}

# History workbooks may only grow.  The generic update/delete paths refuse them.
APPEND_ONLY = {"audit", "approvals", "test_case_versions", "assignment_history", "execution_history", "import_history", "test_steps"}
# Workbooks that only grow are split into parts: when the current workbook reaches
# SEGMENT_ROWS rows it is moved, unchanged, to "<name>_archive/<name>_0001.xlsx" and a
# new, small current workbook continues.  All parts are read together as one list.
SEGMENT_ROWS = 10_000
# Records the search box looks in.
SEARCHABLE = {"projects", "folders", "test_cases", "requirements", "test_plans", "test_cycles", "defects"}

# An ID written at the start of a title, e.g. "SUR-TC-022 – Add question to survey".
_TITLE_ID = re.compile(r"^\s*([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d+)(?=\s|[–—:|]|$)")

def title_case_key(title: Any) -> str:
    match = _TITLE_ID.match(str(title or ""))
    return match.group(1) if match else ""

_EDGE_SPACE = frozenset(chr(c) for c in (32, 9, 13, 10))  # leading/trailing whitespace must be marked to survive Excel
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
# Characters XML 1.0 does not allow; removed when writing.
_XML_STRIP = {code: None for code in [*range(0, 9), 11, 12, *range(14, 32), 0xFFFE, 0xFFFF]}
_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()

def as_text(value: Any) -> str:
    if value is None: return ""
    if isinstance(value, (dict, list)): return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)

def from_text(value: str) -> Any:
    value = value or ""
    if value.startswith(("{", "[")):
        try: return json.loads(value)
        except json.JSONDecodeError: pass
    if value.lower() in {"true", "false"}: return value.lower() == "true"
    return value

def _retry_io(operation, attempts: int = 40, delay: float = 0.025):
    """Run a file operation, retrying briefly on Windows sharing violations.

    On Windows a workbook cannot be replaced while another thread, Excel or an
    antivirus scanner has it open for a moment; that surfaces as PermissionError.
    """
    for attempt in range(attempts):
        try:
            return operation()
        except PermissionError:
            if attempt == attempts - 1: raise
            time.sleep(delay * (1 + attempt // 5))

def normalize_value(value: Any) -> Any:
    """Return the value exactly as it reads back after a workbook write."""
    if isinstance(value, str):
        return from_text(value) if value[:1] in "{[" or value.lower() in {"true", "false"} else value
    if isinstance(value, bool) or isinstance(value, (dict, list)):
        return value
    return from_text(as_text(value))

class XlsxCodec:
    """Small OOXML reader/writer for internal, string-based record sheets."""
    @staticmethod
    def write(path: Path, headers: Iterable[str], records: Iterable[dict[str, Any]]) -> None:
        headers = tuple(headers)
        letters: list[str] = []
        for col in range(1, len(headers) + 1):
            name, n = "", col
            while n: n, remainder = divmod(n - 1, 26); name = chr(65 + remainder) + name
            letters.append(name)
        def cell(letter: str, row: int, value: Any) -> str:
            if value is None or value == "": return ""  # empty cells are optional in OOXML
            text = as_text(value).translate(_XML_STRIP)
            if not text: return ""
            space = ' xml:space="preserve"' if text[0] in _EDGE_SPACE or text[-1] in _EDGE_SPACE else ""
            text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return f'<c r="{letter}{row}" t="inlineStr"><is><t{space}>{text}</t></is></c>'
        pairs = list(zip(letters, headers))
        rows = ["<row r=\"1\">" + "".join(cell(letter, 1, h) for letter, h in pairs) + "</row>"]
        for row_num, record in enumerate(records, 2):
            get = record.get
            rows.append(f'<row r="{row_num}">' + "".join(cell(letter, row_num, get(h, "")) for letter, h in pairs) + "</row>")
        sheet = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>' % (NS, "".join(rows))
        files = {
            "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
            "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
            "xl/workbook.xml": f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{NS}" xmlns:r="{REL}"><sheets><sheet name="Records" sheetId="1" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/worksheets/sheet1.xml": sheet,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for name, content in files.items(): archive.writestr(name, content)

    @staticmethod
    def read(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
        if not path.exists(): return [], []
        return XlsxCodec.read_bytes(_retry_io(path.read_bytes))

    @staticmethod
    def verify(path: Path, expected_rows: int) -> None:
        """Cheap integrity check of a workbook this codec just wrote.

        Confirms the zip CRCs, that the sheet XML is well formed, and that the
        row count matches, without materialising every cell.
        """
        try:
            with zipfile.ZipFile(path) as archive:
                if archive.testzip() is not None: raise ValueError("corrupt archive member")
                sheet = archive.read("xl/worksheets/sheet1.xml")
            expat.ParserCreate().Parse(sheet, True)
        except (zipfile.BadZipFile, KeyError, expat.ExpatError, OSError) as exc:
            raise ValueError("Workbook validation failed before replacement") from exc
        if sheet.count(b"<row ") != expected_rows + 1:
            raise ValueError("Workbook validation failed before replacement")

    @staticmethod
    def read_bytes(data: bytes, sheet: str | None = None) -> tuple[list[str], list[dict[str, Any]]]:
        try:
            return XlsxCodec._read_bytes(data, sheet)
        except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as exc:
            raise ValueError("The uploaded file is not a readable .xlsx workbook") from exc

    @staticmethod
    def sheet_names(data: bytes) -> list[str]:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                root = ET.fromstring(archive.read("xl/workbook.xml"))
            return [item.get("name", "") for item in root.findall(f".//{{{NS}}}sheet")]
        except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as exc:
            raise ValueError("The uploaded file is not a readable .xlsx workbook") from exc

    @staticmethod
    def _open_sheet(data: bytes, sheet: str | None = None) -> tuple[bytes | None, list[str]]:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            if sum(item.file_size for item in archive.infolist()) > 50 * 1024 * 1024:
                raise ValueError("The workbook expands beyond the 50 MB import limit")
            workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
            sheets = workbook_root.findall(f".//{{{NS}}}sheet")
            first_sheet = next((item for item in sheets if item.get("name") == sheet), None) if sheet else (sheets[0] if sheets else None)
            if sheet and first_sheet is None: raise ValueError(f"The workbook has no sheet named “{sheet}”")
            if first_sheet is None: return None, []
            relationship_id = first_sheet.get(f"{{{REL}}}id")
            package_rel = "http://schemas.openxmlformats.org/package/2006/relationships"
            relationships_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            relationship = next((item for item in relationships_root.findall(f"{{{package_rel}}}Relationship") if item.get("Id") == relationship_id), None)
            if relationship is None: raise ValueError("The workbook worksheet relationship is invalid")
            target = relationship.get("Target", "")
            worksheet_path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
            if worksheet_path not in names: raise ValueError("The workbook worksheet could not be found")
            xml = archive.read(worksheet_path)
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = ["".join(item.itertext()) for item in shared_root.findall(f"{{{NS}}}si")]
        return xml, shared

    _COLUMN_CACHE: dict[str, int] = {}

    @staticmethod
    def _row_values(row: Any, shared: list[str]) -> dict[int, str]:
        tag_c, tag_v = f"{{{NS}}}c", f"{{{NS}}}v"
        cache = XlsxCodec._COLUMN_CACHE
        values: dict[int, str] = {}
        position = -1
        for cell in row:
            if cell.tag != tag_c: continue
            # The cell reference ("B7") is optional in OOXML; without it a cell follows the previous one.
            ref = cell.get("r")
            if ref:
                letters = ref.rstrip("0123456789").upper()
                column = cache.get(letters)
                if column is None:
                    column = 0
                    for letter in letters: column = column * 26 + ord(letter) - 64
                    column -= 1
                    cache[letters] = column
            else:
                column = position + 1
            position = column
            kind = cell.get("t")
            if kind == "inlineStr":
                value = "".join(cell.itertext())
            else:
                value = ""
                for child in cell:
                    if child.tag == tag_v: value = child.text or ""; break
                if kind == "s" and value:
                    try: value = shared[int(value)]
                    except (IndexError, ValueError) as exc: raise ValueError("The workbook contains an invalid shared string reference") from exc
            values[column] = value
        return values

    @staticmethod
    def _read_bytes(data: bytes, sheet: str | None = None) -> tuple[list[str], list[dict[str, Any]]]:
        xml, shared = XlsxCodec._open_sheet(data, sheet)
        if xml is None: return [], []
        root = ET.fromstring(xml)
        rows = [XlsxCodec._row_values(row, shared) for row in root.iter(f"{{{NS}}}row")]
        if not rows: return [], []
        headers = [rows[0].get(i, "") for i in range(max(rows[0], default=-1) + 1)]
        if not any(headers): return [], []
        records = []
        for values in rows[1:]:
            if any(values.values()): records.append({header: from_text(values.get(i, "")) for i, header in enumerate(headers) if header})
        return headers, records

    @staticmethod
    def read_headers(path: Path) -> list[str]:
        """Only the header row (much faster than a full read for large workbooks)."""
        if not path.exists(): return []
        try:
            xml, shared = XlsxCodec._open_sheet(_retry_io(path.read_bytes))
            if xml is None: return []
            for _, element in ET.iterparse(io.BytesIO(xml), events=("end",)):
                if element.tag == f"{{{NS}}}row":
                    values = XlsxCodec._row_values(element, shared)
                    return [values.get(i, "") for i in range(max(values, default=-1) + 1)]
        except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as exc:
            raise ValueError(f"{path.name} is not a readable .xlsx workbook") from exc
        return []

class WorkbookLock:
    def __init__(self, lock_path: Path, mutex: threading.RLock): self.lock_path, self.mutex, self.fd = lock_path, mutex, None
    def __enter__(self):
        self.mutex.acquire(); self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 15
        while True:
            try:
                self.fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY); os.write(self.fd, str(os.getpid()).encode()); return self
            except FileExistsError:
                if time.monotonic() > deadline:
                    if time.time() - self.lock_path.stat().st_mtime > 60: self.lock_path.unlink(missing_ok=True); continue
                    self.mutex.release(); raise TimeoutError(f"Workbook is busy: {self.lock_path.stem}")
                time.sleep(.05)
    def __exit__(self, *_):
        if self.fd is not None: os.close(self.fd)
        self.lock_path.unlink(missing_ok=True); self.mutex.release()

class ExcelStorageService:
    """Transactional record store backed solely by named Excel workbooks."""
    def __init__(self, data_dir: str | Path, backup_retention: int = 30, backup_interval_seconds: float = 300):
        self.root = Path(data_dir); self.backup_retention = backup_retention
        self.backup_interval = backup_interval_seconds
        # entity -> (mtime_ns, headers, rows, rows_by_id)
        self.cache: dict[str, tuple[int, tuple[str, ...], list[dict[str, Any]], dict[str, dict[str, Any]]]] = {}
        self.index: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self.index_words: dict[tuple[str, str], set[str]] = {}
        self.last_backup: dict[str, float] = {}
        self.segment_rows = SEGMENT_ROWS
        self.segment_files: dict[str, list[Path]] = {}
        self.segment_cache: dict[str, tuple[int, list[dict[str, Any]]]] = {}
        self._batch = threading.local()
        self.locks: defaultdict[str, threading.RLock] = defaultdict(threading.RLock)
        for folder in ("core", "repository", "requirements", "execution", "history", "backups", "indexes", "temp", "attachments", "imports"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        for entity, (relative, headers) in WORKBOOKS.items():
            target = self.root / relative
            if not target.exists(): XlsxCodec.write(target, headers, [])
        self._migrate_test_case_schema()
        for entity in WORKBOOKS: self._migrate_additive_columns(entity)
        self._backfill_case_keys()
        self._adopt_title_case_keys()
        self.rebuild_index()

    # ------------------------------------------------------------------ schema
    def _schema_error(self, path: Path, headers: Iterable[str], expected: Iterable[str]) -> ValueError:
        headers = list(headers); expected = list(expected)
        missing = [header for header in expected if header not in headers]
        extra = [header for header in headers if header not in expected]
        return ValueError(
            f"Unsupported schema in {path.name}; missing columns: "
            f"{', '.join(missing) or 'none'}; unexpected columns: "
            f"{', '.join(extra) or 'none'}. The workbook was not changed."
        )
    def _replace_workbook(self, entity: str, headers: tuple[str, ...], records: list[dict[str, Any]], label: str) -> None:
        self._backup(entity, force=True)
        temporary = self.root / "temp" / f"{entity}-{label}-{uuid.uuid4().hex}.xlsx"
        try:
            XlsxCodec.write(temporary, headers, records)
            XlsxCodec.verify(temporary, len(records))
            _retry_io(lambda: os.replace(temporary, self.path_for(entity)))
        finally:
            temporary.unlink(missing_ok=True)
        self.cache.pop(entity, None)
    def _migrate_test_case_schema(self) -> None:
        """Upgrade the original 24-column Test_Cases layout (adds expected_result/last_status)."""
        path = self.path_for("test_cases")
        if tuple(XlsxCodec.read_headers(path)) != LEGACY_TEST_CASE_HEADERS:
            return
        headers, records = XlsxCodec.read(path)
        latest_status: dict[str, tuple[str, str]] = {}
        _, runs = XlsxCodec.read(self.path_for("test_runs"))
        for run in runs:
            case_id = str(run.get("test_case_id", ""))
            status = str(run.get("status", ""))
            updated_at = str(run.get("updated_at", ""))
            if case_id and status in VALID_LAST_STATUSES and updated_at >= latest_status.get(case_id, ("", ""))[0]:
                latest_status[case_id] = (updated_at, status)
        migrated = [
            {**record, "expected_result": "", "last_status": latest_status.get(str(record.get("id", "")), ("", "Not Run"))[1]}
            for record in records
        ]
        self._replace_workbook("test_cases", LEGACY_LEVEL2_TEST_CASE_HEADERS, migrated, "migration")
    def _migrate_additive_columns(self, entity: str) -> None:
        """Append newly introduced columns to an existing workbook, preserving all data."""
        path = self.path_for(entity)
        expected = WORKBOOKS[entity][1]
        headers = XlsxCodec.read_headers(path)
        missing = [header for header in expected if header not in headers]
        if not missing or not headers:
            return
        headers, records = XlsxCodec.read(path)
        additions = ADDITIVE_COLUMNS.get(entity, {})
        if len(headers) != len(set(headers)) or not set(missing).issubset(additions):
            return  # reported with full detail on first read; workbook left untouched
        new_headers = expected + tuple(header for header in headers if header and header not in expected)
        upgraded = []
        for record in records:
            record = dict(record)
            for column in missing:
                default = additions[column]
                record[column] = default(record) if callable(default) else default
            upgraded.append(record)
        self._replace_workbook(entity, new_headers, upgraded, "add-columns")
    def _backfill_case_keys(self) -> None:
        """Give every existing test case a stable, readable ID (e.g. LMS-TC-0001)."""
        rows = self._load("test_cases")[2]
        if all(row.get("case_key") for row in rows):
            return
        with WorkbookLock(self.root / "temp" / "test_cases.lock", self.locks["test_cases"]):
            records = self._read_all("test_cases")
            pending = sorted((r for r in records if not r.get("case_key")), key=lambda r: (str(r.get("created_at", "")), str(r.get("id", ""))))
            keys = self._allocate_case_keys(records, pending, use_titles=False)
            for record, key in zip(pending, keys): record["case_key"] = key
            self._commit("test_cases", records, force_backup=True)
        self._audit_many([self._audit_entry("Migrate", "test_cases", "*", {}, {"case_key_backfill": len(pending)}, "system")])

    # ---------------------------------------------------------------- reading
    def path_for(self, entity: str) -> Path:
        if entity not in WORKBOOKS: raise KeyError(f"Unknown entity: {entity}")
        return self.root / WORKBOOKS[entity][0]
    def _segments(self, entity: str) -> list[Path]:
        if entity not in APPEND_ONLY: return []
        if entity not in self.segment_files:
            path = self.path_for(entity)
            folder = path.parent / f"{path.stem}_archive"
            self.segment_files[entity] = sorted(folder.glob(f"{path.stem}_*.xlsx")) if folder.exists() else []
        return self.segment_files[entity]
    def _read_segment(self, entity: str, segment: Path) -> list[dict[str, Any]]:
        stamp = segment.stat().st_mtime_ns
        cached = self.segment_cache.get(str(segment))
        if cached and cached[0] == stamp: return cached[1]
        headers, rows = XlsxCodec.read(segment)
        expected = WORKBOOKS[entity][1]
        missing = [h for h in expected if h not in headers]
        if missing and not set(missing).issubset(ADDITIVE_COLUMNS.get(entity, {})): raise self._schema_error(segment, headers, expected)
        for row in rows:  # parts written before a column existed get its default in memory
            for column in missing:
                default = ADDITIVE_COLUMNS[entity][column]
                row[column] = default(row) if callable(default) else default
        self.segment_cache[str(segment)] = (stamp, rows)
        return rows
    def _load(self, entity: str) -> tuple[Any, tuple[str, ...], list[dict[str, Any]], dict[str, dict[str, Any]], int]:
        path = self.path_for(entity); segments = self._segments(entity)
        stamp = (path.stat().st_mtime_ns, len(segments)) if segments else path.stat().st_mtime_ns
        cached = self.cache.get(entity)
        if cached and cached[0] == stamp: return cached
        headers, rows = XlsxCodec.read(path)
        expected = WORKBOOKS[entity][1]
        if tuple(headers) != expected and not (
            len(headers) == len(set(headers)) and set(expected).issubset(headers)
        ):
            raise self._schema_error(path, headers, expected)
        archived: list[dict[str, Any]] = []
        for segment in segments: archived.extend(self._read_segment(entity, segment))
        if archived:
            seen = {r.get("id") for r in archived}
            rows = archived + [r for r in rows if r.get("id") not in seen]  # self-heals an interrupted move
        entry = (stamp, tuple(headers), rows, {str(r.get("id", "")): r for r in rows}, len(archived))
        self.cache[entity] = entry
        return entry
    def _read_all(self, entity: str) -> list[dict[str, Any]]:
        return [r.copy() for r in self._load(entity)[2]]
    def list(self, entity: str, *, filters: dict[str, Any] | None = None, query: str = "", page: int = 1, page_size: int = 50, include_archived: bool = False) -> dict[str, Any]:
        rows = self._load(entity)[2]
        filters = filters or {}
        def matches(record: dict[str, Any]) -> bool:
            if not include_archived and record.get("archived") is True: return False
            if any(str(record.get(k, "")) != str(v) for k, v in filters.items() if v not in (None, "")): return False
            return not query or query.lower() in " ".join(map(str, record.values())).lower()
        selected = [r for r in rows if matches(r)]; total = len(selected); start = max(0, (page - 1) * page_size)
        return {"items": [r.copy() for r in selected[start:start + page_size]], "total": total, "page": page, "page_size": page_size}
    def get(self, entity: str, record_id: str, include_archived: bool = True) -> dict[str, Any] | None:
        record = self._load(entity)[3].get(str(record_id))
        if record is None or (not include_archived and record.get("archived")): return None
        return record.copy()

    # ------------------------------------------------------------- validation
    def _validate(self, entity: str, record: dict[str, Any], existing_id: str | None = None) -> None:
        missing = [field for field in REQUIRED.get(entity, ()) if record.get(field) in (None, "")]
        if missing: raise ValueError("Required fields: " + ", ".join(missing))
        for field, related_entity in REFERENCES.get(entity, {}).items():
            value = record.get(field)
            if value not in (None, "") and not self.get(related_entity, str(value)):
                raise ValueError(f"Invalid {field}: referenced {related_entity} record does not exist")
        if entity in {"folders", "test_cases", "requirements", "test_plans", "test_suites", "test_cycles", "test_runs"}:
            project_id = record.get("project_id", "")
            related_project_fields = {
                "folders": ("parent_id", "folders"),
                "test_cases": ("folder_id", "folders"),
                "requirements": ("parent_id", "requirements"),
                "test_suites": ("plan_id", "test_plans"),
                "test_cycles": ("suite_id", "test_suites"),
                "test_runs": ("cycle_id", "test_cycles"),
            }
            if entity in related_project_fields:
                field, related_entity = related_project_fields[entity]
                related_id = record.get(field)
                related = self.get(related_entity, str(related_id)) if related_id else None
                if related and related.get("project_id") != project_id:
                    raise ValueError(f"{field} must belong to the same project")
            if entity == "test_cases":
                for requirement_id in record.get("requirement_ids") or []:
                    requirement = self.get("requirements", str(requirement_id))
                    if not requirement or requirement.get("project_id") != project_id:
                        raise ValueError("Requirement links must belong to the same project")
                key = str(record.get("case_key") or "").casefold()
                if key and any(str(r.get("case_key") or "").casefold() == key and r.get("id") != record.get("id") and r.get("project_id") == project_id for r in self._load("test_cases")[2]):
                    raise ValueError(f"Test case ID {record.get('case_key')} is already in use")
            if entity == "test_runs":
                case = self.get("test_cases", str(record.get("test_case_id", "")))
                if case and case.get("project_id") != project_id:
                    raise ValueError("The test case must belong to the same project")
            if entity == "folders":
                parent_id = str(record.get("parent_id", ""))
                visited = {existing_id} if existing_id else set()
                while parent_id:
                    if parent_id in visited:
                        raise ValueError("Repository folders cannot contain a parent cycle")
                    visited.add(parent_id)
                    parent = self.get("folders", parent_id)
                    if not parent:
                        break
                    parent_id = str(parent.get("parent_id", ""))
        if entity == "execution_results":
            run = self.get("test_runs", str(record.get("test_run_id", "")))
            step = self.get("test_steps", str(record.get("step_id", "")))
            if run and step and run.get("test_case_id") != step.get("test_case_id"):
                raise ValueError("Execution step must belong to the test run's test case")
        status = record.get("status")
        if entity in VALID_STATUSES and status and status not in VALID_STATUSES[entity]: raise ValueError(f"Invalid {entity} status: {status}")
        if entity == "folders" and record.get("approval_status") and record["approval_status"] not in VALID_FOLDER_APPROVAL:
            raise ValueError(f"Invalid module approval status: {record['approval_status']}")
        if entity == "test_cases" and record.get("last_status") and record["last_status"] not in VALID_LAST_STATUSES:
            raise ValueError(f"Invalid test case last status: {record['last_status']}")
        if entity == "projects" and any(r["key"].lower() == str(record["key"]).lower() and r["id"] != existing_id for r in self._load(entity)[2]): raise ValueError("Project key must be unique")
        if entity == "users" and any(r["email"].lower() == str(record["email"]).lower() and r["id"] != existing_id for r in self._load(entity)[2]): raise ValueError("Email must be unique")

    def _allocate_case_keys(self, existing: list[dict[str, Any]], new_records: list[dict[str, Any]], use_titles: bool = True) -> list[str]:
        """Choose a test case ID for each new record.  Call while holding the test_cases lock.

        An ID at the start of the title (e.g. "SUR-TC-022 – Add question") is used
        when it is free in the project; otherwise <PROJECT KEY>-TC-#### is allocated.
        """
        projects = self._load("projects")[3]
        used: dict[str, set[str]] = {}
        highest: dict[tuple[str, str], int] = {}
        keys: list[str] = []
        for record in new_records:
            project_id = str(record.get("project_id", ""))
            if project_id not in used:
                used[project_id] = {str(r.get("case_key") or "").casefold() for r in existing if str(r.get("project_id", "")) == project_id and r.get("case_key")}
            key = title_case_key(record.get("title")) if use_titles else ""
            if not key or key.casefold() in used[project_id]:
                prefix = str((projects.get(project_id) or {}).get("key") or "TC").upper()
                if (project_id, prefix) not in highest:
                    pattern = re.compile(rf"^{re.escape(prefix)}-TC-(\d+)$", re.IGNORECASE)
                    highest[(project_id, prefix)] = max((int(m.group(1)) for r in existing if str(r.get("project_id", "")) == project_id and (m := pattern.match(str(r.get("case_key") or "")))), default=0)
                while True:
                    highest[(project_id, prefix)] += 1
                    key = f"{prefix}-TC-{highest[(project_id, prefix)]:04d}"
                    if key.casefold() not in used[project_id]: break
            used[project_id].add(key.casefold())
            keys.append(key)
        return keys

    def _adopt_title_case_keys(self) -> None:
        """One-time switch to the IDs teams already write at the start of titles.

        Active cases are served first; an archived copy whose title ID is already
        taken keeps its generated ID.  Runs once (recorded in the audit history).
        """
        marker = self.root / "indexes" / "migrations.json"
        done = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {}
        if done.get("adopt_title_ids"): return
        if any(r.get("action") == "Migrate" and isinstance(r.get("new_value"), dict) and "adopt_title_ids" in r["new_value"] for r in self._load("audit")[2]):
            marker.write_text(json.dumps({**done, "adopt_title_ids": True}), encoding="utf-8"); return
        changed: list[tuple[dict[str, Any], dict[str, Any]]] = []
        with WorkbookLock(self.root / "temp" / "test_cases.lock", self.locks["test_cases"]):
            records = self._read_all("test_cases")
            by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for record in records: by_project[str(record.get("project_id", ""))].append(record)
            new_keys: dict[str, str] = {}
            for rows in by_project.values():
                taken: set[str] = set()
                for record in sorted(rows, key=lambda r: (r.get("archived") is True, str(r.get("created_at", "")), str(r.get("id", "")))):
                    key = title_case_key(record.get("title"))
                    if key and key.casefold() not in taken:
                        taken.add(key.casefold()); new_keys[record["id"]] = key
                keep = [r for r in rows if r["id"] not in new_keys]
                kept_ok = [r for r in keep if r.get("case_key") and str(r["case_key"]).casefold() not in taken]
                for record in kept_ok: taken.add(str(record["case_key"]).casefold())
                clashing = [r for r in keep if r not in kept_ok]
                reference = [{**r, "case_key": new_keys.get(r["id"], r.get("case_key", ""))} for r in rows if r["id"] in new_keys or r in kept_ok]
                for record, key in zip(clashing, self._allocate_case_keys(reference, clashing, use_titles=False)):
                    new_keys[record["id"]] = key
            for index, record in enumerate(records):
                key = new_keys.get(record["id"], record.get("case_key", ""))
                if key != record.get("case_key"):
                    changed.append((record, {**record, "case_key": key})); records[index] = {**record, "case_key": key}
            if changed: self._commit("test_cases", records, force_backup=True)
        entries = [self._audit_entry("Re-key", "test_cases", after["id"], {"case_key": before.get("case_key", "")}, {"case_key": after["case_key"], "project_id": after.get("project_id", "")}, "system") for before, after in changed]
        entries.append(self._audit_entry("Migrate", "test_cases", "*", {}, {"adopt_title_ids": len(changed)}, "system"))
        self._audit_many(entries)
        marker.write_text(json.dumps({**done, "adopt_title_ids": True}), encoding="utf-8")

    # ---------------------------------------------------------------- writing
    def _backup(self, entity: str, force: bool = False) -> None:
        source = self.path_for(entity); backup_dir = self.root / "backups" / entity; backup_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        if not force:
            last = self.last_backup.get(entity)
            if last is None:
                newest = max((item.stat().st_mtime for item in backup_dir.glob("*.xlsx")), default=0.0)
                last = self.last_backup[entity] = newest
            if now - last < self.backup_interval:
                return
        if source.exists(): shutil.copy2(source, backup_dir / f"{datetime.now():%Y%m%dT%H%M%S%f}.xlsx")
        self.last_backup[entity] = now
        old = sorted(backup_dir.glob("*.xlsx"), reverse=True)[self.backup_retention:]
        for item in old: item.unlink()
    def _commit(self, entity: str, records: list[dict[str, Any]], force_backup: bool = False, normalized_prefix: int = 0, changed_ids: set[str] | None = None) -> None:
        """Write a workbook atomically. ``records[:normalized_prefix]`` are unchanged cached rows."""
        expected = WORKBOOKS[entity][1]
        current = self._load(entity)
        headers = expected + tuple(header for header in current[1] if header and header not in expected)
        if len({r.get("id") for r in records}) != len(records): raise ValueError(f"Duplicate IDs detected in {entity}")
        archived = current[4] if entity in APPEND_ONLY else 0
        if archived and (len(records) < archived or records[archived - 1].get("id") != current[2][archived - 1].get("id")):
            raise ValueError(f"{entity.replace('_', ' ')} records are append-only")
        active = records[archived:]
        segment = None
        if entity in APPEND_ONLY and len(active) >= self.segment_rows:
            # Move the full current part to the archive (unchanged from now on) and start a new one.
            path = self.path_for(entity)
            folder = path.parent / f"{path.stem}_archive"; folder.mkdir(parents=True, exist_ok=True)
            segment = folder / f"{path.stem}_{len(self._segments(entity)) + 1:04d}.xlsx"
            part = self.root / "temp" / f"{entity}-part-{uuid.uuid4().hex}.xlsx"
            try:
                XlsxCodec.write(part, headers, active); XlsxCodec.verify(part, len(active))
                _retry_io(lambda: os.replace(part, segment))
            finally:
                part.unlink(missing_ok=True)
            active = []
        temp = self.root / "temp" / f"{entity}-{uuid.uuid4().hex}.xlsx"
        try:
            XlsxCodec.write(temp, headers, active)
            XlsxCodec.verify(temp, len(active))
            self._backup(entity, force=force_backup); _retry_io(lambda: os.replace(temp, self.path_for(entity)))
        finally:
            temp.unlink(missing_ok=True)
        # Keep the parsed copy so the next read does not re-parse the workbook.
        reuse = max(normalized_prefix, archived)
        cached_by_id = current[3] if changed_ids is not None and tuple(current[1]) == tuple(headers) else {}
        def prepared(record: dict[str, Any]) -> dict[str, Any]:
            record_id = str(record.get("id", ""))
            if record_id in cached_by_id and record_id not in changed_ids: return cached_by_id[record_id]
            return {header: normalize_value(record.get(header, "")) for header in headers if header}
        rows = current[2][:reuse] + [prepared(record) for record in records[reuse:]]
        if segment is not None:
            self.segment_files[entity] = [*self._segments(entity), segment]
            self.segment_cache[str(segment)] = (segment.stat().st_mtime_ns, rows[archived:])
            archived = len(rows)
        segments = self._segments(entity)
        stamp = (self.path_for(entity).stat().st_mtime_ns, len(segments)) if segments else self.path_for(entity).stat().st_mtime_ns
        self.cache[entity] = (stamp, headers, rows, {str(r.get("id", "")): r for r in rows}, archived)
    def backup_now(self, entities: Iterable[str]) -> None:
        """Take a backup of each workbook now (e.g. right before an import)."""
        for entity in entities: self._backup(entity, force=True)
    def _audit_entry(self, action: str, entity: str, record_id: str, before: Any, after: Any, actor_id: str = "system") -> dict[str, Any]:
        if entity == "users":  # never copy credentials into the audit history
            before = {k: ("[redacted]" if k == "password_hash" else v) for k, v in (before or {}).items()}
            after = {k: ("[redacted]" if k == "password_hash" else v) for k, v in (after or {}).items()}
        project_id = (after or before or {}).get("project_id", "") if isinstance(after or before or {}, dict) else ""
        return {"id": str(uuid.uuid4()), "actor_id": actor_id, "action": action, "entity": entity, "record_id": record_id, "project_id": project_id, "previous_value": before or {}, "new_value": after or {}, "result": "Success", "created_at": utcnow()}
    @staticmethod
    def audit_hash(previous: str, entry: dict[str, Any]) -> str:
        """Checksum of an audit entry chained to the previous one (tamper evidence)."""
        content = json.dumps({k: as_text(v) for k, v in sorted(entry.items()) if k != "hash"}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256((previous + "|" + content).encode("utf-8")).hexdigest()
    @contextmanager
    def audit_batch(self):
        """Collect the audit entries of one request and write them once at the end."""
        if getattr(self._batch, "entries", None) is not None:
            yield; return
        self._batch.entries = []
        try:
            yield
        finally:
            entries, self._batch.entries = self._batch.entries, None
            self._write_audit(entries)
    def _audit_many(self, entries: list[dict[str, Any]]) -> None:
        if not entries: return
        pending = getattr(self._batch, "entries", None)
        if pending is not None: pending.extend(entries); return
        self._write_audit(entries)
    def _write_audit(self, entries: list[dict[str, Any]]) -> None:
        if not entries: return
        with WorkbookLock(self.root / "temp" / "audit.lock", self.locks["audit"]):
            current = self._load("audit")
            previous = next((str(r.get("hash") or "") for r in reversed(current[2]) if r.get("hash")), "")
            for entry in entries:
                entry["hash"] = self.audit_hash(previous, entry); previous = entry["hash"]
            self._commit("audit", current[2] + entries, normalized_prefix=len(current[2]))
    def verify_audit(self) -> dict[str, Any]:
        """Re-compute the audit checksum chain; report the first entry that does not match."""
        rows = self._load("audit")[2]
        previous, legacy, checked = "", 0, 0
        for index, row in enumerate(rows):
            stored = str(row.get("hash") or "")
            if not stored and not previous: legacy += 1; continue
            expected = self.audit_hash(previous, {k: v for k, v in row.items() if k in WORKBOOKS["audit"][1]})
            if stored != expected:
                return {"ok": False, "checked": checked, "legacy": legacy, "total": len(rows), "broken_at": {"position": index + 1, "id": row.get("id", ""), "created_at": row.get("created_at", ""), "action": row.get("action", ""), "entity": row.get("entity", "")}}
            previous = stored; checked += 1
        return {"ok": True, "checked": checked, "legacy": legacy, "total": len(rows), "broken_at": None}
    def _audit(self, action: str, entity: str, record_id: str, before: Any, after: Any, actor_id: str = "system") -> None:
        if entity == "audit": return
        self._audit_many([self._audit_entry(action, entity, record_id, before, after, actor_id)])
    def record_event(self, action: str, entity: str, record_id: str, details: dict[str, Any], actor_id: str) -> None:
        """Audit an action that changes no workbook row (e.g. sign-in)."""
        self._audit_many([self._audit_entry(action, entity, record_id, {}, details, actor_id)])
    def _check_unique(self, entity: str, existing: list[dict[str, Any]], new_records: list[dict[str, Any]]) -> None:
        rule = UNIQUE_TOGETHER.get(entity)
        if not rule: return
        fields, message = rule
        new_ids = {r.get("id") for r in new_records}
        seen = {tuple(str(r.get(f, "")) for f in fields) for r in existing if r.get("archived") is not True and r.get("id") not in new_ids}
        for record in new_records:
            if record.get("archived") is True: continue
            key = tuple(str(record.get(f, "")) for f in fields)
            if key in seen: raise ValueError(message)
            seen.add(key)
    def _stamp_author(self, entity: str, record: dict[str, Any], actor_id: str, creating: bool = False) -> None:
        columns = WORKBOOKS[entity][1]
        if creating and "created_by" in columns and not record.get("created_by"): record["created_by"] = actor_id
        if "updated_by" in columns: record["updated_by"] = actor_id
    def _guard_append_only(self, entity: str) -> None:
        if entity in APPEND_ONLY: raise ValueError(f"{entity.replace('_', ' ').title()} records are append-only")
    def create(self, entity: str, data: dict[str, Any], actor_id: str = "system") -> dict[str, Any]:
        if entity == "audit": raise ValueError("Audit records are written by the system only")
        now = utcnow(); record = {field: data.get(field, "") for field in WORKBOOKS[entity][1]}
        record.update(data); record["id"] = str(data.get("id") or uuid.uuid4())
        if not _SAFE_ID.match(record["id"]): raise ValueError("Record IDs may only use letters, digits, '.', '_', ':' and '-'")
        if "created_at" in record and not record.get("created_at"):
            record["created_at"] = now
        if "updated_at" in record:
            record["updated_at"] = now
        if "archived" in record and record.get("archived") in (None, ""):
            record["archived"] = False
        self._stamp_author(entity, record, actor_id, creating=True)
        with WorkbookLock(self.root / "temp" / f"{entity}.lock", self.locks[entity]):
            records = self._read_all(entity)
            if entity == "test_cases" and not record.get("case_key"):
                record["case_key"] = self._allocate_case_keys(records, [record])[0]
            self._validate(entity, record)
            self._check_unique(entity, records, [record])
            if any(r["id"] == record["id"] for r in records): raise ValueError("Duplicate ID detected")
            records.append(record); self._commit(entity, records, changed_ids={record["id"]})
        self._reindex(entity, [record]); self._audit("Create", entity, record["id"], None, record, actor_id); return record
    def update(self, entity: str, record_id: str, changes: dict[str, Any], actor_id: str = "system", action: str = "Update") -> dict[str, Any]:
        self._guard_append_only(entity)
        immutable = {"id", "created_at", "case_key"} if entity == "test_cases" else {"id", "created_at"}
        with WorkbookLock(self.root / "temp" / f"{entity}.lock", self.locks[entity]):
            records = self._read_all(entity); index = next((i for i, r in enumerate(records) if r["id"] == record_id), None)
            if index is None: raise KeyError("Record not found")
            before = records[index].copy(); updated = {**before, **{k: v for k, v in changes.items() if k in WORKBOOKS[entity][1] and k not in immutable}}
            if "updated_at" in WORKBOOKS[entity][1]: updated["updated_at"] = utcnow()
            self._stamp_author(entity, updated, actor_id)
            self._validate(entity, updated, record_id); self._check_unique(entity, records, [updated]); records[index] = updated; self._commit(entity, records, changed_ids={record_id})
        self._reindex(entity, [updated]); self._audit(action, entity, record_id, before, updated, actor_id); return updated
    def bulk_update(self, entity: str, changes_by_id: dict[str, dict[str, Any]], actor_id: str = "system", action: str = "Update") -> list[dict[str, Any]]:
        """Apply many updates with one validated workbook write and one audit write."""
        self._guard_append_only(entity)
        if not changes_by_id: return []
        immutable = {"id", "created_at", "case_key"} if entity == "test_cases" else {"id", "created_at"}
        now = utcnow(); pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        with WorkbookLock(self.root / "temp" / f"{entity}.lock", self.locks[entity]):
            records = self._read_all(entity)
            positions = {r["id"]: i for i, r in enumerate(records)}
            missing = [record_id for record_id in changes_by_id if record_id not in positions]
            if missing: raise KeyError(f"Record not found: {missing[0]}")
            for record_id, changes in changes_by_id.items():
                before = records[positions[record_id]]
                updated = {**before, **{k: v for k, v in changes.items() if k in WORKBOOKS[entity][1] and k not in immutable}}
                if "updated_at" in WORKBOOKS[entity][1]: updated["updated_at"] = now
                self._stamp_author(entity, updated, actor_id)
                self._validate(entity, updated, record_id)
                records[positions[record_id]] = updated; pairs.append((before, updated))
            self._commit(entity, records, force_backup=len(pairs) > 1, changed_ids={after["id"] for _, after in pairs})
        self._reindex(entity, [after for _, after in pairs])
        self._audit_many([self._audit_entry(action, entity, after["id"], before, after, actor_id) for before, after in pairs])
        return [after for _, after in pairs]
    def delete(self, entity: str, record_id: str, actor_id: str = "system", permanent: bool = False) -> None:
        self._guard_append_only(entity)
        if not self.get(entity, record_id): raise KeyError("Record not found")
        if permanent:
            with WorkbookLock(self.root / "temp" / f"{entity}.lock", self.locks[entity]):
                rows = self._read_all(entity); before = next(r for r in rows if r["id"] == record_id); self._commit(entity, [r for r in rows if r["id"] != record_id], force_backup=True)
            self._unindex(entity, record_id)
            self._audit("Delete", entity, record_id, before, None, actor_id)
        else:
            if "archived" not in WORKBOOKS[entity][1]: raise ValueError(f"{entity} records cannot be archived")
            self.update(entity, record_id, {"archived": True}, actor_id, action="Archive")
    def delete_many(self, entity: str, record_ids: list[str], actor_id: str = "system") -> dict[str, Any]:
        """Archive many records with a single workbook write and one index update."""
        self._guard_append_only(entity)
        ids = [str(item) for item in dict.fromkeys(record_ids) if str(item)]
        if not ids:
            raise ValueError("Select at least one record to delete")
        if len(ids) > 500:
            raise ValueError("Delete at most 500 records at once")
        wanted = set(ids)
        now = utcnow()
        deleted: list[tuple[dict[str, Any], dict[str, Any]]] = []
        missing: list[str] = []
        with WorkbookLock(self.root / "temp" / f"{entity}.lock", self.locks[entity]):
            records = self._read_all(entity)
            found_ids = {record["id"] for record in records if record.get("id") in wanted}
            missing = [item for item in ids if item not in found_ids]
            if not found_ids:
                raise KeyError("Record not found")
            updated_records: list[dict[str, Any]] = []
            for record in records:
                if record.get("id") in found_ids and record.get("archived") is not True:
                    before = record.copy()
                    record = {**record}
                    if "archived" in WORKBOOKS[entity][1]:
                        record["archived"] = True
                    if "updated_at" in WORKBOOKS[entity][1]:
                        record["updated_at"] = now
                    self._stamp_author(entity, record, actor_id)
                    deleted.append((before, record))
                updated_records.append(record)
            self._commit(entity, updated_records, force_backup=True)
        self._reindex(entity, [after for _, after in deleted])
        self._audit_many([self._audit_entry("Archive", entity, after["id"], before, after, actor_id) for before, after in deleted])
        return {"deleted": len(deleted), "ids": [item[1]["id"] for item in deleted], "missing": missing}
    def batch_create(self, entity: str, rows: list[dict[str, Any]], actor_id: str = "system") -> list[dict[str, Any]]:
        """Validate the whole batch before replacing the workbook once.

        This is the transaction boundary used by imports: no valid early rows
        are persisted if a later row has a bad relationship or duplicate ID.
        """
        if entity == "audit": raise ValueError("Audit records are written by the system only")
        now = utcnow(); created: list[dict[str, Any]] = []
        with WorkbookLock(self.root / "temp" / f"{entity}.lock", self.locks[entity]):
            records = self._read_all(entity); occupied = {record["id"] for record in records}
            prepared: list[dict[str, Any]] = []
            for data in rows:
                record = {field: data.get(field, "") for field in WORKBOOKS[entity][1]}; record.update(data)
                record["id"] = str(data.get("id") or uuid.uuid4())
                if not _SAFE_ID.match(record["id"]): raise ValueError("Record IDs may only use letters, digits, '.', '_', ':' and '-'")
                if record["id"] in occupied: raise ValueError("Duplicate ID detected")
                if "created_at" in record and not record.get("created_at"): record["created_at"] = now
                if "updated_at" in record: record["updated_at"] = now
                if "archived" in record and record.get("archived") in (None, ""): record["archived"] = False
                self._stamp_author(entity, record, actor_id, creating=True)
                occupied.add(record["id"]); prepared.append(record)
            if entity == "test_cases":
                unkeyed = [record for record in prepared if not record.get("case_key")]
                for record, key in zip(unkeyed, self._allocate_case_keys(records + prepared, unkeyed)):
                    record["case_key"] = key
                seen_keys: set[str] = set()
                for record in prepared:
                    folded = str(record["case_key"]).casefold()
                    if folded in seen_keys: raise ValueError(f"Test case ID {record['case_key']} appears more than once")
                    seen_keys.add(folded)
            for record in prepared:
                self._validate(entity, record); created.append(record)
            self._check_unique(entity, records, created)
            self._commit(entity, records + created, force_backup=len(created) > 1, changed_ids={r["id"] for r in created})
        self._reindex(entity, created)
        self._audit_many([self._audit_entry("Create", entity, record["id"], {}, record, actor_id) for record in created])
        return created

    # ----------------------------------------------------------------- search
    def _unindex(self, entity: str, record_id: str) -> None:
        for word in self.index_words.pop((entity, record_id), set()):
            bucket = self.index.get(word)
            if bucket is not None:
                bucket.discard((entity, record_id))
                if not bucket: del self.index[word]
    def _reindex(self, entity: str, records: Iterable[dict[str, Any]]) -> None:
        if entity not in SEARCHABLE: return
        for record in records:
            record_id = str(record.get("id", ""))
            self._unindex(entity, record_id)
            if record.get("archived") is True: continue
            words = {word.strip(".,:;()[]{}\"'") for word in str(record).lower().split()}
            self.index_words[(entity, record_id)] = words
            for word in words: self.index[word].add((entity, record_id))
    def rebuild_index(self) -> int:
        self.index.clear(); self.index_words.clear(); count = 0
        for entity in WORKBOOKS:
            if entity not in SEARCHABLE:
                continue
            rows = self._load(entity)[2]
            self._reindex(entity, rows)
            count += sum(1 for row in rows if row.get("archived") is not True)
        return count
    def search(self, query: str, project_id: str = "") -> list[dict[str, Any]]:
        words = [w.lower() for w in query.split() if w]
        hits = set.intersection(*(self.index.get(w, set()) for w in words)) if words else set()
        results = []
        for entity, record_id in hits:
            if entity == "users": continue
            record = self.get(entity, record_id)
            if record and (not project_id or record.get("project_id") == project_id or entity == "projects" and record_id == project_id): results.append({"entity": entity, "record": record})
        return results[:100]

    # --------------------------------------------------------------- recovery
    def restore_backup(self, entity: str, backup_name: str) -> None:
        source = self.root / "backups" / entity / Path(backup_name).name
        if not source.exists(): raise FileNotFoundError("Backup not found")
        headers, _ = XlsxCodec.read(source)
        if not set(WORKBOOKS[entity][1]) - set(ADDITIVE_COLUMNS.get(entity, {})) <= set(headers): raise ValueError("Backup schema is invalid")
        with WorkbookLock(self.root / "temp" / f"{entity}.lock", self.locks[entity]): self._backup(entity, force=True); shutil.copy2(source, self.path_for(entity)); self.cache.pop(entity, None)
        self._migrate_additive_columns(entity)
        self.rebuild_index()
    def import_csv(self, entity: str, source: Path, mapping: dict[str, str], defaults: dict[str, Any], actor_id: str) -> dict[str, Any]:
        with source.open(newline="", encoding="utf-8-sig") as file: incoming = list(csv.DictReader(file))
        return self._import_rows(entity, incoming, mapping, defaults, actor_id)
    def import_xlsx(self, entity: str, source: Path, mapping: dict[str, str], defaults: dict[str, Any], actor_id: str) -> dict[str, Any]:
        """Import an external workbook through an explicit column mapping.

        Incoming workbooks are read only.  They are never copied over an
        internal workbook; mapped records pass the same transaction validation
        and batch-write path as API-created records.
        """
        headers, rows = XlsxCodec.read(source)
        if not headers: return {"created": 0, "errors": [{"row": 1, "error": "Workbook has no header row"}]}
        return self._import_rows(entity, rows, mapping, defaults, actor_id)
    def _import_rows(self, entity: str, incoming: list[dict[str, Any]], mapping: dict[str, str], defaults: dict[str, Any], actor_id: str) -> dict[str, Any]:
        valid, errors = [], []
        for row_number, row in enumerate(incoming, 2):
            data = {target: row.get(origin, "") for origin, target in mapping.items()}; data.update(defaults)
            try: self._validate(entity, {**{h: "" for h in WORKBOOKS[entity][1]}, **data, "id": str(uuid.uuid4())}); valid.append(data)
            except ValueError as exc: errors.append({"row": row_number, "error": str(exc)})
        if errors: return {"created": 0, "errors": errors, "message": "Nothing imported; correct the reported rows and try again."}
        return {"created": len(self.batch_create(entity, valid, actor_id)), "errors": []}
