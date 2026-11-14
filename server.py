from __future__ import annotations
import base64, binascii, http.cookies, json, mimetypes, os, socket, sys, traceback, urllib.parse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from app.storage import ExcelStorageService, WORKBOOKS
from app.service import TMExpertService
from app.workflow import normalize_title
from app.cycles import CycleService, TESTING_TYPES
from app.assignments import AssignmentService, AssignmentConflict
from app.attachments import AttachmentService
from app.execution import ExecutionService, StaleExecution
from app.comparison import ComparisonService
from app.reports import ReportService
from app.audit_log import AuditLogService
from app.auth import AuthService, PermissionDenied, canonical_role, has_permission, public_user, ROLE_PERMISSIONS, ADMIN

ROOT = Path(__file__).parent; WEB = ROOT / "web"
DATA = Path(os.getenv("TMEXPERT_DATA_DIR") or ROOT / "application-data")
SESSION_COOKIE = "tmx_session"


def resolve_port(value: str | None = None, host: str = "127.0.0.1") -> int:
    requested = int(value if value is not None else os.getenv("TMEXPERT_PORT", "8787"))
    if requested == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            return int(sock.getsockname()[1])

    for candidate in [requested, *range(requested + 1, requested + 20)]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind((host, candidate))
                return candidate
        except OSError:
            continue
    raise RuntimeError(f"No free local port available near {requested}")


def resolve_host() -> str:
    """127.0.0.1 by default; set TMEXPERT_HOST=0.0.0.0 to share with the team network."""
    return os.getenv("TMEXPERT_HOST", "127.0.0.1").strip() or "127.0.0.1"


class App:
    def __init__(self, data_dir: str | Path):
        self.store = ExcelStorageService(data_dir)
        self.service = TMExpertService(self.store); self.service.bootstrap()
        self.auth = AuthService(self.store)
        self.cycles = CycleService(self.store, self.service.workflow)
        self.assignments = AssignmentService(self.store)
        self.attachments = AttachmentService(self.store)
        self.execution = ExecutionService(self.store, self.service, self.attachments)
        self.comparison = ComparisonService(self.store)
        self.reports = ReportService(self.store, self.service.workflow)
        self.audit_log = AuditLogService(self.store)


# Generic record routes.  Anything not listed is not reachable generically:
# users, approvals, audit and history have dedicated, permission-checked paths.
READ_PERMISSIONS = {
    "projects": "projects.read", "folders": "repository.read", "test_cases": "repository.read",
    "test_steps": "repository.read", "test_case_versions": "repository.read", "approvals": "repository.read",
    "requirements": "requirements.read", "test_plans": "plans.read", "test_suites": "plans.read",
    "test_cycles": "cycles.read", "test_runs": "execution.read", "execution_results": "execution.read",
    "defects": "execution.read", "audit": "audit.read",
}
CREATE_PERMISSIONS = {
    "requirements": "requirements.write", "test_plans": "plans.write",
    "test_suites": "plans.write", "test_cycles": "cycles.write", "defects": "execution.update",
}
UPDATE_PERMISSIONS = {
    "projects": "projects.write", "test_cases": "repository.write", "requirements": "requirements.write",
    "test_plans": "plans.write", "test_suites": "plans.write", "test_cycles": "cycles.write", "defects": "execution.update",
}
DELETE_PERMISSIONS = {
    "projects": "projects.write", "folders": "repository.write", "test_cases": "repository.write",
    "requirements": "requirements.write", "test_plans": "plans.write", "test_suites": "plans.write",
    "test_cycles": "cycles.write", "test_runs": "cycles.write", "defects": "execution.update",
}
# Repository content a team member may see only when its test case is approved.
CASE_CHILD_ENTITIES = {"test_steps", "test_case_versions", "approvals"}


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "", extra: dict | None = None):
        super().__init__(message); self.status = status; self.code = code; self.extra = extra or {}


