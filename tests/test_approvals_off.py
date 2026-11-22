"""With approvals switched off (the default), draft test cases and modules are usable by everyone."""
import tempfile
import threading
import unittest

import server
from test_api import Client, ADMIN_EMAIL, ADMIN_PASSWORD

STEP = [{"action": "Do it", "expected_result": "Done"}]


class ApprovalsOffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = server.make_server("127.0.0.1", 0, self.tmp.name)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.store = self.httpd.app.store
        self.admin = Client(self.base)
        self.admin.login(ADMIN_EMAIL, "change-me")
        self.admin.call("POST", "/api/auth/change-password", {"current_password": "change-me", "new_password": ADMIN_PASSWORD})
        self.pid = self.store.list("projects")["items"][0]["id"]

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join(5); self.tmp.cleanup()

    def test_draft_cases_are_visible_eligible_and_stay_unchanged_on_edit(self):
        self.assertFalse(self.httpd.app.service.workflow.approvals_enabled)
        folder = self.admin.call("POST", "/api/folders", {"project_id": self.pid, "parent_id": "", "name": "Checkout"})[1]
        self.assertEqual(folder["approval_status"], "Draft")
        case = self.admin.call("POST", "/api/test-cases", {"project_id": self.pid, "folder_id": folder["id"], "title": "Pay by card", "priority": "High", "steps": STEP})[1]
        self.assertEqual(case["status"], "Draft")

        # Draft case in a draft module can go straight into a cycle.
        candidates = self.admin.call("GET", f"/api/cycles/candidates?project_id={self.pid}")[1]
        self.assertIn(case["id"], {c["id"] for c in candidates["cases"]})
        status, result = self.admin.call("POST", "/api/cycles", {"project_id": self.pid, "name": "Smoke", "testing_type": "Smoke", "module_ids": [folder["id"]]})
        self.assertEqual(status, 201, result)
        runs = self.store.list("test_runs", filters={"cycle_id": result["cycle"]["id"]})["items"]
        self.assertEqual([r["test_case_id"] for r in runs], [case["id"]])

        # Team members see draft cases and modules.
        self.admin.call("POST", "/api/users", {"name": "Arun", "email": "arun@x.local", "role": "Team Member", "password": "Temp-Pass-1"})
        arun = Client(self.base); arun.login("arun@x.local", "Temp-Pass-1")
        arun.call("POST", "/api/auth/change-password", {"current_password": "Temp-Pass-1", "new_password": "Team-Pass-22"})
        self.assertIn(case["id"], {c["id"] for c in arun.call("GET", f"/api/test_cases?project_id={self.pid}&page_size=500")[1]["items"]})
        self.assertIn(folder["id"], {f["id"] for f in arun.call("GET", f"/api/folders?project_id={self.pid}&page_size=500")[1]["items"]})
        self.assertEqual(arun.call("GET", f"/api/test-cases/{case['id']}/details")[0], 200)

        # A previously approved case keeps its status when edited (no re-approval).
        self.store.update("test_cases", case["id"], {"status": "Approved"})
        status, edited = self.admin.call("PUT", f"/api/test_cases/{case['id']}", {"title": "Pay by debit card", "steps": STEP})
        self.assertEqual(status, 200, edited)
        self.assertEqual(edited["status"], "Approved")
        self.assertEqual(str(edited["version"]), "2")

    def test_deprecated_and_archived_cases_stay_out_of_cycles(self):
        folder = self.admin.call("POST", "/api/folders", {"project_id": self.pid, "parent_id": "", "name": "Old"})[1]
        old = self.admin.call("POST", "/api/test-cases", {"project_id": self.pid, "folder_id": folder["id"], "title": "Legacy flow", "priority": "Low", "steps": STEP})[1]
        self.store.update("test_cases", old["id"], {"status": "Deprecated"})
        candidates = self.admin.call("GET", f"/api/cycles/candidates?project_id={self.pid}")[1]
        self.assertNotIn(old["id"], {c["id"] for c in candidates["cases"]})
        self.assertEqual(candidates["excluded_counts"].get("Deprecated"), 1)


if __name__ == "__main__":
    unittest.main()
