"""Phase 9: Excel import preview, template recognition, validation, history and exports."""
import base64
import io
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import server
from app.storage import XlsxCodec
from app.excel_import import STANDARD_COLUMNS
from test_api import Client, ADMIN_EMAIL, ADMIN_PASSWORD


def workbook(headers, rows, tmp):
    path = Path(tmp) / f"w{len(list(Path(tmp).glob('w*.xlsx')))}.xlsx"
    XlsxCodec.write(path, headers, rows)
    return path.read_bytes()


def multi_sheet(sheets):
    """Build a workbook with several named sheets (list of (name, headers, rows))."""
    def sheet_xml(headers, rows):
        def cell(value): return f'<c t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
        body = "".join(f"<row>{''.join(cell(row.get(h, '')) for h in headers)}</row>" for row in [dict(zip(headers, headers))] + rows)
        return f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{body}</sheetData></worksheet>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                         + "".join(f'<sheet name="{name}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, (name, _, _) in enumerate(sheets)) + "</sheets></workbook>")
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(sheets))) + "</Relationships>")
        for i, (_, headers, rows) in enumerate(sheets):
            archive.writestr(f"xl/worksheets/sheet{i + 1}.xml", sheet_xml(headers, rows))
    return out.getvalue()


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = server.make_server("127.0.0.1", 0, self.tmp.name)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.store = self.httpd.app.store
        self.admin = Client(self.base)
        self.admin.login(ADMIN_EMAIL, "change-me")
        self.admin.call("POST", "/api/auth/change-password", {"current_password": "change-me", "new_password": ADMIN_PASSWORD})
        self.project = self.store.list("projects")["items"][0]

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join(5); self.tmp.cleanup()

    def preview(self, data, **options):
        return self.admin.call("POST", "/api/import/test-cases/preview", {"project_id": self.project["id"], "workbook": base64.b64encode(data).decode(), **options})

    def do_import(self, data, filename="cases.xlsx", **options):
        return self.admin.call("POST", "/api/import/test-cases", {"project_id": self.project["id"], "workbook": base64.b64encode(data).decode(), "filename": filename, **options})

    def standard(self, rows):
        return workbook(STANDARD_COLUMNS, rows, self.tmp.name)

    def case_count(self):
        return self.store.list("test_cases", filters={"project_id": self.project["id"]})["total"]

    # ------------------------------------------------------------ recognition
    def test_preview_recognises_the_template_and_writes_nothing(self):
        before = self.case_count()
        data = self.standard([{"Scenario Name": "Login works", "Folder Path": "/User Management/Login", "Priority": "High", "Step Number": "1", "Action": "Open", "Step Expected Result": "Shown"},
                              {"Step Number": "2", "Action": "Submit", "Step Expected Result": "Signed in"}])
        status, plan = self.preview(data)
        self.assertEqual(status, 200)
        self.assertEqual((plan["recognition"]["template"], plan["recognition"]["standard_columns"], plan["sheet"]), ("standard", 14, "Records"))
        self.assertEqual((plan["can_import"], plan["summary"]["cases"], plan["summary"]["steps"], plan["cases"][0]["module_path"], plan["cases"][0]["status"]),
                         (True, 1, 2, "/User Management/Login", "ready"))
        self.assertEqual(self.case_count(), before)

    def test_aliases_and_unknown_columns_are_reported(self):
        data = workbook(("Test Case", "Module", "Owner", "Action", "Expected Outcome"), [{"Test Case": "Alias case", "Module": "Login", "Owner": "Arun", "Action": "Go", "Expected Outcome": "Done"}], self.tmp.name)
        plan = self.preview(data)[1]
        self.assertEqual(plan["recognition"]["template"], "partial")
        self.assertEqual({m["column"]: m["field"] for m in plan["recognition"]["matched"]}, {"Test Case": "Scenario Name", "Module": "Folder Path", "Action": "Action", "Expected Outcome": "Step Expected Result"})
        self.assertEqual([i["column"] for i in plan["recognition"]["ignored"]], ["Owner"])
        self.assertTrue(any(w["column"] == "Owner" for w in plan["warnings"]))
        self.assertTrue(plan["can_import"])

    def test_missing_title_column_is_reported(self):
        plan = self.preview(workbook(("Name", "Steps"), [{"Name": "x", "Steps": "y"}], self.tmp.name))[1]
        self.assertEqual((plan["can_import"], plan["recognition"]["missing_required"], plan["errors"][0]["column"]), (False, ["Scenario Name"], "Scenario Name"))

    def test_the_sheet_with_test_cases_is_found_and_can_be_chosen(self):
        data = multi_sheet([("Instructions", ("Read me",), [{"Read me": "Fill the Cases sheet"}]),
                            ("Cases", ("Scenario Name", "Folder Path"), [{"Scenario Name": "From second sheet", "Folder Path": "Login"}]),
                            ("Archive", ("Scenario Name",), [{"Scenario Name": "Old one"}])])
        plan = self.preview(data)[1]
        self.assertEqual((plan["sheet"], plan["sheets"], plan["cases"][0]["title"]), ("Cases", ["Instructions", "Cases", "Archive"], "From second sheet"))
        self.assertEqual(self.preview(data, sheet="Archive")[1]["cases"][0]["title"], "Old one")
        self.assertEqual(self.preview(data, sheet="Nope")[0], 400)

    # ------------------------------------------------------------- validation
    def test_row_level_validation(self):
        data = self.standard([
            {"Scenario Name": "Bad priority", "Folder Path": "Login", "Priority": "Urgent"},
            {"Scenario Name": "Excel error", "Folder Path": "Login", "Priority": "#N/A"},
            {"Scenario Name": "X" * 201, "Folder Path": "Login"},
            {"Scenario Name": "Bad status", "Folder Path": "Login", "Last Status": "Done"},
            {"Scenario Name": "Half step", "Folder Path": "Login", "Action": "Click"},
            {"Scenario Name": "Dup steps", "Folder Path": "Login", "Step Number": "1", "Action": "a", "Step Expected Result": "b"},
            {"Scenario Name": "Dup steps", "Folder Path": "Login", "Step Number": "1", "Action": "c", "Step Expected Result": "d"},
            {"Scenario Name": "Typo module", "Folder Path": "/User Managment/Login"},
        ])
        plan = self.preview(data)[1]
        self.assertFalse(plan["can_import"])
        by_row = {}
        for error in plan["errors"]: by_row.setdefault(error["row"], []).append((error["column"], error["message"]))
        self.assertIn(("Priority", "Priority must be Critical, High, Medium, or Low"), by_row[2])
        self.assertIn(("Priority", "Contains the Excel error value #N/A"), by_row[3])
        self.assertIn("longer than 200", by_row[4][0][1])
        self.assertEqual(by_row[5][0][0], "Last Status")
        self.assertEqual(by_row[6][0], ("Step Expected Result", "Action and Step Expected Result must both be provided"))
        self.assertIn("duplicated", by_row[7][0][1])
        self.assertIn("Did you mean /User Management/Login?", by_row[9][0][1])
        statuses = {c["row"]: c["status"] for c in plan["cases"]}
        self.assertEqual(statuses[3], "error")  # Excel error value marks the case itself
        with_modules = self.preview(data, create_missing_modules=True)[1]
        self.assertTrue(any("check for a typo" in w["message"] for w in with_modules["warnings"]))
        status, body = self.do_import(data)
        self.assertEqual((status, body["created"]), (200, 0))
        self.assertTrue(body["errors"])

    def test_missing_modules_can_be_created_as_draft(self):
        data = self.standard([{"Scenario Name": "Refund to card", "Folder Path": "/Payments/Refunds"}, {"Scenario Name": "Login again", "Folder Path": "Login"}])
        self.assertFalse(self.preview(data)[1]["can_import"])
        plan = self.preview(data, create_missing_modules=True)[1]
        self.assertEqual((plan["can_import"], plan["new_modules"], plan["cases"][0]["module_new"]), (True, ["/Payments/Refunds"], True))
        status, result = self.do_import(data, create_missing_modules=True)
        self.assertEqual((status, result["created"], result["modules_created"]), (201, 2, 2))
        folders = {f["path"]: f for f in self.store.list("folders", filters={"project_id": self.project["id"]})["items"]}
        self.assertEqual((folders["/Payments"]["approval_status"], folders["/Payments/Refunds"]["approval_status"]), ("Draft", "Draft"))
        refund = next(c for c in self.store.list("test_cases")["items"] if c["title"] == "Refund to card")
        self.assertEqual(refund["folder_id"], folders["/Payments/Refunds"]["id"])

    def test_optional_test_case_id_column(self):
        headers = (*STANDARD_COLUMNS, "Test Case ID")
        data = workbook(headers, [{"Scenario Name": "Keyed one", "Folder Path": "Login", "Test Case ID": "LOGIN-77"},
                                  {"Scenario Name": "Keyed two", "Folder Path": "Login", "Test Case ID": "LOGIN-77"},
                                  {"Scenario Name": "Keyed three", "Folder Path": "Login", "Test Case ID": "bad id!"},
                                  {"Scenario Name": "Keyed four", "Folder Path": "Login", "Test Case ID": "LMS-TC-0001"}], self.tmp.name)
        plan = self.preview(data)[1]
        self.assertTrue(plan["recognition"]["has_id_column"])
        messages = {e["row"]: e["message"] for e in plan["errors"]}
        self.assertIn("also used on row 2", messages[3]); self.assertIn("may use letters", messages[4]); self.assertIn("already exists", messages[5])
        ok = workbook(headers, [{"Scenario Name": "Keyed one", "Folder Path": "Login", "Test Case ID": "LOGIN-77"}], self.tmp.name)
        self.assertEqual(self.do_import(ok)[0], 201)
        self.assertTrue(any(c["case_key"] == "LOGIN-77" for c in self.store.list("test_cases")["items"]))

    def test_duplicates_are_flagged_and_can_be_allowed(self):
        data = self.standard([{"Scenario Name": "Sign in with valid credentials", "Folder Path": "/User Management/Login"}])
        plan = self.preview(data)[1]
        self.assertEqual((plan["cases"][0]["status"], plan["cases"][0]["duplicate_of"], plan["summary"]["duplicates"]), ("duplicate", "LMS-TC-0001", 1))
        status, body = self.do_import(data)
        self.assertEqual((status, body["duplicates_only"]), (200, True))
        allowed = self.preview(data, allow_duplicates=True)[1]
        self.assertEqual((allowed["can_import"], allowed["cases"][0]["status"]), (True, "ready"))
        self.assertEqual(self.do_import(data, allow_duplicates=True)[1]["created"], 1)

    # -------------------------------------------------------- commit & history
    def test_import_backs_up_keeps_the_file_and_records_history(self):
        data = self.standard([{"Scenario Name": "Single imported case", "Folder Path": "Login", "Action": "a", "Step Expected Result": "b"}])
        backups = Path(self.tmp.name) / "backups" / "test_steps"
        before = len(list(backups.glob("*.xlsx"))) if backups.exists() else 0
        status, result = self.do_import(data, filename="sprint-12.xlsx")
        self.assertEqual((status, result["created"], result["steps_created"]), (201, 1, 1))
        self.assertGreater(len(list(backups.glob("*.xlsx"))), before)
        self.do_import(self.standard([{"Scenario Name": "Broken", "Folder Path": "Nowhere"}]), filename="broken.xlsx")
        history = self.admin.call("GET", f"/api/imports?project_id={self.project['id']}")[1]["items"]
        self.assertEqual([(h["filename"], h["status"]) for h in history], [("broken.xlsx", "Rejected"), ("sprint-12.xlsx", "Imported")])
        imported = history[1]
        self.assertEqual((imported["cases_created"], imported["steps_created"], imported["imported_by_name"], imported["sheet"]), ("1", "1", "Avery Chen", "Records"))
        status, original = self.admin.call("GET", f"/api/imports/{imported['id']}/file")
        self.assertEqual((status, original), (200, data))
        self.assertFalse(history[0]["has_file"])
        with self.assertRaises(ValueError): self.store.update("import_history", imported["id"], {"status": "Imported"})

    # ------------------------------------------------------------------ export
    def test_standard_export_is_unchanged_and_ids_are_opt_in(self):
        status, blob = self.admin.call("GET", f"/api/export/test-cases.xlsx?project_id={self.project['id']}")
        headers, rows = XlsxCodec.read_bytes(blob)
        self.assertEqual(tuple(headers), STANDARD_COLUMNS)
        headers, rows = XlsxCodec.read_bytes(self.admin.call("GET", f"/api/export/test-cases.xlsx?project_id={self.project['id']}&include_ids=true")[1])
        self.assertEqual((tuple(headers[:14]), headers[14], rows[0]["Test Case ID"]), (STANDARD_COLUMNS, "Test Case ID", "LMS-TC-0001"))
        template, _ = XlsxCodec.read_bytes(self.admin.call("GET", f"/api/test-case-import-template.xlsx?project_id={self.project['id']}&include_ids=true")[1])
        self.assertEqual(tuple(template), STANDARD_COLUMNS)
        # Round trip into another project keeps the IDs.
        other = self.httpd.app.service.create_project({"name": "Copy", "key": "CPY", "owner_id": ""}, "system")
        _, blob = self.admin.call("GET", f"/api/export/test-cases.xlsx?project_id={self.project['id']}&include_ids=true")
        status, result = self.admin.call("POST", "/api/import/test-cases", {"project_id": other["id"], "workbook": base64.b64encode(blob).decode(), "create_missing_modules": True})
        self.assertEqual(status, 201, result)
        self.assertEqual(self.store.list("test_cases", filters={"project_id": other["id"]})["items"][0]["case_key"], "LMS-TC-0001")

    def test_team_members_cannot_import(self):
        self.admin.call("POST", "/api/users", {"name": "Tara", "email": "tara@x.local", "role": "Team Member", "password": "Temp-Pass-1"})
        team = Client(self.base); team.login("tara@x.local", "Temp-Pass-1")
        team.call("POST", "/api/auth/change-password", {"current_password": "Temp-Pass-1", "new_password": "Team-Pass-22"})
        data = base64.b64encode(self.standard([{"Scenario Name": "x"}])).decode()
        self.assertEqual(team.call("POST", "/api/import/test-cases/preview", {"project_id": self.project["id"], "workbook": data})[0], 403)
        self.assertEqual(team.call("GET", f"/api/imports?project_id={self.project['id']}")[0], 403)


if __name__ == "__main__": unittest.main()