class Handler(SimpleHTTPRequestHandler):
    server_version = "TMExpert"
    sys_version = ""

    @property
    def app(self) -> App: return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format, *args): print("TMExpert", format % args)

    # ---------------------------------------------------------------- helpers
    def _send(self, status: int, blob: bytes, content_type: str, extra: dict[str, str] | None = None):
        if getattr(self, "_held", None) is not None:
            self._held.append((status, blob, content_type, extra)); return
        self.send_response(status)
        self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY"); self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if not (extra and "Content-Security-Policy" in extra):
            # Only TMExpert's own scripts, styles, images and API; nothing can be loaded from or sent to another site.
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for key, value in (extra or {}).items(): self.send_header(key, value)
        self.end_headers(); self.wfile.write(blob)
    def _json(self, status, data, extra: dict[str, str] | None = None):
        self._send(status, json.dumps(data, default=str).encode(), "application/json", extra)
    def _download(self, blob: bytes, content_type: str, filename: str):
        self._send(200, blob, content_type, {"Content-Disposition": f'attachment; filename="{filename}"'})
    def _body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length < 0 or length > 14 * 1024 * 1024: raise ValueError("Request body exceeds the 14 MB limit")
        if length and not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            raise ApiError(415, "Requests must be sent as application/json")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Request body is not valid JSON") from exc
        if not isinstance(body, dict): raise ValueError("Request body must be a JSON object")
        return body
    def _token(self) -> str | None:
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else None
    def _check_origin(self):
        """Reject cross-site writes (in addition to the SameSite=Strict session cookie)."""
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host", ""):
            raise ApiError(403, "Cross-site requests are not allowed")
    def _user(self, allow_password_change: bool = False) -> dict:
        user = self.app.auth.user_for(self._token())
        if not user: raise ApiError(401, "Please sign in", "auth_required")
        if user.get("must_change_password") is True and not allow_password_change:
            raise ApiError(403, "Change your password to continue", "password_change_required")
        return user
    def _require(self, user: dict, permission: str):
        if not has_permission(user, permission): raise PermissionDenied("You do not have permission to perform this action")
    def _is_admin(self, user: dict) -> bool: return canonical_role(user.get("role")) == ADMIN
    def _require_run_owner(self, user: dict, run_id: str):
        """Team members record results only for test cases assigned to them."""
        if has_permission(user, "execution.update_any"): return
        run = self.app.store.get("test_runs", run_id, include_archived=False)
        if not run: raise KeyError("Test run not found")
        if run.get("assignee_id") != user["id"]:
            raise PermissionDenied("This test case is not assigned to you")
    def _attachment_access(self, user: dict, owner_type: str, owner_id: str, write: bool) -> dict:
        """Check access to a test run's evidence or a test case's attachments; return project info."""
        store = self.app.store
        if owner_type == "test_run":
            run = store.get("test_runs", owner_id, include_archived=False)
            if not run: raise KeyError("Test run not found")
            if write: self._require(user, "execution.update"); self._require_run_owner(user, owner_id)
            else: self._require(user, "execution.read")
            return {"project_id": run.get("project_id", "")}
        if owner_type == "test_case":
            case = store.get("test_cases", owner_id, include_archived=False)
            if not case: raise KeyError("Test case not found")
            self._require(user, "repository.write" if write else "repository.read")
            if not write and not self._is_admin(user) and not self.app.service.workflow.case_visible_to_team(case): raise KeyError("Test case not found")
            return {"project_id": case.get("project_id", "")}
        raise ValueError("Unknown attachment owner")
    def _send_file(self, record: dict, data: bytes):
        from app.attachments import INLINE
        import os as _os
        name = record.get("filename", "file")
        ascii_name = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in name)
        disposition = "inline" if _os.path.splitext(name)[1].lower() in INLINE else "attachment"
        self._send(200, data, record.get("content_type") or "application/octet-stream", {
            "Content-Disposition": f'{disposition}; filename="{ascii_name}"; filename*=UTF-8\'\'{urllib.parse.quote(name)}',
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
        })
    def _session_payload(self, user: dict) -> dict:
        role = canonical_role(user.get("role"))
        return {"user": public_user(user), "must_change_password": user.get("must_change_password") is True,
                "permissions": sorted(ROLE_PERMISSIONS.get(role, set()))}
    def _approved_case(self, case_id: str) -> bool:
        return self.app.service.workflow.visible_case(case_id)
    def _reject_duplicates(self, body: dict, project_id: str, title, folder_id: str, exclude_id: str = ""):
        if body.pop("allow_duplicate", False) is True: return
        duplicates = self.app.service.workflow.find_duplicates(project_id, title, folder_id, exclude_id)
        if duplicates:
            raise ApiError(409, "A test case with this title already exists in this module", "duplicate", {"duplicates": duplicates})

    def _dispatch(self, method: str):
        parsed = urllib.parse.urlparse(self.path); route = parsed.path
        try:
            if not route.startswith("/api/"):
                if method != "GET": raise ApiError(404, "Not found")
                return self._static(route)
            if method != "GET": self._check_origin()
            query = dict(urllib.parse.parse_qsl(parsed.query))
            parts = [urllib.parse.unquote(part) for part in route.split("/")[2:]]
            handler = getattr(self, f"_{method.lower()}_api")
            if method == "GET": return handler(route, parts, query)
            # One audit write per request, and the reply is held until that write has succeeded.
            self._held = []
            try:
                with self.app.store.audit_batch():
                    handler(route, parts, query)
            finally:
                held, self._held = self._held, None
            for args in held: self._send(*args)
            return None
        except ApiError as exc: self._json(exc.status, {"error": str(exc), "code": exc.code, **exc.extra})
        except StaleExecution as exc: self._json(409, {"error": str(exc), "code": "stale_execution"})
        except AssignmentConflict as exc: self._json(409, {"error": str(exc), "code": "assignment_conflict", "current": exc.current})
        except PermissionDenied as exc: self._json(403, {"error": str(exc), "code": "forbidden"})
        except PermissionError as exc: self._json(401, {"error": str(exc), "code": "auth_failed"})
        except (KeyError, FileNotFoundError) as exc:
            message = exc.args[0] if exc.args else "Not found"
            self._json(404 if "not found" in str(message).lower() else 400, {"error": str(message)})
        except (ValueError, TimeoutError, IndexError, TypeError) as exc: self._json(400, {"error": str(exc) or "Invalid request"})
        except Exception:  # never drop the connection; log the stack, not the request body
            traceback.print_exc(file=sys.stderr)
            self._json(500, {"error": "Unexpected server error. The change was not saved; please retry."})

    def do_GET(self): self._dispatch("GET")
    def do_POST(self): self._dispatch("POST")
    def do_PUT(self): self._dispatch("PUT")
    def do_DELETE(self): self._dispatch("DELETE")

    # -------------------------------------------------------------------- GET
    def _get_api(self, route, parts, query):
        store, service = self.app.store, self.app.service
        if route == "/api/health": return self._json(200, {"status": "ok", "storage": "excel-only", "workbooks": len(WORKBOOKS)})
        if route == "/api/auth/session": return self._json(200, self._session_payload(self._user(allow_password_change=True)))
        user = self._user()
        if route == "/api/dashboard":
            self._require(user, "dashboard.read"); return self._json(200, service.dashboard(query.get("project_id", "")))
        if route == "/api/search":
            self._require(user, "repository.read")
            items = store.search(query.get("q", ""), query.get("project_id", ""))
            if not self._is_admin(user):
                items = [item for item in items if item["entity"] != "test_cases" or item["record"].get("status") == "Approved"]
            return self._json(200, {"items": items})
        if route == "/api/export/test-cases":
            self._require(user, "repository.write")
            return self._download(service.export_cases_csv(query["project_id"]), "text/csv", "TMExpert_Test_Cases.csv")
        if route in {"/api/export/test-cases.xlsx", "/api/test-case-import-template.xlsx"}:
            self._require(user, "repository.write")
            template = route == "/api/test-case-import-template.xlsx"
            blob = service.export_cases_xlsx(query.get("project_id", ""), template=template, include_ids=query.get("include_ids") == "true")
            filename = "TMExpert_Test_Case_Import_Template.xlsx" if template else "TMExpert_Test_Cases.xlsx"
            return self._download(blob, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename)
        if len(parts) == 3 and parts[0] == "test-runs" and parts[2] == "execution":
            self._require(user, "execution.read"); return self._json(200, self.app.execution.detail(parts[1]))
        if len(parts) == 3 and parts[0] == "test-runs" and parts[2] == "history":
            self._require(user, "execution.read"); return self._json(200, {"items": self.app.execution.history(parts[1])})
        if route == "/api/reports/dashboard" or (len(parts) == 2 and parts[0] == "reports" and parts[1].endswith(".xlsx")):
            self._require(user, "dashboard.read")
            filters = {k: query.get(k, "") for k in ("cycle_ids", "cycle_scope", "module", "member", "date_from", "date_to")}
            if not has_permission(user, "reports.export"): filters["member"] = user["id"]  # team members see their own work
            if route == "/api/reports/dashboard": return self._json(200, self.app.reports.dashboard(query.get("project_id", ""), filters))
            self._require(user, "reports.export")
            blob, filename = self.app.reports.export(parts[1][:-5], query.get("project_id", ""), filters, store.root / "temp")
            return self._download(blob, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename)
        if len(parts) == 3 and parts[0] == "test-cases" and parts[2] == "cycle-history":
            self._require(user, "execution.read")
            case = store.get("test_cases", parts[1])
            if not case: raise KeyError("Test case not found")
            return self._json(200, self.app.comparison.case_history(case["project_id"], parts[1], self.app.execution))
        if len(parts) == 3 and parts[0] == "cycles" and parts[2] == "timeline":
            self._require(user, "cycles.read"); return self._json(200, self.app.comparison.cycle_timeline(parts[1]))
        if route in {"/api/comparison", "/api/comparison/export.xlsx"}:
            self._require(user, "cycles.read")
            ids = [item for item in query.get("cycle_ids", "").split(",") if item]
            data = self.app.comparison.compare(query.get("project_id", ""), ids, query.get("previous_id", ""), query.get("current_id", ""))
            if route == "/api/comparison": return self._json(200, data)
            return self._download(self.app.comparison.export_xlsx(data, store.root / "temp"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "TMExpert_Cycle_Comparison.xlsx")
        if route in {"/api/audit-log", "/api/audit-log/verify", "/api/audit-log/export.xlsx"}:
            self._require(user, "audit.read")
            if route.endswith("/verify"): return self._json(200, store.verify_audit())
            filters = {k: query.get(k, "") for k in ("entity", "action", "actor", "date_from", "date_to", "q", "page", "page_size")}
            if route.endswith(".xlsx"):
                return self._download(self.app.audit_log.export(query.get("project_id", ""), filters, store.root / "temp"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "TMExpert_Audit_Log.xlsx")
            return self._json(200, self.app.audit_log.query(query.get("project_id", ""), filters))
        if route == "/api/imports":
            self._require(user, "repository.write"); return self._json(200, {"items": service.importer.history(query.get("project_id", ""))})
        if len(parts) == 3 and parts[0] == "imports" and parts[2] == "file":
            self._require(user, "repository.write")
            blob, filename = service.importer.stored_file(parts[1])
            return self._download(blob, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename.replace('"', "_"))
        if route == "/api/workspace":
            self._require(user, "execution.read")
            assignee = query.get("assignee", "me")
            if assignee == "me" or not has_permission(user, "execution.update_any"): assignee = user["id"]
            return self._json(200, self.app.execution.workspace(query.get("project_id", ""), assignee, query.get("cycle_id", ""), query.get("include_completed") == "true"))
        if route == "/api/attachments":
            self._attachment_access(user, query.get("owner_type", ""), query.get("owner_id", ""), write=False)
            return self._json(200, {"items": self.app.attachments.list(query.get("owner_type", ""), query.get("owner_id", ""))})
        if len(parts) == 3 and parts[0] == "attachments" and parts[2] == "file":
            record = self.app.attachments.get(parts[1])
            self._attachment_access(user, record["owner_type"], record["owner_id"], write=False)
            return self._send_file(record, self.app.attachments.read(record))
        workflow = service.workflow
        if len(parts) == 3 and parts[0] == "test-cases" and parts[2] == "details":
            self._require(user, "repository.read")
            if not self._is_admin(user) and not workflow.visible_case(parts[1]): raise KeyError("Test case not found")
            details = workflow.case_details(parts[1])
            if not self._is_admin(user):
                for key in ("approvals", "latest_review", "allowed_transitions"): details.pop(key, None)
            return self._json(200, details)
        if len(parts) == 4 and parts[0] == "test-cases" and parts[2] == "versions":
            self._require(user, "repository.read")
            if not self._is_admin(user) and not workflow.visible_case(parts[1]): raise KeyError("Test case not found")
            return self._json(200, workflow.version_detail(parts[1], parts[3]))
        if route == "/api/cycles/candidates":
            self._require(user, "cycles.write")
            include_unapproved = query.get("include_unapproved") == "true"
            if include_unapproved: self._require(user, "approvals.decide")
            return self._json(200, {**self.app.cycles.candidates(query.get("project_id", ""), include_unapproved), "testing_types": list(TESTING_TYPES)})
        if len(parts) == 3 and parts[0] == "cycles" and parts[2] == "summary":
            self._require(user, "cycles.read"); return self._json(200, self.app.cycles.summary(parts[1]))
        if len(parts) == 3 and parts[0] == "cycles" and parts[2] == "assignments":
            self._require(user, "cycles.read"); return self._json(200, self.app.assignments.overview(parts[1]))
        if len(parts) == 3 and parts[0] == "cycles" and parts[2] == "assignment-history":
            self._require(user, "cycles.read"); return self._json(200, {"items": self.app.assignments.history(parts[1])})
        if route == "/api/approvals/queue":
            self._require(user, "approvals.decide"); return self._json(200, workflow.approval_queue(query.get("project_id", "")))
        if parts[0] == "users":
            self._require(user, "users.directory")
            if len(parts) == 2:
                target = store.get("users", parts[1])
                if not target: raise KeyError("User not found")
                return self._json(200, public_user(target))
            users = [public_user(u) for u in store.list("users", page_size=100000, include_archived=query.get("archived") == "true")["items"]]
            return self._json(200, {"items": users, "total": len(users), "page": 1, "page_size": len(users)})
        entity = parts[0]
        if entity not in READ_PERMISSIONS: raise ApiError(404, "Unknown resource")
        self._require(user, READ_PERMISSIONS[entity])
        admin = self._is_admin(user)
        if len(parts) == 2:
            record = store.get(entity, parts[1]) or {}
            if not admin and record:
                if entity == "test_cases" and not workflow.case_visible_to_team(record): raise KeyError("Record not found")
                if entity == "folders" and not workflow.folder_chain_approved(record["id"], workflow._folders(record.get("project_id", ""))): raise KeyError("Record not found")
                if entity in CASE_CHILD_ENTITIES and not self._approved_case(str(record.get("test_case_id", ""))): raise KeyError("Record not found")
            return self._json(200, record)
        filters = {k: v for k, v in query.items() if k not in {"q", "page", "page_size", "archived"}}
        page = max(1, int(query.get("page", 1))); page_size = max(1, min(int(query.get("page_size", 50)), 200))
        if not admin:
            if entity in CASE_CHILD_ENTITIES and not self._approved_case(filters.get("test_case_id", "")):
                raise PermissionDenied("Approved test case required")
            if entity == "test_cases": return self._json(200, workflow.list_team_cases(filters, query.get("q", ""), page, page_size))
            if entity == "folders": return self._json(200, workflow.list_team_folders(filters, page, page_size))
        include_archived = query.get("archived") == "true" and admin
        return self._json(200, store.list(entity, filters=filters, query=query.get("q", ""), page=max(1, int(query.get("page", 1))), page_size=max(1, min(int(query.get("page_size", 50)), 200)), include_archived=include_archived))

    # ------------------------------------------------------------------- POST
    def _post_api(self, route, parts, query):
        store, service, auth = self.app.store, self.app.service, self.app.auth
        if route == "/api/auth/login":
            body = self._body()
            token, user = auth.login(body.get("email"), body.get("password"))
            cookie = f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/" + ("; Secure" if getattr(self.server, "tls", False) else "")
            return self._json(200, self._session_payload(user), {"Set-Cookie": cookie})
        if route == "/api/auth/logout":
            auth.logout(self._token())
            return self._json(200, {"ok": True}, {"Set-Cookie": f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
        if route == "/api/auth/change-password":
            user = self._user(allow_password_change=True); body = self._body()
            updated = auth.change_password(user, body.get("current_password"), body.get("new_password"), self._token())
            return self._json(200, self._session_payload(updated))
        user = self._user(); actor = user["id"]
        if route == "/api/users":
            self._require(user, "users.manage"); return self._json(201, public_user(auth.create_user(self._body(), actor)))
        if len(parts) == 3 and parts[0] == "users" and parts[2] == "reset-password":
            self._require(user, "users.manage")
            return self._json(200, public_user(auth.reset_password(parts[1], self._body().get("password"), actor)))
        if route == "/api/test-cases/bulk-delete":
            self._require(user, "repository.write")
            ids = self._body().get("ids")
            if not isinstance(ids, list) or not ids:
                raise ValueError("Select at least one test case to delete")
            return self._json(200, store.delete_many("test_cases", [str(item) for item in ids], actor))
        workflow = service.workflow
        if route == "/api/test-cases":
            self._require(user, "repository.write"); body = self._body()
            self._reject_duplicates(body, str(body.get("project_id", "")), body.get("title"), str(body.get("folder_id", "")))
            body.pop("status", None)
            return self._json(201, service.create_test_case(body, actor))
        if route == "/api/test-cases/bulk":
            body = self._body(); action = str(body.get("action", ""))
            self._require(user, "approvals.decide" if action in {"approve", "reject", "deprecate", "restore"} else "repository.write")
            return self._json(200, workflow.bulk(action, body.get("ids"), body, actor))
        if len(parts) == 3 and parts[0] == "test-cases" and parts[2] == "transition":
            body = self._body(); target = str(body.get("status", ""))
            self._require(user, "repository.write" if target in {"In Review", "Draft"} and (store.get("test_cases", parts[1]) or {}).get("status") != "Deprecated" else "approvals.decide")
            return self._json(200, workflow.transition_case(parts[1], target, body.get("comment"), actor))
        if len(parts) == 3 and parts[0] == "folders" and parts[2] == "transition":
            body = self._body(); target = str(body.get("status", ""))
            self._require(user, "repository.write" if target in {"In Review", "Draft"} else "approvals.decide")
            return self._json(200, workflow.transition_folder(parts[1], target, body.get("comment"), actor))
        if route == "/api/folders":
            self._require(user, "repository.write"); return self._json(201, workflow.create_folder(self._body(), actor))
        if len(parts) == 3 and parts[0] == "test-runs" and parts[2] == "execute":
            self._require(user, "execution.update"); self._require_run_owner(user, parts[1])
            return self._json(200, self.app.execution.execute(parts[1], self._body(), actor))
        if route == "/api/attachments":
            body = self._body(); owner_type = str(body.get("owner_type", "")); owner_id = str(body.get("owner_id", ""))
            info = self._attachment_access(user, owner_type, owner_id, write=True)
            record = self.app.attachments.upload(owner_type, owner_id, info["project_id"], body.get("filename"), body.get("data"), actor)
            return self._json(201, self.app.attachments.public(record))
        if len(parts) == 3 and parts[0] == "cycles" and parts[2] == "module-assignment":
            self._require(user, "assignments.manage"); body = self._body()
            return self._json(200, self.app.assignments.set_module_default(parts[1], body.get("module_path"), body.get("assignee_id"), actor))
        if len(parts) == 3 and parts[0] == "cycles" and parts[2] == "assign":
            self._require(user, "assignments.manage"); body = self._body()
            expected = body.get("expected") if isinstance(body.get("expected"), dict) else None
            return self._json(200, self.app.assignments.assign_runs(parts[1], body.get("run_ids"), body.get("assignee_id"), actor, expected, body.get("use_module_default") is True))
        if route in {"/api/cycles", "/api/cycles/preview"} or (len(parts) == 3 and parts[0] == "cycles" and parts[2] in {"add-cases", "preview"}):
            self._require(user, "cycles.write"); body = self._body()
            if body.get("include_unapproved") is True: self._require(user, "approvals.decide")
            cycles = self.app.cycles
            if route == "/api/cycles": return self._json(201, cycles.create_cycle(body, actor))
            if route == "/api/cycles/preview": return self._json(200, cycles.preview(str(body.get("project_id", "")), body))
            cycle = store.get("test_cycles", parts[1], include_archived=False)
            if not cycle: raise KeyError("Cycle not found")
            if parts[2] == "preview": return self._json(200, cycles.preview(cycle["project_id"], body, cycle["id"]))
            return self._json(200, cycles.add_cases(parts[1], body, actor))
        if route == "/api/projects":
            self._require(user, "projects.write"); return self._json(201, service.create_project(self._body(), actor))
        if route == "/api/test_runs":
            self._require(user, "cycles.write"); return self._json(201, service.create_test_run(self._body(), actor))
        if route in {"/api/import/test-cases", "/api/import/test-cases/preview"}:
            self._require(user, "repository.write")
            body = self._body()
            encoded_workbook = body.get("workbook", "")
            if not isinstance(encoded_workbook, str):
                raise ValueError("The uploaded Excel file is invalid")
            try:
                workbook = base64.b64decode(encoded_workbook, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("The uploaded Excel file is invalid") from exc
            options = {"target_folder_id": str(body.get("target_folder_id", "") or ""), "allow_duplicates": body.get("allow_duplicates") is True,
                       "create_missing_modules": body.get("create_missing_modules") is True, "sheet": str(body.get("sheet", "") or "")}
            importer = service.importer
            if route.endswith("/preview"):
                return self._json(200, importer.public(importer.plan(workbook, str(body.get("project_id", "")), options)))
            result = importer.commit(workbook, str(body.get("project_id", "")), options, actor, str(body.get("filename", "") or ""))
            if not result["created"]:
                plan = result["plan"]
                errors = [{"row": e["row"], "error": e["message"], "column": e.get("column", ""), **({"duplicate": True} if e.get("duplicate") else {})} for e in plan["errors"]] or [{"row": 1, "error": "No test cases were found in the workbook"}]
                return self._json(200, {"created": 0, "errors": errors, "duplicates_only": all(e.get("duplicate") for e in errors),
                                        "message": "Nothing imported; correct the reported rows and try again.", "preview": importer.public(plan)})
            return self._json(201, {k: v for k, v in result.items() if k != "plan"} | {"errors": []})
        if len(parts) == 3 and parts[0] == "test-cases" and parts[2] == "approval":
            self._require(user, "approvals.decide"); body = self._body()
            return self._json(201, service.approve_test_case(parts[1], actor, str(body.get("decision", "")), str(body.get("comment", "")), actor))
        if route == "/api/rebuild-index":
            self._require(user, "admin.storage"); return self._json(200, {"indexed_records": store.rebuild_index()})
        if len(parts) == 1 and parts[0] in CREATE_PERMISSIONS:
            self._require(user, CREATE_PERMISSIONS[parts[0]]); return self._json(201, store.create(parts[0], self._body(), actor))
        raise ApiError(404, "Not found")

    # -------------------------------------------------------------------- PUT
    def _put_api(self, route, parts, query):
        store, service, auth = self.app.store, self.app.service, self.app.auth
        user = self._user(); actor = user["id"]
        if len(parts) == 3 and parts[0] == "folders" and parts[2] == "move":
            self._require(user, "repository.write")
            return self._json(200, service.move_folder(parts[1], str(self._body().get("parent_id", "") or ""), actor))
        if len(parts) == 3 and parts[0] == "test-cases" and parts[2] == "approval-status":
            body = self._body(); target = str(body.get("status", ""))
            self._require(user, "repository.write" if target in {"In Review", "Draft"} and (store.get("test_cases", parts[1]) or {}).get("status") != "Deprecated" else "approvals.decide")
            return self._json(200, service.update_test_case_approval_status(parts[1], target, actor, str(body.get("comment", ""))))
        if len(parts) == 3 and parts[0] == "folders" and parts[2] == "rename":
            self._require(user, "repository.write")
            return self._json(200, service.workflow.rename_folder(parts[1], self._body().get("name"), actor))
        if len(parts) == 3 and parts[0] == "test-cases" and parts[2] == "last-status":
            self._require(user, "repository.write")
            return self._json(200, service.update_test_case_last_status(parts[1], self._body().get("last_status", ""), actor))
        if len(parts) == 3 and parts[0] == "test-runs" and parts[2] == "results":
            self._require(user, "execution.update"); self._require_run_owner(user, parts[1])
            return self._json(200, self.app.execution.save_steps(parts[1], self._body().get("results", []), actor))
        if len(parts) == 3 and parts[0] == "test-runs" and parts[2] == "status":
            self._require(user, "execution.update"); self._require_run_owner(user, parts[1])
            body = self._body()
            return self._json(200, self.app.execution.quick_status(parts[1], str(body.get("status", "")), actor, body.get("base_attempt")))
        if len(parts) == 2 and parts[0] == "users":
            self._require(user, "users.manage"); return self._json(200, public_user(auth.update_user(parts[1], self._body(), user)))
        if len(parts) != 2 or parts[0] not in UPDATE_PERMISSIONS: raise ApiError(404, "Not found")
        entity, record_id = parts
        self._require(user, UPDATE_PERMISSIONS[entity]); body = self._body()
        if entity == "test_cases":
            if "status" in body: raise PermissionDenied("Use the approval workflow to change a test case's approval status")
            existing = store.get("test_cases", record_id)
            if not existing: raise KeyError("Test case not found")
            if ("title" in body and normalize_title(body["title"]) != normalize_title(existing.get("title"))) or ("folder_id" in body and body["folder_id"] != existing.get("folder_id")):
                self._reject_duplicates(body, existing["project_id"], body.get("title", existing.get("title")), str(body.get("folder_id", existing.get("folder_id"))), record_id)
            body.pop("allow_duplicate", None)
            return self._json(200, service.update_test_case(record_id, body, actor, body.pop("version_comment", "Updated")))
        if entity == "test_cycles": return self._json(200, self.app.cycles.update_cycle(record_id, body, actor))
        return self._json(200, store.update(entity, record_id, body, actor))

    # ----------------------------------------------------------------- DELETE
    def _delete_api(self, route, parts, query):
        user = self._user()
        if len(parts) == 2 and parts[0] == "attachments":
            record = self.app.attachments.get(parts[1])
            self._attachment_access(user, record["owner_type"], record["owner_id"], write=True)
            if record["owner_type"] == "test_run" and any(parts[1] in (h.get("attachment_ids") or []) for h in self.app.store.list("execution_history", filters={"test_run_id": record["owner_id"]}, page_size=1_000_000)["items"]):
                raise ValueError("This evidence is part of a saved result and cannot be removed")
            self.app.attachments.archive(parts[1], user["id"])
            return self._json(200, {"archived": True, "id": parts[1]})
        if len(parts) != 2 or parts[0] not in DELETE_PERMISSIONS: raise ApiError(404, "Not found")
        self._require(user, DELETE_PERMISSIONS[parts[0]])
        if parts[0] == "folders": self.app.service.delete_folder(parts[1], user["id"])
        elif parts[0] == "test_runs": self.app.cycles.remove_run(parts[1], user["id"])
        else: self.app.store.delete(parts[0], parts[1], user["id"])
        return self._json(200, {"archived": True, "id": parts[1]})

    def _static(self, route):
        relative = "index.html" if route in {"/", ""} else route.lstrip("/"); target = (WEB / relative).resolve()
        if WEB.resolve() not in target.parents and target != WEB.resolve() or not target.is_file(): self.send_error(404); return
        self._send(200, target.read_bytes(), mimetypes.guess_type(str(target))[0] or "application/octet-stream")


def make_server(host: str, port: int, data_dir: str | Path = DATA, cert: str = "", key: str = "") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.app = App(data_dir)  # type: ignore[attr-defined]
    server.tls = False  # type: ignore[attr-defined]
    if cert and key:  # optional HTTPS with your own certificate
        import ssl
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.tls = True  # type: ignore[attr-defined]
    return server


def is_local_only(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def accounts_with_default_password(app: "App") -> list[str]:
    """Active accounts that still use the shipped default password (unsafe on a network)."""
    from app.auth import verify_password
    return [u.get("email", "") for u in app.store.list("users", page_size=1_000_000)["items"]
            if u.get("status") == "Active" and verify_password("change-me", u.get("password_hash", ""))]


def network_addresses() -> list[str]:
    """IPv4 addresses teammates can use to reach this computer."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:  # no packet is sent
            probe.connect(("10.255.255.255", 1)); found.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in found and not address.startswith(("127.", "169.254.")): found.append(address)
    except OSError:
        pass
    return [a for a in found if not a.startswith("127.")]


if __name__ == "__main__":
    host = resolve_host()
    cert, key = os.getenv("TMEXPERT_CERT", ""), os.getenv("TMEXPERT_KEY", "")
    port = resolve_port(os.getenv("TMEXPERT_PORT"), "127.0.0.1" if is_local_only(host) else host)
    httpd = make_server(host, port, DATA, cert, key)
    scheme = "https" if httpd.tls else "http"  # type: ignore[attr-defined]
    if not is_local_only(host):
        unsafe = accounts_with_default_password(httpd.app)  # type: ignore[attr-defined]
        if unsafe:
            httpd.server_close()
            print("TMExpert was NOT shared on the network.")
            print("These accounts still use the default password 'change-me':", ", ".join(unsafe))
            print("Start TMExpert normally (Start-TMExpert.cmd), sign in as each of them, set a new password, then share again.")
            raise SystemExit(2)
    print(f"TMExpert ready at {scheme}://127.0.0.1:{port}  (on this computer)")
    if not is_local_only(host):
        addresses = network_addresses()
        for address in addresses:
            print(f"Team link: {scheme}://{address}:{port}")
        if not addresses: print("No network address found; check the network connection.")
        if scheme == "http":
            print("Note: shared over plain HTTP. Use this only on a trusted office network or VPN.")
    httpd.serve_forever()
