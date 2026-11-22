"""Phase 6: execution workspace, results history, defects and evidence files."""
import base64
import tempfile
import threading
import unittest
import urllib.request

import server
from test_api import Client, ADMIN_EMAIL, ADMIN_PASSWORD

STEPS = [{"action": "Open page", "expected_result": "Page opens"}, {"action": "Submit", "expected_result": "Saved"}]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = server.make_server("127.0.0.1", 0, self.tmp.name, approvals=True)  # these tests cover the approval workflow
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.store = self.httpd.app.store
        self.admin = Client(self.base)
        self.admin.login(ADMIN_EMAIL, "change-me")
        self.admin.call("POST", "/api/auth/change-password", {"current_password": "change-me", "new_password": ADMIN_PASSWORD})
        self.project = self.store.list("projects")["items"][0]
        folder = self.admin.call("POST", "/api/folders", {"project_id": self.project["id"], "parent_id": "", "name": "Checkout"})[1]
        self.admin.call("POST", f"/api/folders/{folder['id']}/transition", {"status": "Approved"})
        self.cases = []
        for title in ("CHK-1 – Pay", "CHK-2 – Refund", "CHK-3 – Cancel"):
            case = self.admin.call("POST", "/api/test-cases", {"project_id": self.project["id"], "folder_id": folder["id"], "title": title, "priority": "High", "steps": STEPS})[1]
            self.cases.append(self.admin.call("POST", f"/api/test-cases/{case['id']}/transition", {"status": "Approved"})[1])
        self.cycle = self.admin.call("POST", "/api/cycles", {"project_id": self.project["id"], "name": "Smoke 1", "testing_type": "Smoke", "module_ids": [folder["id"]]})[1]["cycle"]
        self.runs = {r["case_key"]: r for r in self.store.list("test_runs", filters={"cycle_id": self.cycle["id"]})["items"]}
        self.arun, self.arun_client = self.member("Arun")
        self.kumar, self.kumar_client = self.member("Kumar")
        self.admin.call("POST", f"/api/cycles/{self.cycle['id']}/module-assignment", {"module_path": "/Checkout", "assignee_id": self.arun["id"]})
        self.admin.call("POST", f"/api/cycles/{self.cycle['id']}/assign", {"run_ids": [self.runs["CHK-3"]["id"]], "assignee_id": self.kumar["id"]})

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join(5); self.tmp.cleanup()

    def member(self, name):
        email = f"{name.lower()}@x.local"
        user = self.admin.call("POST", "/api/users", {"name": name, "email": email, "role": "Team Member", "password": "Temp-Pass-1"})[1]
        client = Client(self.base); client.login(email, "Temp-Pass-1")
        client.call("POST", "/api/auth/change-password", {"current_password": "Temp-Pass-1", "new_password": "Team-Pass-22"})
        return user, client

    def detail(self, client, key):
        return client.call("GET", f"/api/test-runs/{self.runs[key]['id']}/execution")[1]

    def upload(self, client, key, name="screen.png", data=PNG, owner_type="test_run"):
        owner_id = self.runs[key]["id"] if owner_type == "test_run" else key
        return client.call("POST", "/api/attachments", {"owner_type": owner_type, "owner_id": owner_id, "filename": name, "data": b64(data)})

    # --------------------------------------------------------------- results
    def test_full_execution_records_result_and_history(self):
        run_id = self.runs["CHK-1"]["id"]
        steps = self.detail(self.arun_client, "CHK-1")["steps"]
        status, evidence = self.upload(self.arun_client, "CHK-1")
        self.assertEqual(status, 201, evidence)
        status, result = self.arun_client.call("POST", f"/api/test-runs/{run_id}/execute", {
            "status": "Failed", "actual_result": "Payment page shows HTTP 500", "notes": "Only with saved cards",
            "defect_ids": "PAY-412, https://jira.example.com/browse/PAY-413", "attachment_ids": [evidence["id"]], "base_attempt": 0,
            "step_results": [{"step_id": steps[0]["step"]["id"], "status": "Passed", "actual_result": "Opened"},
                             {"step_id": steps[1]["step"]["id"], "status": "Failed", "actual_result": "Error 500"}]})
        self.assertEqual(status, 200, result)
        run = self.store.get("test_runs", run_id)
        self.assertEqual((run["status"], run["actual_result"], run["execution_notes"], run["defect_ids"], run["executed_by"], run["attempt_count"]),
                         ("Failed", "Payment page shows HTTP 500", "Only with saved cards", ["PAY-412", "https://jira.example.com/browse/PAY-413"], self.arun["id"], "1"))
        self.assertTrue(run["executed_at"] and run["completed_at"])
        self.assertEqual(self.store.get("test_cases", self.runs["CHK-1"]["test_case_id"])["last_status"], "Failed")
        self.assertEqual(self.store.get("test_cycles", self.cycle["id"])["status"], "In Progress")
        history = result["history"]
        self.assertEqual((history[0]["attempt"], history[0]["status"], history[0]["previous_status"], history[0]["executed_by_name"], history[0]["source"]), ("1", "Failed", "Not Run", "Arun", "execution"))
        self.assertEqual([s["status"] for s in history[0]["step_results"]], ["Passed", "Failed"])
        self.assertEqual(history[0]["attachments"][0]["filename"], "screen.png")

    def test_re_execution_keeps_every_earlier_result(self):
        run_id = self.runs["CHK-1"]["id"]
        self.arun_client.call("POST", f"/api/test-runs/{run_id}/execute", {"status": "Failed", "actual_result": "Broken", "defect_ids": ["PAY-1"]})
        self.arun_client.call("POST", f"/api/test-runs/{run_id}/execute", {"status": "Passed", "actual_result": "Fixed in build 2.5.1"})
        history = self.arun_client.call("GET", f"/api/test-runs/{run_id}/history")[1]["items"]
        self.assertEqual([(h["attempt"], h["status"], h["actual_result"], h["defect_ids"]) for h in history],
                         [("2", "Passed", "Fixed in build 2.5.1", ["PAY-1"]), ("1", "Failed", "Broken", ["PAY-1"])])  # defect link kept when not re-sent
        self.assertEqual(history[0]["previous_status"], "Failed")
        with self.assertRaises(ValueError): self.store.update("execution_history", history[1]["id"], {"status": "Passed"})
        self.arun_client.call("POST", f"/api/test-runs/{run_id}/execute", {"status": "Failed", "notes": "Regressed again"})
        self.arun_client.call("POST", f"/api/test-runs/{run_id}/execute", {"status": "Blocked", "actual_result": "Env down"})
        run = self.store.get("test_runs", run_id)
        self.assertEqual((run["status"], run["actual_result"], run["execution_notes"]), ("Blocked", "Env down", "Regressed again"))  # unsent fields kept

    def test_quick_status_is_logged_and_stale_saves_are_refused(self):
        run_id = self.runs["CHK-2"]["id"]
        self.assertEqual(self.arun_client.call("PUT", f"/api/test-runs/{run_id}/status", {"status": "Passed", "base_attempt": 0})[0], 200)
        status, body = self.arun_client.call("PUT", f"/api/test-runs/{run_id}/status", {"status": "Failed", "base_attempt": 0})
        self.assertEqual((status, body["code"]), (409, "stale_execution"))
        status, body = self.arun_client.call("POST", f"/api/test-runs/{run_id}/execute", {"status": "Blocked", "base_attempt": 0})
        self.assertEqual(status, 409)
        history = self.store.list("execution_history", filters={"test_run_id": run_id})["items"]
        self.assertEqual([(h["status"], h["source"]) for h in history], [("Passed", "quick")])

    def test_step_results_endpoint_also_keeps_history(self):
        run_id = self.runs["CHK-2"]["id"]
        steps = self.detail(self.arun_client, "CHK-2")["steps"]
        self.arun_client.call("PUT", f"/api/test-runs/{run_id}/results", {"results": [{"step_id": steps[0]["step"]["id"], "status": "Passed", "actual_result": "ok"}]})
        self.arun_client.call("PUT", f"/api/test-runs/{run_id}/results", {"results": [{"step_id": steps[0]["step"]["id"], "status": "Failed", "actual_result": "now broken"}]})
        history = self.arun_client.call("GET", f"/api/test-runs/{run_id}/history")[1]["items"]
        self.assertEqual([(h["status"], h["step_results"][0]["actual"]) for h in history], [("Failed", "now broken"), ("In Progress", "ok")])

    def test_invalid_input_and_closed_cycles_are_refused(self):
        run_id = self.runs["CHK-1"]["id"]
        for payload, message in [({"status": "Done"}, "Choose an execution status"), ({"status": "Failed", "defect_ids": "bad<id>"}, "not a valid defect"),
                                 ({"status": "Failed", "defect_ids": [f"D-{i}" for i in range(21)]}, "at most 20"), ({"status": "Failed", "actual_result": "x" * 4001}, "4000 characters")]:
            status, body = self.arun_client.call("POST", f"/api/test-runs/{run_id}/execute", payload)
            self.assertEqual(status, 400, payload); self.assertIn(message, body["error"])
        other_evidence = self.upload(self.kumar_client, "CHK-3")[1]
        status, body = self.arun_client.call("POST", f"/api/test-runs/{run_id}/execute", {"status": "Passed", "attachment_ids": [other_evidence["id"]]})
        self.assertEqual(status, 400); self.assertIn("uploaded to this test case", body["error"])
        self.admin.call("PUT", f"/api/test_cycles/{self.cycle['id']}", {"status": "Completed"})
        status, body = self.arun_client.call("POST", f"/api/test-runs/{run_id}/execute", {"status": "Passed"})
        self.assertEqual(status, 400); self.assertIn("completed", body["error"])
        self.assertEqual(self.store.get("test_runs", run_id)["status"], "Not Run")

    def test_only_the_owner_or_an_admin_records_results_and_evidence(self):
        kumar_run = self.runs["CHK-3"]["id"]
        self.assertEqual(self.arun_client.call("POST", f"/api/test-runs/{kumar_run}/execute", {"status": "Passed"})[0], 403)
        self.assertEqual(self.upload(self.arun_client, "CHK-3")[0], 403)
        self.assertEqual(self.kumar_client.call("POST", f"/api/test-runs/{kumar_run}/execute", {"status": "Passed"})[0], 200)
        self.assertEqual(self.admin.call("POST", f"/api/test-runs/{kumar_run}/execute", {"status": "Failed", "actual_result": "Rechecked"})[0], 200)
        self.assertEqual(self.arun_client.call("GET", f"/api/test-runs/{kumar_run}/history")[0], 200)  # everyone can read results

    # ----------------------------------------------------------------- files
    def test_only_safe_matching_files_are_accepted(self):
        for name, data, message in [("page.html", b"<html></html>", "not allowed"), ("logo.svg", b"<svg/>", "not allowed"),
                                    ("fake.png", b"<html><script>alert(1)</script>", "does not match"), ("notes.txt", b"<!DOCTYPE html><html>", "does not match"),
                                    ("empty.png", b"", "empty"), ("big.png", PNG + b"0" * (10 * 1024 * 1024), "10 MB")]:
            status, body = self.upload(self.arun_client, "CHK-1", name, data)
            self.assertEqual(status, 400, name); self.assertIn(message, body["error"])
        status, record = self.upload(self.arun_client, "CHK-1", "../../outside/evil.png")
        self.assertEqual((status, record["filename"]), (201, "evil.png"))
        stored = self.store.get("attachments", record["id"])["stored_name"]
        self.assertTrue((self.store.root / "attachments" / stored).is_file())
        self.assertFalse((self.store.root.parent / "outside").exists())

    def test_files_are_served_safely_and_checked_for_tampering(self):
        record = self.upload(self.arun_client, "CHK-1", "log.txt", b"step 1 ok\nstep 2 failed")[1]
        request = urllib.request.Request(f"{self.base}/api/attachments/{record['id']}/file")
        with self.kumar_client.opener.open(request) as response:
            body, headers = response.read(), response.headers
        self.assertEqual(body, b"step 1 ok\nstep 2 failed")
        self.assertEqual(headers.get_content_type(), "text/plain")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(Client(self.base).call("GET", f"/api/attachments/{record['id']}/file")[0], 401)
        stored = self.store.root / "attachments" / self.store.get("attachments", record["id"])["stored_name"]
        stored.write_bytes(b"tampered")
        self.assertEqual(self.kumar_client.call("GET", f"/api/attachments/{record['id']}/file")[0], 400)

    def test_saved_evidence_cannot_be_removed_but_unsaved_uploads_can(self):
        run_id = self.runs["CHK-1"]["id"]
        saved = self.upload(self.arun_client, "CHK-1")[1]
        spare = self.upload(self.arun_client, "CHK-1", "spare.png")[1]
        self.arun_client.call("POST", f"/api/test-runs/{run_id}/execute", {"status": "Passed", "attachment_ids": [saved["id"]]})
        status, body = self.arun_client.call("DELETE", f"/api/attachments/{saved['id']}")
        self.assertEqual(status, 400); self.assertIn("part of a saved result", body["error"])
        self.assertEqual(self.kumar_client.call("DELETE", f"/api/attachments/{spare['id']}")[0], 403)
        self.assertEqual(self.arun_client.call("DELETE", f"/api/attachments/{spare['id']}")[0], 200)
        self.assertEqual([a["filename"] for a in self.detail(self.arun_client, "CHK-1")["attachments"]], ["screen.png"])

    def test_test_case_attachments_follow_repository_permissions(self):
        case_id = self.cases[0]["id"]
        self.assertEqual(self.upload(self.arun_client, case_id, owner_type="test_case")[0], 403)
        status, record = self.upload(self.admin, case_id, "spec.pdf", b"%PDF-1.4 test", owner_type="test_case")
        self.assertEqual(status, 201)
        listed = self.arun_client.call("GET", f"/api/attachments?owner_type=test_case&owner_id={case_id}")[1]["items"]
        self.assertEqual([a["filename"] for a in listed], ["spec.pdf"])
        self.admin.call("POST", f"/api/test-cases/{case_id}/transition", {"status": "Draft"})
        self.assertEqual(self.arun_client.call("GET", f"/api/attachments/{record['id']}/file")[0], 404)

    # ------------------------------------------------------------- workspace
    def test_workspace_shows_my_work_and_admins_can_see_everyone(self):
        mine = self.arun_client.call("GET", f"/api/workspace?project_id={self.project['id']}&assignee=__all")[1]
        self.assertEqual(sorted(r["case_key"] for r in mine["runs"]), ["CHK-1", "CHK-2"])  # team members only ever see their own
        self.arun_client.call("PUT", f"/api/test-runs/{self.runs['CHK-1']['id']}/status", {"status": "Passed"})
        mine = self.arun_client.call("GET", f"/api/workspace?project_id={self.project['id']}")[1]
        self.assertEqual((mine["total"], mine["executed"], mine["completion"], mine["counts"]["Passed"]), (2, 1, 50.0, 1))
        self.assertEqual(mine["runs"][0]["case_key"], "CHK-2")  # open work first
        everyone = self.admin.call("GET", f"/api/workspace?project_id={self.project['id']}&assignee=__all")[1]
        self.assertEqual(everyone["total"], 3)
        self.assertEqual(self.admin.call("GET", f"/api/workspace?project_id={self.project['id']}&assignee={self.kumar['id']}")[1]["total"], 1)
        self.admin.call("PUT", f"/api/test_cycles/{self.cycle['id']}", {"status": "Completed"})
        self.assertEqual(self.arun_client.call("GET", f"/api/workspace?project_id={self.project['id']}")[1]["total"], 0)
        self.assertEqual(self.arun_client.call("GET", f"/api/workspace?project_id={self.project['id']}&include_completed=true")[1]["total"], 2)


if __name__ == "__main__": unittest.main()
