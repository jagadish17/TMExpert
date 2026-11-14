"""Phase 3: approval lifecycle, module approval, duplicates, bulk work and history."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

import server
from app.storage import ExcelStorageService, XlsxCodec, WORKBOOKS
from test_api import Client, ADMIN_EMAIL, ADMIN_PASSWORD

STEP = [{"action": "Open page", "expected_result": "Page opens"}]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = server.make_server("127.0.0.1", 0, self.tmp.name)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.store = self.httpd.app.store
        self.admin = Client(self.base)
        self.admin.login(ADMIN_EMAIL, "change-me")
        self.admin.call("POST", "/api/auth/change-password", {"current_password": "change-me", "new_password": ADMIN_PASSWORD})
        self.admin_id = self.admin.call("GET", "/api/auth/session")[1]["user"]["id"]
        self.project = self.store.list("projects")["items"][0]
        self.login_folder = next(f for f in self.store.list("folders")["items"] if f["name"] == "Login")
        self.team = None

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join(5); self.tmp.cleanup()

    def team_client(self):
        if self.team: return self.team
        self.admin.call("POST", "/api/users", {"name": "Tara Q", "email": "tara@tmexpert.local", "role": "Team Member", "password": "Temp-Pass-1"})
        self.team = Client(self.base)
        self.team.login("tara@tmexpert.local", "Temp-Pass-1")
        self.team.call("POST", "/api/auth/change-password", {"current_password": "Temp-Pass-1", "new_password": "Team-Pass-22"})
        return self.team

    def create_case(self, title="Workflow case", folder=None, **extra):
        status, case = self.admin.call("POST", "/api/test-cases", {"project_id": self.project["id"], "folder_id": (folder or self.login_folder)["id"], "title": title, "priority": "Medium", "steps": STEP, **extra})
        self.assertEqual(status, 201, case)
        return case

    def transition(self, case_id, status, comment="", client=None):
        return (client or self.admin).call("POST", f"/api/test-cases/{case_id}/transition", {"status": status, "comment": comment})

    def team_case_ids(self):
        return {c["id"] for c in self.team_client().call("GET", f"/api/test_cases?project_id={self.project['id']}&page_size=200")[1]["items"]}

    # ------------------------------------------------------------ lifecycle
    def test_full_lifecycle_with_rejection_and_resubmission(self):
        case = self.create_case()
        self.assertEqual(case["status"], "Draft")
        self.assertEqual(self.transition(case["id"], "In Review")[1]["status"], "In Review")
        status, body = self.transition(case["id"], "Rejected")
        self.assertEqual(status, 400); self.assertIn("reason", body["error"])
        status, rejected = self.transition(case["id"], "Rejected", "Step 1 is ambiguous")
        self.assertEqual((status, rejected["status"]), (200, "Rejected"))
        status, edited = self.admin.call("PUT", f"/api/test_cases/{case['id']}", {"title": "Workflow case (clarified)", "version_comment": "Clarified step"})
        self.assertEqual((status, edited["status"], edited["version"]), (200, "Draft", "2"))
        self.assertEqual(self.transition(case["id"], "In Review")[1]["status"], "In Review")
        status, approved = self.transition(case["id"], "Approved", "Looks good")
        self.assertEqual((status, approved["status"], approved["reviewer_id"]), (200, "Approved", self.admin_id))
        history = self.admin.call("GET", f"/api/test-cases/{case['id']}/details")[1]["approvals"]
        self.assertEqual([h["decision"] for h in history], ["Approved", "Submitted", "Rejected", "Submitted"])
        self.assertEqual(history[2]["comment"], "Step 1 is ambiguous")
        self.assertEqual(history[0]["version"], "2")
        self.assertEqual(history[0]["reviewer_name"], "Avery Chen")

    def test_invalid_transitions_are_refused(self):
        case = self.create_case()
        self.assertEqual(self.transition(case["id"], "Rejected", "nope")[0], 400)
        self.assertEqual(self.transition(case["id"], "Published")[0], 400)
        self.transition(case["id"], "Deprecated")
        self.assertEqual(self.transition(case["id"], "In Review")[0], 400)
        self.assertEqual(self.transition(case["id"], "Draft")[1]["status"], "Draft")  # restore
        decisions = [a["decision"] for a in self.store.list("approvals", filters={"test_case_id": case["id"]})["items"]]
        self.assertEqual(decisions, ["Deprecated", "Restored"])

    def test_saving_without_changes_keeps_version_and_approval(self):
        case = self.create_case()
        self.transition(case["id"], "Approved")
        status, same = self.admin.call("PUT", f"/api/test_cases/{case['id']}", {"title": case["title"], "steps": STEP})
        self.assertEqual((status, same["version"], same["status"]), (200, "1", "Approved"))
        self.assertEqual(self.store.list("test_case_versions", filters={"test_case_id": case["id"]})["total"], 1)

    def test_editing_an_approved_case_hides_it_from_the_team_until_reapproved(self):
        case = self.create_case()
        self.transition(case["id"], "Approved")
        self.assertIn(case["id"], self.team_case_ids())
        self.admin.call("PUT", f"/api/test_cases/{case['id']}", {"steps": STEP + [{"action": "Submit", "expected_result": "Saved"}]})
        self.assertEqual(self.store.get("test_cases", case["id"])["status"], "Draft")
        self.assertNotIn(case["id"], self.team_case_ids())

    def test_team_member_cannot_approve_or_see_review_history(self):
        case = self.create_case(); self.transition(case["id"], "Approved")
        team = self.team_client()
        other = self.create_case("Pending one"); self.transition(other["id"], "In Review")
        self.assertEqual(self.transition(other["id"], "Approved", client=team)[0], 403)
        self.assertEqual(team.call("POST", f"/api/folders/{self.login_folder['id']}/transition", {"status": "Draft"})[0], 403)
        self.assertEqual(team.call("POST", "/api/test-cases/bulk", {"action": "approve", "ids": [other["id"]]})[0], 403)
        self.assertEqual(team.call("GET", f"/api/approvals/queue?project_id={self.project['id']}")[0], 403)
        details = team.call("GET", f"/api/test-cases/{case['id']}/details")[1]
        self.assertEqual(details["case"]["status"], "Approved")
        self.assertNotIn("approvals", details)
        self.assertEqual(team.call("GET", f"/api/test-cases/{other['id']}/details")[0], 404)

    # ------------------------------------------------------------- modules
    def test_module_approval_controls_team_visibility(self):
        status, module = self.admin.call("POST", "/api/folders", {"project_id": self.project["id"], "parent_id": "", "name": "Payments"})
        self.assertEqual((status, module["approval_status"], module["path"]), (201, "Draft", "/Payments"))
        status, sub = self.admin.call("POST", "/api/folders", {"project_id": self.project["id"], "parent_id": module["id"], "name": "Refunds"})
        self.admin.call("POST", f"/api/folders/{sub['id']}/transition", {"status": "Approved"})
        case = self.create_case("Refund a card payment", folder=sub)
        self.transition(case["id"], "Approved")
        self.assertNotIn(case["id"], self.team_case_ids())  # parent module still Draft
        team_folders = {f["id"] for f in self.team.call("GET", f"/api/folders?project_id={self.project['id']}")[1]["items"]}
        self.assertNotIn(sub["id"], team_folders)
        self.assertEqual(self.admin.call("POST", f"/api/folders/{module['id']}/transition", {"status": "Rejected"})[0], 400)
        self.admin.call("POST", f"/api/folders/{module['id']}/transition", {"status": "In Review"})
        approved = self.admin.call("POST", f"/api/folders/{module['id']}/transition", {"status": "Approved", "comment": "Scope agreed"})[1]
        self.assertEqual((approved["approval_status"], approved["reviewed_by"]), ("Approved", self.admin_id))
        self.assertIn(case["id"], self.team_case_ids())
        decisions = [a["decision"] for a in self.store.list("approvals", filters={"folder_id": module["id"]})["items"]]
        self.assertEqual(decisions, ["Submitted", "Approved"])

    def test_module_names_are_unique_per_parent_and_rename_updates_paths(self):
        status, module = self.admin.call("POST", "/api/folders", {"project_id": self.project["id"], "parent_id": "", "name": "Enrollment"})
        sub = self.admin.call("POST", "/api/folders", {"project_id": self.project["id"], "parent_id": module["id"], "name": "Waitlist"})[1]
        self.assertEqual(self.admin.call("POST", "/api/folders", {"project_id": self.project["id"], "parent_id": "", "name": "enrollment"})[0], 400)
        self.assertEqual(self.admin.call("POST", "/api/folders", {"project_id": self.project["id"], "parent_id": "", "name": "a/b"})[0], 400)
        status, renamed = self.admin.call("PUT", f"/api/folders/{module['id']}/rename", {"name": "Enrolment"})
        self.assertEqual((status, renamed["path"]), (200, "/Enrolment"))
        self.assertEqual(self.store.get("folders", sub["id"])["path"], "/Enrolment/Waitlist")

    def test_existing_folders_become_approved_modules_on_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExcelStorageService(tmp)
            old = WORKBOOKS["folders"][1][:8]
            XlsxCodec.write(store.path_for("folders"), old, [{"id": "f1", "project_id": "p", "parent_id": "", "name": "Legacy", "path": "/Legacy", "archived": "False"}])
            upgraded = ExcelStorageService(tmp)
            self.assertEqual(upgraded.get("folders", "f1")["approval_status"], "Approved")
            headers, _ = XlsxCodec.read(upgraded.path_for("folders"))
            self.assertEqual(tuple(headers[:8]), old)

    def test_stale_module_paths_are_repaired_and_old_paths_still_import(self):
        svc = self.httpd.app.service
        assets = self.admin.call("POST", "/api/folders", {"project_id": self.project["id"], "parent_id": "", "name": "Assets"})[1]
        survey = self.admin.call("POST", "/api/folders", {"project_id": self.project["id"], "parent_id": "", "name": "Survey"})[1]
        listing = self.admin.call("POST", "/api/folders", {"project_id": self.project["id"], "parent_id": survey["id"], "name": "Listing"})[1]
        # Reproduce the older bug: parent changed without rewriting stored paths.
        self.store.update("folders", survey["id"], {"parent_id": assets["id"]})
        self.assertEqual(self.store.get("folders", listing["id"])["path"], "/Survey/Listing")
        self.assertEqual(svc.workflow.repair_folder_paths(), 2)
        self.assertEqual(self.store.get("folders", listing["id"])["path"], "/Assets/Survey/Listing")
        self.assertEqual(self.store.list("audit", filters={"entity": "folders", "action": "Repair path"})["total"], 2)
        workbook = Path(self.tmp.name) / "old-path.xlsx"
        XlsxCodec.write(workbook, svc.CASE_IMPORT_COLUMNS, [{"Scenario Name": "Imported with old path", "Folder Path": "/Survey/Listing"}])
        self.assertEqual(svc.import_test_cases_xlsx(workbook.read_bytes(), self.project["id"], self.admin_id)["created"], 1)
        imported = next(c for c in self.store.list("test_cases")["items"] if c["title"] == "Imported with old path")
        self.assertEqual(imported["folder_id"], listing["id"])

    # ------------------------------------------------------------ test case IDs
    def test_id_at_the_start_of_a_title_becomes_the_test_case_id(self):
        first = self.create_case("SUR-TC-101 – Create a survey")
        self.assertEqual(first["case_key"], "SUR-TC-101")
        again = self.create_case("SUR-TC-101 – Create a survey from a template")
        self.assertEqual(again["case_key"], "LMS-TC-0002")  # ID already taken -> generated
        plain = self.create_case("Plain title")
        self.assertEqual(plain["case_key"], "LMS-TC-0003")
        svc = self.httpd.app.service
        workbook = Path(self.tmp.name) / "ids.xlsx"
        XlsxCodec.write(workbook, svc.CASE_IMPORT_COLUMNS, [
            {"Scenario Name": "SUR-TC-102 – Delete a survey", "Folder Path": "/User Management/Login"},
            {"Scenario Name": "SUR-TC-103: Copy a survey", "Folder Path": "/User Management/Login"},
        ])
        self.assertEqual(svc.import_test_cases_xlsx(workbook.read_bytes(), self.project["id"], self.admin_id)["created"], 2)
        keys = {c["title"]: c["case_key"] for c in self.store.list("test_cases", page_size=100)["items"]}
        self.assertEqual((keys["SUR-TC-102 – Delete a survey"], keys["SUR-TC-103: Copy a survey"]), ("SUR-TC-102", "SUR-TC-103"))
        other = self.httpd.app.service.create_project({"name": "Payments", "key": "PAY", "owner_id": self.admin_id}, self.admin_id)
        other_folder = self.store.list("folders", filters={"project_id": other["id"]})["items"][0]
        status, same_id = self.admin.call("POST", "/api/test-cases", {"project_id": other["id"], "folder_id": other_folder["id"], "title": "SUR-TC-101 – Create a survey", "priority": "Low", "steps": STEP})
        self.assertEqual((status, same_id["case_key"]), (201, "SUR-TC-101"))  # unique per project

    def test_existing_cases_switch_to_title_ids_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc_store = ExcelStorageService(tmp)
            from app.service import TMExpertService
            TMExpertService(svc_store).bootstrap()
            project = svc_store.list("projects")["items"][0]
            folder = svc_store.list("folders")["items"][0]
            base = {"project_id": project["id"], "folder_id": folder["id"], "priority": "High", "status": "Draft", "version": "1"}
            rows = [
                {**base, "id": "old-copy", "title": "SUR-TC-001 – Listing", "case_key": "LMS-TC-0002", "archived": True, "created_at": "2026-01-01T00:00:00+00:00"},
                {**base, "id": "active", "title": "SUR-TC-001 – Listing", "case_key": "LMS-TC-0003", "archived": False, "created_at": "2026-02-01T00:00:00+00:00"},
                {**base, "id": "other", "title": "SUR-TC-002 – Details", "case_key": "LMS-TC-0004", "archived": False, "created_at": "2026-02-01T00:00:00+00:00"},
                {**base, "id": "no-id", "title": "No ID here", "case_key": "LMS-TC-0005", "archived": False, "created_at": "2026-02-01T00:00:00+00:00"},
            ]
            current = svc_store._read_all("test_cases")
            svc_store._commit("test_cases", current + rows)
            audit = [r for r in svc_store._read_all("audit") if not (r.get("action") == "Migrate" and "adopt_title_ids" in (r.get("new_value") or {}))]
            svc_store._commit("audit", audit)
            (Path(tmp) / "indexes" / "migrations.json").unlink(missing_ok=True)  # simulate data from before the migration
            reopened = ExcelStorageService(tmp)
            keys = {r["id"]: r["case_key"] for r in reopened.list("test_cases", page_size=100, include_archived=True)["items"]}
            self.assertEqual((keys["active"], keys["other"], keys["old-copy"], keys["no-id"]), ("SUR-TC-001", "SUR-TC-002", "LMS-TC-0002", "LMS-TC-0005"))
            self.assertEqual(reopened.list("audit", filters={"action": "Re-key"})["total"], 2)
            reopened.update("test_cases", "no-id", {"title": "SUR-TC-009 – Renamed later"})
            again = ExcelStorageService(tmp)
            self.assertEqual(again.get("test_cases", "no-id")["case_key"], "LMS-TC-0005")  # migration runs only once

    # ---------------------------------------------------------- duplicates
    def test_duplicate_titles_in_a_module_need_confirmation(self):
        first = self.create_case("Reset password via email")
        status, body = self.admin.call("POST", "/api/test-cases", {"project_id": self.project["id"], "folder_id": self.login_folder["id"], "title": "  reset PASSWORD via e-mail ", "steps": STEP, "priority": "Low"})
        self.assertEqual(status, 201)  # "e-mail" differs from "email"
        status, body = self.admin.call("POST", "/api/test-cases", {"project_id": self.project["id"], "folder_id": self.login_folder["id"], "title": "Reset Password  via email!", "steps": STEP, "priority": "Low"})
        self.assertEqual((status, body["code"], body["duplicates"][0]["case_key"]), (409, "duplicate", first["case_key"]))
        status, _ = self.admin.call("POST", "/api/test-cases", {"project_id": self.project["id"], "folder_id": self.login_folder["id"], "title": "Reset Password via email", "steps": STEP, "priority": "Low", "allow_duplicate": True})
        self.assertEqual(status, 201)
        other = self.create_case("Unique title")
        self.assertEqual(self.admin.call("PUT", f"/api/test_cases/{other['id']}", {"title": "Reset password via email"})[0], 409)

    def test_import_reports_cases_that_already_exist(self):
        svc = self.httpd.app.service
        existing = self.create_case("Existing scenario")
        workbook = Path(self.tmp.name) / "dupe.xlsx"
        XlsxCodec.write(workbook, svc.CASE_IMPORT_COLUMNS, [
            {"Scenario Name": "Existing scenario", "Folder Path": "/User Management/Login"},
            {"Scenario Name": "Brand new scenario", "Folder Path": "/User Management/Login"},
        ])
        result = svc.import_test_cases_xlsx(workbook.read_bytes(), self.project["id"], self.admin_id)
        self.assertEqual(result["created"], 0)
        self.assertTrue(result["duplicates_only"])
        self.assertIn(existing["case_key"], result["errors"][0]["error"])
        self.assertEqual(result["errors"][0]["row"], 2)
        forced = svc.import_test_cases_xlsx(workbook.read_bytes(), self.project["id"], self.admin_id, allow_duplicates=True)
        self.assertEqual(forced["created"], 2)

    # ---------------------------------------------------------------- bulk
    def test_bulk_approval_reports_skipped_cases(self):
        a = self.create_case("Bulk A"); b = self.create_case("Bulk B"); c = self.create_case("Bulk C")
        self.transition(c["id"], "Deprecated")
        status, result = self.admin.call("POST", "/api/test-cases/bulk", {"action": "approve", "ids": [a["id"], b["id"], c["id"]]})
        self.assertEqual((status, result["updated"]), (200, 2))
        self.assertEqual([s["case_key"] for s in result["skipped"]], [c["case_key"]])
        self.assertEqual(len(self.store.list("approvals", filters={"decision": "Approved"})["items"]), 3)  # 2 + bootstrap

    def test_bulk_metadata_changes_do_not_create_versions(self):
        a = self.create_case("Meta A", tags=["smoke"]); b = self.create_case("Meta B")
        status, module = self.admin.call("POST", "/api/folders", {"project_id": self.project["id"], "parent_id": "", "name": "Moved"})
        ids = [a["id"], b["id"]]
        self.assertEqual(self.admin.call("POST", "/api/test-cases/bulk", {"action": "move", "ids": ids, "folder_id": module["id"]})[1]["updated"], 2)
        self.admin.call("POST", "/api/test-cases/bulk", {"action": "set_priority", "ids": ids, "priority": "critical"})
        self.admin.call("POST", "/api/test-cases/bulk", {"action": "add_tags", "ids": ids, "tags": "regression, Smoke"})
        self.admin.call("POST", "/api/test-cases/bulk", {"action": "remove_tags", "ids": ids, "tags": ["regression"]})
        after = self.store.get("test_cases", a["id"])
        self.assertEqual((after["folder_id"], after["priority"], after["tags"], after["version"]), (module["id"], "Critical", ["smoke"], "1"))
        self.assertEqual(self.store.get("test_cases", b["id"])["tags"], ["Smoke"])
        self.assertEqual(self.admin.call("POST", "/api/test-cases/bulk", {"action": "set_priority", "ids": ids, "priority": "Urgent"})[0], 400)
        self.assertEqual(self.admin.call("POST", "/api/test-cases/bulk", {"action": "explode", "ids": ids})[0], 400)
        self.assertEqual(self.admin.call("POST", "/api/test-cases/bulk", {"action": "archive", "ids": ids})[1]["updated"], 2)

    # ------------------------------------------------------------- history
    def test_version_history_shows_what_changed(self):
        case = self.create_case("History case", objective="Old objective")
        self.admin.call("PUT", f"/api/test_cases/{case['id']}", {"objective": "New objective", "priority": "High", "steps": STEP + [{"action": "Log out", "expected_result": "Signed out"}], "version_comment": "Added logout"})
        details = self.admin.call("GET", f"/api/test-cases/{case['id']}/details")[1]
        self.assertEqual([v["version"] for v in details["versions"]], ["2", "1"])
        self.assertEqual(details["versions"][0]["comment"], "Added logout")
        self.assertEqual(details["created_by_name"], "Avery Chen")
        version = self.admin.call("GET", f"/api/test-cases/{case['id']}/versions/2")[1]
        changed = {c["field"]: (c["before"], c["after"]) for c in version["changes"]}
        self.assertEqual(changed, {"Objective": ("Old objective", "New objective"), "Priority": ("Medium", "High")})
        self.assertTrue(version["steps_changed"])
        self.assertEqual(len(version["steps"]), 2)
        first = self.admin.call("GET", f"/api/test-cases/{case['id']}/versions/1")[1]
        self.assertEqual((first["changes"], len(first["steps"])), ([], 1))

    def test_approval_queue_lists_pending_work(self):
        case = self.create_case("Queue case"); self.transition(case["id"], "In Review")
        module = self.admin.call("POST", "/api/folders", {"project_id": self.project["id"], "parent_id": "", "name": "Queue module"})[1]
        queue = self.admin.call("GET", f"/api/approvals/queue?project_id={self.project['id']}")[1]
        self.assertEqual([c["case_key"] for c in queue["cases"]], [case["case_key"]])
        self.assertEqual(queue["cases"][0]["submitted_by"], "Avery Chen")
        self.assertIn(module["id"], [f["id"] for f in queue["folders"]])
        self.assertEqual(queue["recent"][0]["decision"], "Submitted")

    def test_authorship_is_recorded(self):
        case = self.create_case("Authored")
        self.assertEqual((case["created_by"], case["updated_by"]), (self.admin_id, self.admin_id))
        self.transition(case["id"], "Approved")
        team = self.team_client()
        team_id = team.call("GET", "/api/auth/session")[1]["user"]["id"]
        self.assertNotEqual(team_id, self.admin_id)
        self.assertEqual(self.store.get("test_cases", case["id"])["created_by"], self.admin_id)


if __name__ == "__main__": unittest.main()
