import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
import server
from app.storage import ExcelStorageService, XlsxCodec, LEGACY_TEST_CASE_HEADERS
from app.service import TMExpertService

class ExcelStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.store = ExcelStorageService(self.tmp.name); self.svc = TMExpertService(self.store); self.svc.bootstrap()
    def tearDown(self): self.tmp.cleanup()

    def test_port_resolution_accepts_explicit_port_and_uses_next_free_port(self):
        saved = os.environ.get("TMEXPERT_PORT")
        try:
            os.environ["TMEXPERT_PORT"] = "8788"
            self.assertEqual(server.resolve_port("8788"), 8788)
            self.assertGreaterEqual(server.resolve_port("0"), 1024)
        finally:
            if saved is None:
                os.environ.pop("TMEXPERT_PORT", None)
            else:
                os.environ["TMEXPERT_PORT"] = saved

    def test_bootstrap_creates_named_excel_workbooks(self):
        self.assertTrue((Path(self.tmp.name) / "core" / "Projects.xlsx").exists())
        self.assertTrue((Path(self.tmp.name) / "repository" / "Test_Cases.xlsx").exists())
        self.assertEqual(self.store.list("projects")["total"], 1)
        self.assertTrue(self.store.list("projects")["items"][0]["created_at"])
    def test_duplicate_project_key_is_rejected(self):
        owner = self.store.list("users")["items"][0]["id"]
        with self.assertRaises(ValueError): self.store.create("projects", {"name":"Another", "key":"LMS", "owner_id":owner})
    def test_version_is_preserved_when_case_changes(self):
        case = self.store.list("test_cases")["items"][0]
        project = self.store.get("projects", case["project_id"])
        owner = self.store.list("users")["items"][0]
        plan = self.store.create("test_plans", {"project_id": project["id"], "name": "Version test", "owner_id": owner["id"]})
        suite = self.store.create("test_suites", {"project_id": project["id"], "plan_id": plan["id"], "name": "Version suite", "owner_id": owner["id"]})
        cycle = self.store.create("test_cycles", {"project_id": project["id"], "suite_id": suite["id"], "name": "Version cycle", "owner_id": owner["id"]})
        run = self.svc.create_test_run({"project_id": project["id"], "cycle_id": cycle["id"], "test_case_id": case["id"]}, owner["id"])
        changed = self.svc.update_test_case(case["id"], {"title":"Updated title"}, "system")
        self.assertEqual(changed["version"], "2")
        versions = self.store.list("test_case_versions", filters={"test_case_id":case["id"]}, page_size=20)["items"]
        self.assertEqual(len(versions), 2)
        current_steps = self.store.list("test_steps", filters={"test_case_id": case["id"], "version": "2"})["items"]
        self.assertEqual(len(current_steps), 2)
        self.assertEqual(self.svc.execution_detail(run["id"])["test_case"]["title"], case["title"])
    def test_archived_rows_are_hidden_and_backed_up(self):
        case = self.store.list("test_cases")["items"][0]; self.store.delete("test_cases", case["id"])
        self.assertEqual(self.store.list("test_cases")["total"], 0)
        self.assertTrue(list((Path(self.tmp.name) / "backups" / "test_cases").glob("*.xlsx")))
    def test_invalid_reference_is_rejected(self):
        with self.assertRaises(ValueError): self.store.create("folders", {"project_id":"missing", "name":"Invalid"})

    def test_batch_is_not_partially_written_when_a_record_is_invalid(self):
        project = self.store.list("projects")["items"][0]
        with self.assertRaises(ValueError):
            self.store.batch_create("folders", [{"project_id": project["id"], "name": "Valid"}, {"project_id": "missing", "name": "Invalid"}])
        self.assertEqual(self.store.list("folders", filters={"project_id": project["id"]})["total"], 2)

    def test_xlsx_import_maps_records_without_overwriting_storage(self):
        project = self.store.list("projects")["items"][0]
        source = Path(self.tmp.name) / "external.xlsx"
        XlsxCodec.write(source, ("Case name", "Priority"), [{"Case name": "Imported test", "Priority": "Medium"}])
        result = self.store.import_xlsx("folders", source, {"Case name": "name"}, {"project_id": project["id"]}, "system")
        self.assertEqual(result["created"], 1)
        self.assertTrue(any(row["name"] == "Imported test" for row in self.store.list("folders")["items"]))

    def test_system_actor_can_create_a_versioned_test_case(self):
        project = self.store.list("projects")["items"][0]
        folder = self.store.list("folders", filters={"project_id": project["id"]})["items"][0]
        case = self.svc.create_test_case({"project_id": project["id"], "folder_id": folder["id"], "title": "Created without user header", "priority": "Low", "test_type": "Manual", "steps": [{"action": "Act", "expected_result": "Result"}]}, "system")
        self.assertEqual(case["version"], "1")
        self.assertEqual(self.store.list("test_case_versions", filters={"test_case_id": case["id"]})["total"], 1)

    def test_project_creation_makes_a_repository_root(self):
        owner = self.store.list("users")["items"][0]
        project = self.svc.create_project({"name": "Payments", "key": "PAY", "owner_id": owner["id"]}, owner["id"])
        folders = self.store.list("folders", filters={"project_id": project["id"]})["items"]
        self.assertEqual(folders[0]["name"], "Test Repository")

    def test_approval_updates_test_case_status(self):
        case = self.store.list("test_cases")["items"][0]
        reviewer = self.store.list("users")["items"][0]
        approved = self.svc.approve_test_case(case["id"], reviewer["id"], "Approved", "Ready", reviewer["id"])
        self.assertEqual(approved["status"], "Approved")

    def test_excel_template_imports_multiple_steps_and_exports_cases(self):
        project = self.store.list("projects")["items"][0]
        folder = self.store.list("folders", filters={"project_id": project["id"]})["items"][0]
        columns = self.svc.CASE_IMPORT_COLUMNS
        workbook = Path(self.tmp.name) / "import.xlsx"
        XlsxCodec.write(workbook, columns, [
            {"Scenario Name": "Imported login", "Expected Result": "User is authenticated", "Folder Path": folder["path"], "Priority": "High", "Step Number": "1", "Action": "Open login", "Step Expected Result": "Login form appears"},
            {"Step Number": "2", "Action": "Submit credentials", "Expected Result": "User is signed in"},
        ])

        result = self.svc.import_test_cases_xlsx(workbook.read_bytes(), project["id"], "system")

        self.assertEqual(result, {"created": 1, "errors": []})
        case = next(row for row in self.store.list("test_cases", filters={"project_id": project["id"]})["items"] if row["title"] == "Imported login")
        self.assertEqual(case["expected_result"], "User is authenticated")
        self.assertEqual(case["last_status"], "Not Run")
        steps = self.store.list("test_steps", filters={"test_case_id": case["id"]})["items"]
        self.assertEqual(len(steps), 2)
        self.assertEqual([step["step_number"] for step in steps], ["1", "2"])
        headers, exported = XlsxCodec.read_bytes(self.svc.export_cases_xlsx(project["id"]))
        self.assertEqual(tuple(headers), columns)
        self.assertEqual(sum(row["Scenario Name"] == "Imported login" for row in exported), 2)
        template_headers, template_rows = XlsxCodec.read_bytes(self.svc.export_cases_xlsx(project["id"], template=True))
        self.assertEqual(tuple(template_headers), columns)
        self.assertEqual(template_rows, [])

    def test_excel_import_validation_does_not_create_partial_cases(self):
        project = self.store.list("projects")["items"][0]
        initial_count = self.store.list("test_cases", filters={"project_id": project["id"]})["total"]
        workbook = Path(self.tmp.name) / "invalid.xlsx"
        XlsxCodec.write(workbook, self.svc.CASE_IMPORT_COLUMNS, [
            {"Scenario Name": "Valid case", "Folder Path": "/User Management/Login"},
            {"Scenario Name": "Invalid folder", "Folder Path": "/missing"},
        ])

        result = self.svc.import_test_cases_xlsx(workbook.read_bytes(), project["id"], "system")

        self.assertEqual(result["created"], 0)
        self.assertTrue(result["errors"])
        self.assertEqual(self.store.list("test_cases", filters={"project_id": project["id"]})["total"], initial_count)

    def test_import_accepts_scenario_name_alone_and_optional_expected_result(self):
        project = self.store.list("projects")["items"][0]
        workbook = Path(self.tmp.name) / "scenario-only.xlsx"
        XlsxCodec.write(workbook, self.svc.CASE_IMPORT_COLUMNS, [
            {"Scenario Name": "Only scenario is required"},
            {"Scenario Name": "Scenario with expected result", "Expected Result": "The order confirmation is shown"},
        ])

        result = self.svc.import_test_cases_xlsx(workbook.read_bytes(), project["id"], "system")

        self.assertEqual(result, {"created": 2, "errors": []})
        cases = self.store.list("test_cases", filters={"project_id": project["id"]})["items"]
        only_scenario = next(case for case in cases if case["title"] == "Only scenario is required")
        with_expected = next(case for case in cases if case["title"] == "Scenario with expected result")
        self.assertEqual(only_scenario["expected_result"], "")
        self.assertEqual(only_scenario["last_status"], "Not Run")
        self.assertEqual(self.store.list("test_steps", filters={"test_case_id": only_scenario["id"]})["total"], 0)
        self.assertEqual(with_expected["expected_result"], "The order confirmation is shown")

    def test_legacy_test_case_workbook_schema_is_migrated(self):
        project = self.store.list("projects")["items"][0]
        case = self.store.list("test_cases", filters={"project_id": project["id"]})["items"][0]
        path = self.store.path_for("test_cases")
        legacy_headers = LEGACY_TEST_CASE_HEADERS
        XlsxCodec.write(path, legacy_headers, [case])

        migrated_store = ExcelStorageService(self.tmp.name)
        migrated_case = migrated_store.get("test_cases", case["id"])

        self.assertEqual(migrated_case["expected_result"], "")
        self.assertEqual(migrated_case["last_status"], "Not Run")
        self.assertTrue(list((Path(self.tmp.name) / "backups" / "test_cases").glob("*.xlsx")))

    def test_additive_workbook_columns_are_preserved_on_update(self):
        case = self.store.list("test_cases")["items"][0]
        path = self.store.path_for("test_cases")
        headers = (*self.store._read_all("test_cases")[0].keys(), "future_metadata")
        XlsxCodec.write(path, headers, [{**case, "future_metadata": "keep this value"}])

        compatible_store = ExcelStorageService(self.tmp.name)
        updated = compatible_store.update("test_cases", case["id"], {"last_status": "Passed"})
        headers_after_update, rows_after_update = XlsxCodec.read(path)

        self.assertEqual(updated["last_status"], "Passed")
        self.assertIn("future_metadata", headers_after_update)
        self.assertEqual(rows_after_update[0]["future_metadata"], "keep this value")

    def test_large_test_case_workbook_survives_reload(self):
        case = self.store.list("test_cases")["items"][0]
        records = [{**case, "id": f"case-{index}", "title": f"Bulk scenario {index}"} for index in range(1000)]
        self.store._commit("test_cases", records)

        reloaded_store = ExcelStorageService(self.tmp.name)
        result = reloaded_store.list("test_cases", page=20, page_size=50)

        self.assertEqual(result["total"], 1000)
        self.assertEqual(result["items"][-1]["title"], "Bulk scenario 999")

    def test_standard_excel_shared_strings_and_sparse_columns_are_read(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Import" sheetId="1" r:id="rId1"/></sheets></workbook>')
            archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
            archive.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Test Case</t></si><si><t>Folder Path</t></si><si><t>Checkout</t></si><si><t>/Repository</t></si></sst>')
            archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c></row><row r="2"><c r="A2" t="s"><v>2</v></c><c r="C2" t="s"><v>3</v></c></row></sheetData></worksheet>')

        headers, rows = XlsxCodec.read_bytes(output.getvalue())

        self.assertEqual(headers, ["Test Case", "", "Folder Path"])
        self.assertEqual(rows, [{"Test Case": "Checkout", "Folder Path": "/Repository"}])

    def test_execution_results_update_step_and_run_status(self):
        project = self.store.list("projects")["items"][0]
        owner = self.store.list("users")["items"][0]
        case = self.store.list("test_cases", filters={"project_id": project["id"]})["items"][0]
        plan = self.store.create("test_plans", {"project_id": project["id"], "name": "Regression", "owner_id": owner["id"]})
        suite = self.store.create("test_suites", {"project_id": project["id"], "plan_id": plan["id"], "name": "Regression suite", "owner_id": owner["id"]})
        cycle = self.store.create("test_cycles", {"project_id": project["id"], "suite_id": suite["id"], "name": "QA cycle", "owner_id": owner["id"]})
        run = self.svc.create_test_run({"project_id": project["id"], "cycle_id": cycle["id"], "test_case_id": case["id"], "tester_id": owner["id"], "status": "Not Run"}, owner["id"])
        details = self.svc.execution_detail(run["id"])
        steps = details["steps"]

        partial = self.svc.save_execution_results(run["id"], [{"step_id": steps[0]["step"]["id"], "status": "Passed", "actual_result": "As expected"}], owner["id"])
        self.assertEqual(partial["run"]["status"], "In Progress")
        self.assertEqual(self.store.get("test_cases", case["id"])["last_status"], "In Progress")
        complete = self.svc.save_execution_results(run["id"], [{"step_id": steps[1]["step"]["id"], "status": "Failed", "actual_result": "Dashboard did not load"}], owner["id"])
        self.assertEqual(complete["run"]["status"], "Failed")
        self.assertEqual(self.store.get("test_cases", case["id"])["last_status"], "Failed")
        self.assertEqual(len([item for item in complete["steps"] if item["result"]]), 2)

    def test_last_status_can_be_updated_independently_of_approval(self):
        case = self.store.list("test_cases")["items"][0]

        updated = self.svc.update_test_case_last_status(case["id"], "Passed", "system")

        self.assertEqual(updated["status"], "Approved")
        self.assertEqual(updated["last_status"], "Passed")
        with self.assertRaisesRegex(ValueError, "Last status must be one of"):
            self.svc.update_test_case_last_status(case["id"], "Unknown", "system")

    def test_execution_requires_actual_result_for_pass_or_fail(self):
        project = self.store.list("projects")["items"][0]
        owner = self.store.list("users")["items"][0]
        case = self.store.list("test_cases", filters={"project_id": project["id"]})["items"][0]
        plan = self.store.create("test_plans", {"project_id": project["id"], "name": "Smoke", "owner_id": owner["id"]})
        suite = self.store.create("test_suites", {"project_id": project["id"], "plan_id": plan["id"], "name": "Smoke suite", "owner_id": owner["id"]})
        cycle = self.store.create("test_cycles", {"project_id": project["id"], "suite_id": suite["id"], "name": "Smoke cycle", "owner_id": owner["id"]})
        run = self.svc.create_test_run({"project_id": project["id"], "cycle_id": cycle["id"], "test_case_id": case["id"]}, owner["id"])
        step = self.svc.execution_detail(run["id"])["steps"][0]["step"]

        with self.assertRaisesRegex(ValueError, "Actual Result is required"):
            self.svc.save_execution_results(run["id"], [{"step_id": step["id"], "status": "Passed"}], owner["id"])
        self.assertEqual(self.store.list("execution_results", filters={"test_run_id": run["id"]})["total"], 0)

    def test_test_run_cannot_mix_projects(self):
        project = self.store.list("projects")["items"][0]
        owner = self.store.list("users")["items"][0]
        case = self.store.list("test_cases", filters={"project_id": project["id"]})["items"][0]
        other_project = self.svc.create_project({"name": "Payments", "key": "PAY", "owner_id": owner["id"]}, owner["id"])
        plan = self.store.create("test_plans", {"project_id": other_project["id"], "name": "Smoke", "owner_id": owner["id"]})
        suite = self.store.create("test_suites", {"project_id": other_project["id"], "plan_id": plan["id"], "name": "Smoke suite", "owner_id": owner["id"]})
        cycle = self.store.create("test_cycles", {"project_id": other_project["id"], "suite_id": suite["id"], "name": "Smoke cycle", "owner_id": owner["id"]})

        with self.assertRaisesRegex(ValueError, "same project"):
            self.svc.create_test_run({"project_id": other_project["id"], "cycle_id": cycle["id"], "test_case_id": case["id"]}, owner["id"])

if __name__ == "__main__": unittest.main()
