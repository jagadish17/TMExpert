"""Phase 5: module-wise and individual assignment inside a cycle."""
import tempfile
import threading
import unittest

import server
from test_api import Client, ADMIN_EMAIL, ADMIN_PASSWORD

STEP = [{"action": "Open page", "expected_result": "Page opens"}]


class AssignmentTests(unittest.TestCase):
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
        enrollment = self.module("Enrollment"); waitlist = self.module("Waitlist", enrollment); payment = self.module("Payment")
        for title, folder in [("ENR-1 – Enroll", enrollment), ("ENR-2 – Drop", enrollment), ("ENR-3 – Waitlist join", waitlist), ("PAY-1 – Card", payment), ("PAY-2 – Refund", payment)]:
            case = self.admin.call("POST", "/api/test-cases", {"project_id": self.project["id"], "folder_id": folder["id"], "title": title, "priority": "High", "steps": STEP})[1]
            self.admin.call("POST", f"/api/test-cases/{case['id']}/transition", {"status": "Approved"})
        self.cycle = self.admin.call("POST", "/api/cycles", {"project_id": self.project["id"], "name": "Regression Cycle 01", "testing_type": "Regression",
                                                            "module_ids": [enrollment["id"], payment["id"]]})[1]["cycle"]
        self.arun, self.arun_client = self.member("Arun")
        self.kumar, self.kumar_client = self.member("Kumar")
        self.runs = {r["case_key"]: r for r in self.store.list("test_runs", filters={"cycle_id": self.cycle["id"]})["items"]}

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join(5); self.tmp.cleanup()

    def module(self, name, parent=None):
        folder = self.admin.call("POST", "/api/folders", {"project_id": self.project["id"], "parent_id": parent["id"] if parent else "", "name": name})[1]
        return self.admin.call("POST", f"/api/folders/{folder['id']}/transition", {"status": "Approved"})[1]

    def member(self, name):
        email = f"{name.lower()}@x.local"
        user = self.admin.call("POST", "/api/users", {"name": name, "email": email, "role": "Team Member", "password": "Temp-Pass-1"})[1]
        client = Client(self.base); client.login(email, "Temp-Pass-1")
        client.call("POST", "/api/auth/change-password", {"current_password": "Temp-Pass-1", "new_password": "Team-Pass-22"})
        return user, client

    def assignee(self, key):
        return self.store.get("test_runs", self.runs[key]["id"]).get("assignee_id", "")

    def set_module(self, path, user):
        return self.admin.call("POST", f"/api/cycles/{self.cycle['id']}/module-assignment", {"module_path": path, "assignee_id": user["id"] if user else ""})

    def assign(self, keys, user, **extra):
        return self.admin.call("POST", f"/api/cycles/{self.cycle['id']}/assign", {"run_ids": [self.runs[k]["id"] for k in keys], "assignee_id": user["id"] if user else "", **extra})

    # ------------------------------------------------------------------ modules
    def test_module_assignment_covers_submodules_and_the_deepest_default_wins(self):
        status, result = self.set_module("/Enrollment", self.arun)
        self.assertEqual((status, result["updated"]), (200, 3))
        self.assertEqual({k: self.assignee(k) for k in ("ENR-1", "ENR-2", "ENR-3", "PAY-1")}, {"ENR-1": self.arun["id"], "ENR-2": self.arun["id"], "ENR-3": self.arun["id"], "PAY-1": ""})
        self.set_module("/Enrollment/Waitlist", self.kumar)
        self.assertEqual((self.assignee("ENR-3"), self.assignee("ENR-1")), (self.kumar["id"], self.arun["id"]))
        self.set_module("/Enrollment/Waitlist", None)  # clear -> falls back to the parent default
        self.assertEqual(self.assignee("ENR-3"), self.arun["id"])
        overview = self.admin.call("GET", f"/api/cycles/{self.cycle['id']}/assignments")[1]
        modules = {m["path"]: m for m in overview["modules"]}
        self.assertEqual((modules["/Enrollment"]["default_assignee_id"], modules["/Enrollment"]["total"]), (self.arun["id"], 3))
        self.assertEqual((modules["/Enrollment/Waitlist"]["inherited_assignee_id"], modules["/Enrollment/Waitlist"]["inherited_from"]), (self.arun["id"], "/Enrollment"))
        self.assertEqual((overview["unassigned"], modules["/Payment"]["unassigned"]), (2, 2))
        arun = next(m for m in overview["members"] if m["id"] == self.arun["id"])
        self.assertEqual((arun["cycle_total"], arun["cycle_open"], arun["open_workload"]), (3, 3, 3))

    def test_individual_assignment_overrides_and_survives_module_changes(self):
        self.set_module("/Enrollment", self.arun)
        self.assertEqual(self.assign(["ENR-2"], self.kumar)[0], 200)
        result = self.set_module("/Enrollment", self.kumar)[1]
        self.assertEqual(result["kept_manual"], 1)
        self.set_module("/Enrollment", self.arun)
        self.assertEqual(self.assignee("ENR-2"), self.kumar["id"])  # manual override untouched
        run = self.store.get("test_runs", self.runs["ENR-2"]["id"])
        self.assertEqual(run["assignment_source"], "manual")
        self.assign(["ENR-1"], None)  # explicitly unassigned stays unassigned
        self.set_module("/Enrollment", self.kumar)
        self.assertEqual(self.assignee("ENR-1"), "")
        self.assertEqual(self.assign(["ENR-1", "ENR-2"], None, use_module_default=True)[0], 200)
        self.assertEqual((self.assignee("ENR-1"), self.assignee("ENR-2")), (self.kumar["id"], self.kumar["id"]))
        self.assertEqual(self.store.get("test_runs", self.runs["ENR-2"]["id"])["assignment_source"], "module")

    def test_started_work_is_not_moved_by_module_changes_and_history_is_kept(self):
        self.set_module("/Payment", self.arun)
        self.arun_client.call("PUT", f"/api/test-runs/{self.runs['PAY-1']['id']}/status", {"status": "Failed"})
        result = self.set_module("/Payment", self.kumar)[1]
        self.assertEqual((result["updated"], result["kept_started"]), (1, 1))
        self.assertEqual((self.assignee("PAY-1"), self.assignee("PAY-2")), (self.arun["id"], self.kumar["id"]))
        # An explicit reassignment of executed work keeps its result.
        self.assign(["PAY-1"], self.kumar)
        run = self.store.get("test_runs", self.runs["PAY-1"]["id"])
        self.assertEqual((run["assignee_id"], run["status"]), (self.kumar["id"], "Failed"))
        history = self.admin.call("GET", f"/api/cycles/{self.cycle['id']}/assignment-history")[1]["items"]
        latest = history[0]
        self.assertEqual((latest["case_key"], latest["from_name"], latest["to_name"], latest["actor_name"], latest["source"]), ("PAY-1", "Arun", "Kumar", "Avery Chen", "manual"))
        self.assertGreaterEqual(len(history), 4)
        with self.assertRaises(ValueError): self.store.update("assignment_history", latest["id"], {"to_assignee_id": ""})

    def test_concurrent_drag_and_drop_is_detected(self):
        run_id = self.runs["ENR-1"]["id"]
        self.assertEqual(self.admin.call("POST", f"/api/cycles/{self.cycle['id']}/assign", {"run_ids": [run_id], "assignee_id": self.arun["id"], "expected": {run_id: ""}})[0], 200)
        status, body = self.admin.call("POST", f"/api/cycles/{self.cycle['id']}/assign", {"run_ids": [run_id], "assignee_id": self.kumar["id"], "expected": {run_id: ""}})
        self.assertEqual((status, body["code"], body["current"][run_id]), (409, "assignment_conflict", self.arun["id"]))
        self.assertEqual(self.assignee("ENR-1"), self.arun["id"])

    def test_invalid_assignments_are_rejected(self):
        self.admin.call("PUT", f"/api/users/{self.kumar['id']}", {"status": "Inactive"})
        self.assertEqual(self.assign(["ENR-1"], self.kumar)[0], 400)
        self.assertEqual(self.set_module("/Enrollment", self.kumar)[0], 400)
        self.assertEqual(self.set_module("/Nowhere", self.arun)[0], 400)
        self.assertEqual(self.admin.call("POST", f"/api/cycles/{self.cycle['id']}/assign", {"run_ids": ["missing"], "assignee_id": self.arun["id"]})[0], 404)
        self.assertEqual(self.admin.call("POST", f"/api/cycles/{self.cycle['id']}/assign", {"run_ids": [], "assignee_id": self.arun["id"]})[0], 400)

    # -------------------------------------------------------------- permissions
    def test_team_members_cannot_assign_and_execute_only_their_own_work(self):
        self.assertEqual(self.arun_client.call("POST", f"/api/cycles/{self.cycle['id']}/assign", {"run_ids": [self.runs["ENR-1"]["id"]], "assignee_id": self.arun["id"]})[0], 403)
        self.assertEqual(self.arun_client.call("POST", f"/api/cycles/{self.cycle['id']}/module-assignment", {"module_path": "/Enrollment", "assignee_id": self.arun["id"]})[0], 403)
        self.assign(["ENR-1"], self.arun)
        self.assertEqual(self.arun_client.call("PUT", f"/api/test-runs/{self.runs['ENR-1']['id']}/status", {"status": "Passed"})[0], 200)
        self.assertEqual(self.kumar_client.call("PUT", f"/api/test-runs/{self.runs['ENR-1']['id']}/status", {"status": "Failed"})[0], 403)
        self.assertEqual(self.admin.call("PUT", f"/api/test-runs/{self.runs['ENR-2']['id']}/status", {"status": "Blocked"})[0], 200)
        self.assertEqual(self.kumar_client.call("GET", f"/api/cycles/{self.cycle['id']}/assignments")[0], 200)

    def test_workload_counts_only_open_work_in_open_cycles(self):
        self.set_module("/Enrollment", self.arun)
        self.arun_client.call("PUT", f"/api/test-runs/{self.runs['ENR-1']['id']}/status", {"status": "Passed"})
        arun = next(m for m in self.admin.call("GET", f"/api/cycles/{self.cycle['id']}/assignments")[1]["members"] if m["id"] == self.arun["id"])
        self.assertEqual((arun["cycle_total"], arun["cycle_open"], arun["open_workload"]), (3, 2, 2))
        self.admin.call("PUT", f"/api/test_cycles/{self.cycle['id']}", {"status": "Completed"})
        arun = next(m for m in self.admin.call("GET", f"/api/cycles/{self.cycle['id']}/assignments")[1]["members"] if m["id"] == self.arun["id"])
        self.assertEqual(arun["open_workload"], 0)


if __name__ == "__main__": unittest.main()
