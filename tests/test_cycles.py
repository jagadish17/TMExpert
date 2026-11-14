"""Phase 4: test cycle creation, module selection, snapshots and duplicate prevention."""
import tempfile
import threading
import unittest

import server
from app.storage import ExcelStorageService, XlsxCodec, WORKBOOKS
from test_api import Client, ADMIN_EMAIL, ADMIN_PASSWORD

STEP = [{"action": "Open page", "expected_result": "Page opens"}]


class CycleTests(unittest.TestCase):
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
        # Modules: Enrollment (approved) > Waitlist (approved); Payments (draft)
        self.enrollment = self.module("Enrollment", approve=True)
        self.waitlist = self.module("Waitlist", parent=self.enrollment, approve=True)
        self.payments = self.module("Payments")
        self.e1 = self.case("ENR-1 – Enroll in a course", self.enrollment, approve=True)
        self.e2 = self.case("ENR-2 – Drop a course", self.enrollment, approve=True)
        self.w1 = self.case("ENR-3 – Join the waitlist", self.waitlist, approve=True)
        self.draft = self.case("ENR-4 – Draft scenario", self.enrollment)
        self.p1 = self.case("PAY-1 – Pay by card", self.payments, approve=True)

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join(5); self.tmp.cleanup()

    def module(self, name, parent=None, approve=False):
        status, folder = self.admin.call("POST", "/api/folders", {"project_id": self.project["id"], "parent_id": parent["id"] if parent else "", "name": name})
        self.assertEqual(status, 201, folder)
        if approve: folder = self.admin.call("POST", f"/api/folders/{folder['id']}/transition", {"status": "Approved"})[1]
        return folder

    def case(self, title, folder, approve=False):
        status, case = self.admin.call("POST", "/api/test-cases", {"project_id": self.project["id"], "folder_id": folder["id"], "title": title, "priority": "High", "steps": STEP})
        self.assertEqual(status, 201, case)
        if approve: case = self.admin.call("POST", f"/api/test-cases/{case['id']}/transition", {"status": "Approved"})[1]
        return case

    def info(self, **extra):
        return {"project_id": self.project["id"], "name": "Regression Cycle 01", "testing_type": "Regression", "environment": "QA", "build": "2.5.0",
                "start_date": "2026-11-16", "target_date": "2026-11-20", "description": "Release 2.5", **extra}

    def runs(self, cycle_id):
        return self.store.list("test_runs", filters={"cycle_id": cycle_id}, page_size=1000)["items"]

    # --------------------------------------------------------------- eligibility
    def test_candidates_only_offer_approved_cases_in_approved_modules(self):
        status, data = self.admin.call("GET", f"/api/cycles/candidates?project_id={self.project['id']}")
        self.assertEqual(status, 200)
        keys = {c["case_key"] for c in data["cases"]}
        self.assertTrue({"ENR-1", "ENR-2", "ENR-3"} <= keys)
        self.assertNotIn("ENR-4", keys)  # draft
        self.assertNotIn("PAY-1", keys)  # module not approved
        self.assertEqual((data["excluded_counts"]["Not approved"], data["excluded_counts"]["Module not approved"]), (1, 1))
        override = self.admin.call("GET", f"/api/cycles/candidates?project_id={self.project['id']}&include_unapproved=true")[1]
        self.assertTrue({"ENR-4", "PAY-1"} <= {c["case_key"] for c in override["cases"]})

    def test_selecting_a_module_includes_its_submodules_with_exclusions_and_extras(self):
        preview = self.admin.call("POST", "/api/cycles/preview", {"project_id": self.project["id"], "module_ids": [self.enrollment["id"]],
                                                                 "exclude_case_ids": [self.e2["id"]], "include_case_ids": [self.p1["id"], self.draft["id"]]})[1]
        self.assertEqual([c["case_key"] for c in preview["cases"]], ["ENR-1", "ENR-3"])
        self.assertEqual({s["case_key"]: s["reason"] for s in preview["skipped"]}, {"PAY-1": "Module not approved", "ENR-4": "Not approved"})
        self.assertEqual(preview["rule_excluded"], {"Not approved": 1})
        self.assertEqual([m["path"] for m in preview["modules"]], ["/Enrollment", "/Enrollment/Waitlist"])

    # ------------------------------------------------------------------ creation
    def test_create_cycle_snapshots_each_case(self):
        status, result = self.admin.call("POST", "/api/cycles", {**self.info(), "module_ids": [self.enrollment["id"]]})
        self.assertEqual(status, 201, result)
        cycle = result["cycle"]
        self.assertEqual((cycle["testing_type"], cycle["status"], cycle["end_date"], cycle["module_ids"], result["added"]),
                         ("Regression", "Not Started", "2026-11-20", [self.enrollment["id"]], 3))
        run = next(r for r in self.runs(cycle["id"]) if r["case_key"] == "ENR-1")
        self.assertEqual((run["case_title"], run["module_path"], run["test_case_version"], run["status"], run["tester_id"], run["environment"]),
                         ("ENR-1 – Enroll in a course", "/Enrollment", "1", "Not Run", "", "QA"))
        # Later repository changes do not alter the cycle.
        self.admin.call("PUT", f"/api/test_cases/{self.e1['id']}", {"title": "ENR-1 – Enroll in a course (v2)", "steps": STEP + [{"action": "Confirm", "expected_result": "Enrolled"}]})
        self.admin.call("POST", "/api/test-cases/bulk", {"action": "move", "ids": [self.e1["id"]], "folder_id": self.waitlist["id"]})
        run_after = self.store.get("test_runs", run["id"])
        self.assertEqual((run_after["test_case_version"], run_after["case_title"], run_after["module_path"]), ("1", "ENR-1 – Enroll in a course", "/Enrollment"))
        detail = self.admin.call("GET", f"/api/test-runs/{run['id']}/execution")[1]
        self.assertEqual((len(detail["steps"]), detail["test_case"]["title"]), (1, "ENR-1 – Enroll in a course"))

    def test_cycle_information_is_validated(self):
        selection = {"module_ids": [self.enrollment["id"]]}
        for changes, message in [({"name": " "}, "name is required"), ({"testing_type": "Exploratory"}, "testing type"),
                                 ({"testing_type": "Custom"}, "custom testing type"), ({"target_date": "2026-11-01"}, "before the start"),
                                 ({"start_date": "16/11/2026"}, "must be a date")]:
            status, body = self.admin.call("POST", "/api/cycles", {**self.info(**changes), **selection})
            self.assertEqual(status, 400); self.assertIn(message, body["error"])
        status, body = self.admin.call("POST", "/api/cycles", {**self.info(), "module_ids": [self.payments["id"]]})
        self.assertEqual(status, 400); self.assertIn("at least one eligible", body["error"])
        self.assertEqual(self.admin.call("POST", "/api/cycles", {**self.info(testing_type="Custom", custom_type="Accessibility"), **selection})[1]["cycle"]["testing_type"], "Accessibility")
        status, body = self.admin.call("POST", "/api/cycles", {**self.info(name="regression cycle 01"), **selection})
        self.assertEqual(status, 400); self.assertIn("already exists", body["error"])
        self.assertEqual(self.store.list("test_cycles", filters={"project_id": self.project["id"]})["total"], 1)

    def test_unapproved_cases_need_the_explicit_admin_override(self):
        status, result = self.admin.call("POST", "/api/cycles", {**self.info(), "module_ids": [self.payments["id"]], "include_unapproved": True})
        self.assertEqual((status, result["added"]), (201, 1))

    def test_plan_is_optional_and_linked_when_chosen(self):
        plan = self.store.create("test_plans", {"project_id": self.project["id"], "name": "Release 2.5"})
        cycle = self.admin.call("POST", "/api/cycles", {**self.info(plan_id=plan["id"]), "module_ids": [self.enrollment["id"]]})[1]["cycle"]
        suite = self.store.get("test_suites", cycle["suite_id"])
        self.assertEqual(suite["plan_id"], plan["id"])

    # ---------------------------------------------------------------- duplicates
    def test_a_case_appears_once_per_cycle(self):
        cycle = self.admin.call("POST", "/api/cycles", {**self.info(), "module_ids": [self.waitlist["id"]]})[1]["cycle"]
        status, result = self.admin.call("POST", f"/api/cycles/{cycle['id']}/add-cases", {"module_ids": [self.enrollment["id"]]})
        self.assertEqual((status, result["added"], result["already_in_cycle"]), (200, 2, 1))
        self.assertEqual(len(self.runs(cycle["id"])), 3)
        status, body = self.admin.call("POST", f"/api/cycles/{cycle['id']}/add-cases", {"include_case_ids": [self.e1["id"]]})
        self.assertEqual(status, 400); self.assertIn("already in this cycle", body["error"])
        preview = self.admin.call("POST", f"/api/cycles/{cycle['id']}/preview", {"module_ids": [self.enrollment["id"]]})[1]
        self.assertEqual((preview["total"], len(preview["already_in_cycle"])), (0, 3))
        status, body = self.admin.call("POST", "/api/test_runs", {"project_id": self.project["id"], "cycle_id": cycle["id"], "test_case_id": self.e1["id"]})
        self.assertEqual(status, 400); self.assertIn("already in the cycle", body["error"])
        with self.assertRaisesRegex(ValueError, "already in the cycle"):
            self.store.batch_create("test_runs", [{"project_id": self.project["id"], "cycle_id": cycle["id"], "test_case_id": self.p1["id"], "status": "Not Run"}] * 2)

    def test_only_unexecuted_cases_can_be_removed(self):
        cycle = self.admin.call("POST", "/api/cycles", {**self.info(), "module_ids": [self.enrollment["id"]]})[1]["cycle"]
        runs = self.runs(cycle["id"])
        self.admin.call("PUT", f"/api/test-runs/{runs[0]['id']}/status", {"status": "Passed"})
        status, body = self.admin.call("DELETE", f"/api/test_runs/{runs[0]['id']}")
        self.assertEqual(status, 400); self.assertIn("not been executed", body["error"])
        self.assertEqual(self.admin.call("DELETE", f"/api/test_runs/{runs[1]['id']}")[0], 200)
        self.assertEqual(len(self.runs(cycle["id"])), 2)
        status, re_added = self.admin.call("POST", f"/api/cycles/{cycle['id']}/add-cases", {"include_case_ids": [runs[1]["test_case_id"]]})
        self.assertEqual((status, re_added["added"]), (200, 1))

    def test_summary_counts_progress(self):
        cycle = self.admin.call("POST", "/api/cycles", {**self.info(), "module_ids": [self.enrollment["id"]]})[1]["cycle"]
        runs = sorted(self.runs(cycle["id"]), key=lambda r: r["case_key"])
        self.admin.call("PUT", f"/api/test-runs/{runs[0]['id']}/status", {"status": "Passed"})
        self.admin.call("PUT", f"/api/test-runs/{runs[1]['id']}/status", {"status": "Failed"})
        summary = self.admin.call("GET", f"/api/cycles/{cycle['id']}/summary")[1]
        self.assertEqual((summary["total"], summary["executed"], summary["completion"], summary["pass_rate"]), (3, 2, 66.7, 50.0))
        self.assertEqual(summary["counts"]["Not Run"], 1)
        self.assertEqual([(m["path"], m["total"]) for m in summary["modules"]], [("/Enrollment", 2), ("/Enrollment/Waitlist", 1)])

    def test_cycle_details_can_be_edited_with_validation(self):
        cycle = self.admin.call("POST", "/api/cycles", {**self.info(), "module_ids": [self.enrollment["id"]]})[1]["cycle"]
        status, updated = self.admin.call("PUT", f"/api/test_cycles/{cycle['id']}", {"build": "2.5.1", "status": "In Progress"})
        self.assertEqual((status, updated["build"], updated["status"], updated["name"]), (200, "2.5.1", "In Progress", "Regression Cycle 01"))
        self.assertEqual(self.admin.call("PUT", f"/api/test_cycles/{cycle['id']}", {"status": "Paused"})[0], 400)

    def test_team_members_cannot_create_cycles(self):
        self.admin.call("POST", "/api/users", {"name": "Tara", "email": "tara@x.local", "role": "Team Member", "password": "Temp-Pass-1"})
        team = Client(self.base); team.login("tara@x.local", "Temp-Pass-1")
        team.call("POST", "/api/auth/change-password", {"current_password": "Temp-Pass-1", "new_password": "Team-Pass-22"})
        self.assertEqual(team.call("GET", f"/api/cycles/candidates?project_id={self.project['id']}")[0], 403)
        self.assertEqual(team.call("POST", "/api/cycles", {**self.info(), "module_ids": [self.enrollment["id"]]})[0], 403)
        cycle = self.admin.call("POST", "/api/cycles", {**self.info(), "module_ids": [self.enrollment["id"]]})[1]["cycle"]
        self.assertEqual(team.call("GET", f"/api/cycles/{cycle['id']}/summary")[0], 200)


