"""End-to-end HTTP tests for authentication, permissions and audit integrity."""
import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import server
from app.storage import ExcelStorageService, XlsxCodec, WORKBOOKS, LEGACY_CHANGE_ME_HASH

ADMIN_EMAIL = "avery@tmexpert.local"
ADMIN_PASSWORD = "Admin-Pass-2026"


class Client:
    def __init__(self, base: str):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method: str, path: str, body=None, headers=None, raw: bytes | None = None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if body is not None: request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items(): request.add_header(key, value)
        try:
            with self.opener.open(request) as response:
                payload = response.read()
                return response.status, (json.loads(payload) if payload and response.headers.get_content_type() == "application/json" else payload)
        except urllib.error.HTTPError as error:
            payload = error.read()
            return error.code, (json.loads(payload) if payload else {})

    def login(self, email, password):
        return self.call("POST", "/api/auth/login", {"email": email, "password": password})


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = server.make_server("127.0.0.1", 0, self.tmp.name, approvals=True)  # these tests cover the approval workflow
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.store = self.httpd.app.store
        self.admin = Client(self.base)
        status, session = self.admin.login(ADMIN_EMAIL, "change-me")
        self.assertEqual(status, 200)
        self.assertTrue(session["must_change_password"])
        status, _ = self.admin.call("POST", "/api/auth/change-password", {"current_password": "change-me", "new_password": ADMIN_PASSWORD})
        self.assertEqual(status, 200)
        self.project = self.store.list("projects")["items"][0]

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join(5); self.tmp.cleanup()

    def make_team_member(self, email="tara@tmexpert.local"):
        status, user = self.admin.call("POST", "/api/users", {"name": "Tara Q", "email": email, "role": "Team Member", "password": "Temp-Pass-1"})
        self.assertEqual(status, 201, user)
        team = Client(self.base)
        self.assertEqual(team.login(email, "Temp-Pass-1")[0], 200)
        self.assertEqual(team.call("POST", "/api/auth/change-password", {"current_password": "Temp-Pass-1", "new_password": "Team-Pass-22"})[0], 200)
        return team, user

    def folder(self):
        return self.store.list("folders", filters={"project_id": self.project["id"]})["items"][0]

    # ------------------------------------------------------------- authentication
    def test_api_requires_sign_in_but_health_and_static_do_not(self):
        anonymous = Client(self.base)
        self.assertEqual(anonymous.call("GET", "/api/test_cases")[0], 401)
        self.assertEqual(anonymous.call("GET", "/api/health")[0], 200)
        self.assertEqual(anonymous.call("GET", "/")[0], 200)
        self.assertEqual(anonymous.call("POST", "/api/test-cases", {"title": "x"})[0], 401)

    def test_wrong_password_is_rejected_and_repeated_failures_lock_the_account(self):
        anonymous = Client(self.base)
        for _ in range(5):
            self.assertEqual(anonymous.login(ADMIN_EMAIL, "wrong-password")[0], 401)
        status, body = anonymous.login(ADMIN_EMAIL, ADMIN_PASSWORD)
        self.assertEqual(status, 401)
        self.assertIn("Too many", body["error"])

    def test_temporary_password_blocks_everything_until_changed(self):
        fresh = Client(self.base)
        self.admin.call("POST", "/api/users", {"name": "New Person", "email": "new@tmexpert.local", "role": "Team Member", "password": "Temp-Pass-1"})
        fresh.login("new@tmexpert.local", "Temp-Pass-1")
        status, body = fresh.call("GET", "/api/dashboard")
        self.assertEqual((status, body["code"]), (403, "password_change_required"))
        self.assertEqual(fresh.call("POST", "/api/auth/change-password", {"current_password": "Temp-Pass-1", "new_password": "short"})[0], 400)

    def test_passwords_are_salted_and_never_returned_or_audited(self):
        users = self.admin.call("GET", "/api/users")[1]["items"]
        self.assertTrue(users)
        self.assertTrue(all("password_hash" not in user for user in users))
        stored = self.store.list("users")["items"][0]["password_hash"]
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        audit_text = json.dumps(self.store.list("audit", page_size=100000)["items"])
        self.assertNotIn(stored, audit_text)
        self.assertNotIn(LEGACY_CHANGE_ME_HASH, audit_text)

    def test_legacy_sha256_password_is_accepted_once_and_upgraded(self):
        user = self.store.create("users", {"name": "Legacy", "email": "legacy@tmexpert.local", "role": "System Administrator", "status": "Active", "password_hash": LEGACY_CHANGE_ME_HASH})
        legacy = Client(self.base)
        status, session = legacy.login("legacy@tmexpert.local", "change-me")
        self.assertEqual(status, 200)
        self.assertEqual(session["user"]["role"], "Admin")
        self.assertTrue(self.store.get("users", user["id"])["password_hash"].startswith("pbkdf2_sha256$"))

    def test_deactivated_member_is_signed_out_and_cannot_sign_in(self):
        team, user = self.make_team_member()
        self.assertEqual(self.admin.call("PUT", f"/api/users/{user['id']}", {"status": "Inactive"})[0], 200)
        self.assertEqual(team.call("GET", "/api/dashboard")[0], 401)
        self.assertEqual(Client(self.base).login("tara@tmexpert.local", "Team-Pass-22")[0], 401)

    def test_last_active_admin_cannot_be_demoted_or_deactivated(self):
        admin_id = self.admin.call("GET", "/api/auth/session")[1]["user"]["id"]
        self.assertEqual(self.admin.call("PUT", f"/api/users/{admin_id}", {"status": "Inactive"})[0], 400)
        self.assertEqual(self.admin.call("PUT", f"/api/users/{admin_id}", {"role": "Team Member"})[0], 400)

    # ---------------------------------------------------------------- permissions
    def test_team_member_sees_only_approved_cases_and_cannot_change_the_repository(self):
        team, _ = self.make_team_member()
        folder = self.folder()
        status, draft = self.admin.call("POST", "/api/test-cases", {"project_id": self.project["id"], "folder_id": folder["id"], "title": "Draft only", "priority": "Low", "steps": [{"action": "a", "expected_result": "b"}]})
        self.assertEqual(status, 201)
        listed = team.call("GET", f"/api/test_cases?project_id={self.project['id']}")[1]["items"]
        self.assertTrue(listed)
        self.assertTrue(all(case["status"] == "Approved" for case in listed))
        self.assertEqual(team.call("GET", f"/api/test_cases/{draft['id']}")[0], 404)
        self.assertEqual(team.call("GET", f"/api/test_steps?test_case_id={draft['id']}")[0], 403)
        self.assertEqual(team.call("POST", "/api/test-cases", {"project_id": self.project["id"], "folder_id": folder["id"], "title": "x", "steps": [{"action": "a", "expected_result": "b"}]})[0], 403)
        self.assertEqual(team.call("PUT", f"/api/test-cases/{draft['id']}/approval-status", {"status": "Approved"})[0], 403)
        self.assertEqual(team.call("POST", f"/api/test-cases/{draft['id']}/approval", {"decision": "Approved"})[0], 403)
        self.assertEqual(team.call("POST", "/api/test-cases/bulk-delete", {"ids": [draft["id"]]})[0], 403)
        self.assertEqual(team.call("POST", "/api/users", {"name": "x", "email": "x@x.x", "role": "Admin", "password": "Password-1"})[0], 403)
        self.assertEqual(team.call("GET", "/api/audit")[0], 403)
        self.assertEqual(team.call("GET", f"/api/export/test-cases.xlsx?project_id={self.project['id']}")[0], 403)
        self.assertEqual(self.store.get("test_cases", draft["id"])["status"], "Draft")

    def test_team_member_can_record_execution_status(self):
        team, user = self.make_team_member()
        case = self.store.list("test_cases", filters={"status": "Approved"})["items"][0]
        plan = self.store.create("test_plans", {"project_id": self.project["id"], "name": "Plan"})
        suite = self.store.create("test_suites", {"project_id": self.project["id"], "plan_id": plan["id"], "name": "Suite"})
        cycle = self.store.create("test_cycles", {"project_id": self.project["id"], "suite_id": suite["id"], "name": "Cycle"})
        status, run = self.admin.call("POST", "/api/test_runs", {"project_id": self.project["id"], "cycle_id": cycle["id"], "test_case_id": case["id"]})
        self.assertEqual(status, 201)
        self.assertEqual(team.call("POST", "/api/test_runs", {"project_id": self.project["id"], "cycle_id": cycle["id"], "test_case_id": case["id"]})[0], 403)
        self.assertEqual(team.call("PUT", f"/api/test-runs/{run['id']}/status", {"status": "Passed"})[0], 403)  # not assigned yet
        self.assertEqual(self.admin.call("POST", f"/api/cycles/{cycle['id']}/assign", {"run_ids": [run["id"]], "assignee_id": user["id"]})[0], 200)
        status, updated = team.call("PUT", f"/api/test-runs/{run['id']}/status", {"status": "Passed"})
        self.assertEqual((status, updated["status"]), (200, "Passed"))
        audit = self.store.list("audit", filters={"entity": "test_runs", "record_id": run["id"], "action": "Update"})["items"]
        self.assertEqual(audit[-1]["actor_id"], user["id"])

    def test_history_workbooks_cannot_be_written_through_the_api(self):
        audit_id = self.store.list("audit")["items"][0]["id"]
        self.assertEqual(self.admin.call("POST", "/api/audit", {"actor_id": "someone", "action": "Forged"})[0], 404)
        self.assertEqual(self.admin.call("PUT", f"/api/audit/{audit_id}", {"action": "Changed"})[0], 404)
        self.assertEqual(self.admin.call("DELETE", f"/api/audit/{audit_id}")[0], 404)
        self.assertEqual(self.admin.call("POST", "/api/approvals", {"decision": "Approved"})[0], 404)
        self.assertEqual(self.admin.call("POST", "/api/users/x", {})[0], 404)
        with self.assertRaises(ValueError): self.store.update("audit", audit_id, {"action": "Changed"})
        with self.assertRaises(ValueError): self.store.create("audit", {"action": "Forged"})

    def test_generic_update_cannot_bypass_the_approval_workflow(self):
        case = self.store.list("test_cases")["items"][0]
        self.store.update("test_cases", case["id"], {"status": "Draft"})
        status, _ = self.admin.call("PUT", f"/api/test_cases/{case['id']}", {"status": "Approved"})
        self.assertEqual(status, 403)
        self.assertEqual(self.store.get("test_cases", case["id"])["status"], "Draft")

    def test_approving_from_the_repository_records_the_reviewer(self):
        folder = self.folder()
        case = self.admin.call("POST", "/api/test-cases", {"project_id": self.project["id"], "folder_id": folder["id"], "title": "Needs approval", "priority": "High", "steps": [{"action": "a", "expected_result": "b"}]})[1]
        admin_id = self.admin.call("GET", "/api/auth/session")[1]["user"]["id"]
        status, updated = self.admin.call("PUT", f"/api/test-cases/{case['id']}/approval-status", {"status": "Approved"})
        self.assertEqual((status, updated["status"]), (200, "Approved"))
        approvals = self.store.list("approvals", filters={"test_case_id": case["id"]})["items"]
        self.assertEqual([(a["decision"], a["reviewer_id"]) for a in approvals], [("Approved", admin_id)])

    def test_audit_records_the_signed_in_user_not_a_spoofable_header(self):
        folder = self.folder()
        admin_id = self.admin.call("GET", "/api/auth/session")[1]["user"]["id"]
        status, case = self.admin.call("POST", "/api/test-cases", {"project_id": self.project["id"], "folder_id": folder["id"], "title": "Audited", "priority": "Low", "steps": [{"action": "a", "expected_result": "b"}]}, headers={"X-TMExpert-Actor": "spoofed"})
        self.assertEqual(status, 201)
        entry = self.store.list("audit", filters={"entity": "test_cases", "record_id": case["id"], "action": "Create"})["items"][0]
        self.assertEqual(entry["actor_id"], admin_id)

    def test_cross_site_and_non_json_writes_are_rejected(self):
        folder = self.folder()
        payload = {"project_id": self.project["id"], "parent_id": folder["id"], "name": "Sneaky"}
        self.assertEqual(self.admin.call("POST", "/api/folders", payload, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.admin.call("POST", "/api/folders", raw=json.dumps(payload).encode(), headers={"Content-Type": "text/plain"})[0], 415)
        self.assertFalse(any(f["name"] == "Sneaky" for f in self.store.list("folders")["items"]))
        self.assertEqual(self.admin.call("POST", "/api/folders", payload, headers={"Origin": self.base})[0], 201)

    def test_bad_input_returns_json_errors_instead_of_dropping_the_connection(self):
        case = self.store.list("test_cases")["items"][0]
        status, body = self.admin.call("POST", f"/api/test-cases/{case['id']}/approval", {})
        self.assertEqual(status, 400)
        self.assertIn("decision", body["error"])
        self.assertEqual(self.admin.call("POST", "/api/test-cases", raw=b"{not json", headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.admin.call("GET", "/api/test_cases/does-not-exist")[0], 200)

    # --------------------------------------------------------------- data rules
    def test_test_cases_get_sequential_readable_ids_that_cannot_be_changed(self):
        folder = self.folder()
        first = self.admin.call("POST", "/api/test-cases", {"project_id": self.project["id"], "folder_id": folder["id"], "title": "One", "priority": "Low", "steps": [{"action": "a", "expected_result": "b"}]})[1]
        second = self.admin.call("POST", "/api/test-cases", {"project_id": self.project["id"], "folder_id": folder["id"], "title": "Two", "priority": "Low", "steps": [{"action": "a", "expected_result": "b"}]})[1]
        self.assertEqual((first["case_key"], second["case_key"]), ("LMS-TC-0002", "LMS-TC-0003"))
        updated = self.admin.call("PUT", f"/api/test_cases/{first['id']}", {"title": "One renamed", "case_key": "LMS-TC-9999"})[1]
        self.assertEqual(updated["case_key"], "LMS-TC-0002")
        with self.assertRaisesRegex(ValueError, "already in use"):
            self.store.create("test_cases", {"project_id": self.project["id"], "folder_id": folder["id"], "title": "Dup", "priority": "Low", "status": "Draft", "case_key": "lms-tc-0003"})

    def test_lists_page_beyond_200_records(self):
        case = self.store.list("test_cases")["items"][0]
        self.store._commit("test_cases", [{**case, "id": f"bulk-{i}", "case_key": f"LMS-TC-{i + 10:04d}"} for i in range(250)])
        first = self.admin.call("GET", "/api/test_cases?page=1&page_size=500")[1]
        second = self.admin.call("GET", "/api/test_cases?page=2&page_size=200")[1]
        self.assertEqual((first["total"], len(first["items"]), len(second["items"])), (250, 200, 50))


class SchemaUpgradeTests(unittest.TestCase):
    def test_users_workbook_without_new_column_is_upgraded_and_default_admin_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExcelStorageService(tmp)
            path = store.path_for("users")
            old_headers = WORKBOOKS["users"][1][:-1]
            XlsxCodec.write(path, old_headers, [
                {"id": "u1", "name": "Default", "email": "a@x.local", "role": "System Administrator", "password_hash": LEGACY_CHANGE_ME_HASH, "status": "Active", "archived": "False"},
                {"id": "u2", "name": "Custom", "email": "b@x.local", "role": "System Administrator", "password_hash": "0" * 64, "status": "Active", "archived": "False"},
            ])
            upgraded = ExcelStorageService(tmp)
            self.assertIs(upgraded.get("users", "u1")["must_change_password"], True)
            self.assertIs(upgraded.get("users", "u2")["must_change_password"], False)
            self.assertEqual(upgraded.get("users", "u2")["name"], "Custom")
            self.assertTrue(list((Path(tmp) / "backups" / "users").glob("*.xlsx")))

    def test_unknown_layout_is_reported_and_left_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExcelStorageService(tmp)
            path = store.path_for("defects")
            XlsxCodec.write(path, ("id", "summary"), [{"id": "d1", "summary": "Keep me"}])
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "Unsupported schema in Defects.xlsx"):
                ExcelStorageService(tmp)
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__": unittest.main()