class CycleUpgradeTests(unittest.TestCase):
    def test_existing_cycles_and_runs_gain_new_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExcelStorageService(tmp)
            XlsxCodec.write(store.path_for("test_runs"), WORKBOOKS["test_runs"][1][:15], [{"id": "r1", "project_id": "p", "cycle_id": "c", "test_case_id": "t", "status": "Passed", "archived": "False"}])
            XlsxCodec.write(store.path_for("test_cycles"), WORKBOOKS["test_cycles"][1][:13], [{"id": "c", "project_id": "p", "suite_id": "s", "name": "Old cycle", "archived": "False"}])
            upgraded = ExcelStorageService(tmp)
            self.assertEqual((upgraded.get("test_runs", "r1")["status"], upgraded.get("test_runs", "r1")["case_key"]), ("Passed", ""))
            self.assertEqual(upgraded.get("test_cycles", "c")["name"], "Old cycle")


if __name__ == "__main__": unittest.main()


class ConcurrencyTests(unittest.TestCase):
    def test_parallel_reads_and_writes_do_not_fail_or_lose_updates(self):
        import threading as th
        with tempfile.TemporaryDirectory() as tmp:
            store = ExcelStorageService(tmp, backup_interval_seconds=0)
            from app.service import TMExpertService
            TMExpertService(store).bootstrap()
            project = store.list("projects")["items"][0]
            errors = []
            def writer(n):
                try:
                    for i in range(8): store.create("requirements", {"project_id": project["id"], "key": f"W{n}-{i}", "title": "t", "status": "Draft"})
                except Exception as exc: errors.append(repr(exc))
            def reader():
                try:
                    for _ in range(40):
                        store.cache.clear()  # force real file reads while writers replace the workbook
                        store.list("requirements")
                except Exception as exc: errors.append(repr(exc))
            threads = [th.Thread(target=writer, args=(n,)) for n in range(4)] + [th.Thread(target=reader) for _ in range(3)]
            for t in threads: t.start()
            for t in threads: t.join(120)
            self.assertEqual(errors, [])
            self.assertEqual(ExcelStorageService(tmp).list("requirements", page_size=1000)["total"], 32 + 1)  # +1 seeded requirement
