"""Authentication, sessions, roles and permissions for TMExpert.

Users stay in core/Users.xlsx.  Passwords are stored as salted PBKDF2 hashes in
the existing ``password_hash`` column; legacy unsalted SHA-256 hashes are still
accepted once and upgraded on the next successful sign-in.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Any

from .storage import ExcelStorageService, utcnow

PBKDF2_ITERATIONS = 240_000
SESSION_IDLE_SECONDS = 12 * 60 * 60
SESSION_MAX_SECONDS = 7 * 24 * 60 * 60
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 5 * 60
MIN_PASSWORD_LENGTH = 8

ADMIN = "Admin"
TEAM = "Team Member"
ROLES = (ADMIN, TEAM)
ROLE_ALIASES = {
    "admin": ADMIN, "administrator": ADMIN, "system administrator": ADMIN,
    "team member": TEAM, "team": TEAM, "tester": TEAM, "qa": TEAM, "qa engineer": TEAM,
}

# Permission catalogue.  New roles can be added by mapping them to a set here.
ROLE_PERMISSIONS: dict[str, set[str]] = {
    ADMIN: {"*"},
    TEAM: {
        "dashboard.read", "projects.read", "repository.read", "requirements.read",
        "plans.read", "cycles.read", "execution.read", "execution.update", "users.directory",
    },
}

SAFE_USER_FIELDS = ("id", "name", "email", "role", "status", "created_at", "updated_at", "archived")


def canonical_role(role: Any) -> str:
    return ROLE_ALIASES.get(str(role or "").strip().casefold(), TEAM)


def has_permission(user: dict[str, Any] | None, permission: str) -> bool:
    if not user: return False
    granted = ROLE_PERMISSIONS.get(canonical_role(user.get("role")), set())
    return "*" in granted or permission in granted


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    safe = {field: user.get(field, "") for field in SAFE_USER_FIELDS}
    safe["role"] = canonical_role(user.get("role"))
    return safe


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    stored = str(stored or "")
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, iterations, salt, digest = stored.split("$", 3)
            candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(candidate, digest)
    if len(stored) == 64:  # legacy unsalted SHA-256
        return hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), stored)
    return False


def validate_new_password(password: Any) -> str:
    password = str(password or "")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if password.casefold() == "change-me":
        raise ValueError("Choose a password other than the default")
    return password


class PermissionDenied(Exception):
    pass


class AuthService:
    def __init__(self, store: ExcelStorageService):
        self.store = store
        self.sessions: dict[str, dict[str, Any]] = {}
        self.failures: dict[str, tuple[int, float]] = {}
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- sessions
    def login(self, email: Any, password: Any) -> tuple[str, dict[str, Any]]:
        email_key = str(email or "").strip().casefold()
        password = str(password or "")
        with self.lock:
            count, until = self.failures.get(email_key, (0, 0.0))
            if until and until > time.time():
                raise PermissionError("Too many failed sign-in attempts. Try again in a few minutes.")
        user = next((u for u in self.store.list("users", page_size=100000)["items"] if str(u.get("email", "")).casefold() == email_key), None)
        if not user or user.get("status") != "Active" or not verify_password(password, user.get("password_hash", "")):
            with self.lock:
                count, _ = self.failures.get(email_key, (0, 0.0))
                count += 1
                self.failures[email_key] = (count, time.time() + LOCKOUT_SECONDS if count >= MAX_FAILED_LOGINS else 0.0)
            if user: self.store.record_event("Sign-in failed", "users", user["id"], {"email": user.get("email")}, user["id"])
            raise PermissionError("Email or password is incorrect")
        with self.lock:
            self.failures.pop(email_key, None)
        if not str(user.get("password_hash", "")).startswith("pbkdf2_sha256$"):
            user = self.store.update("users", user["id"], {"password_hash": hash_password(password)}, user["id"])
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[token] = {"user_id": user["id"], "seen": time.time(), "started": time.time()}
        self.store.record_event("Sign-in", "users", user["id"], {"email": user.get("email")}, user["id"])
        return token, user

    def user_for(self, token: str | None) -> dict[str, Any] | None:
        if not token: return None
        with self.lock:
            session = self.sessions.get(token)
            if not session: return None
            if time.time() - session["seen"] > SESSION_IDLE_SECONDS or time.time() - session.get("started", session["seen"]) > SESSION_MAX_SECONDS:
                self.sessions.pop(token, None); return None
            session["seen"] = time.time()
            user_id = session["user_id"]
        user = self.store.get("users", user_id)
        if not user or user.get("status") != "Active" or user.get("archived") is True:
            self.logout(token); return None
        return user

    def logout(self, token: str | None) -> None:
        if not token: return
        with self.lock:
            self.sessions.pop(token, None)

    def _revoke_user_sessions(self, user_id: str, keep: str | None = None) -> None:
        with self.lock:
            for token in [t for t, s in self.sessions.items() if s["user_id"] == user_id and t != keep]:
                self.sessions.pop(token, None)

    # ------------------------------------------------------------- user admin
    def change_password(self, user: dict[str, Any], current: Any, new: Any, token: str | None) -> dict[str, Any]:
        if not verify_password(str(current or ""), user.get("password_hash", "")):
            raise ValueError("Current password is incorrect")
        new = validate_new_password(new)
        updated = self.store.update("users", user["id"], {"password_hash": hash_password(new), "must_change_password": False}, user["id"])
        self._revoke_user_sessions(user["id"], keep=token)
        return updated

    def _active_admin_ids(self) -> set[str]:
        return {u["id"] for u in self.store.list("users", page_size=100000)["items"] if u.get("status") == "Active" and canonical_role(u.get("role")) == ADMIN}

    def create_user(self, data: dict[str, Any], actor_id: str) -> dict[str, Any]:
        name = str(data.get("name", "")).strip(); email = str(data.get("email", "")).strip()
        role = str(data.get("role", TEAM))
        if role not in ROLES: raise ValueError("Role must be Admin or Team Member")
        if not name or "@" not in email: raise ValueError("Enter a name and a valid email address")
        password = validate_new_password(data.get("password"))
        return self.store.create("users", {"name": name, "email": email, "role": role, "status": "Active", "password_hash": hash_password(password), "must_change_password": True}, actor_id)

    def update_user(self, user_id: str, data: dict[str, Any], actor: dict[str, Any]) -> dict[str, Any]:
        target = self.store.get("users", user_id)
        if not target: raise KeyError("User not found")
        changes: dict[str, Any] = {}
        if "name" in data:
            name = str(data["name"]).strip()
            if not name: raise ValueError("Name is required")
            changes["name"] = name
        if "email" in data:
            email = str(data["email"]).strip()
            if "@" not in email: raise ValueError("Enter a valid email address")
            changes["email"] = email
        if "role" in data:
            if data["role"] not in ROLES: raise ValueError("Role must be Admin or Team Member")
            changes["role"] = data["role"]
        if "status" in data:
            if data["status"] not in {"Active", "Inactive"}: raise ValueError("Status must be Active or Inactive")
            changes["status"] = data["status"]
        demoted = changes.get("role", canonical_role(target.get("role"))) != ADMIN or changes.get("status", target.get("status")) != "Active"
        if demoted and target["id"] in self._active_admin_ids() and len(self._active_admin_ids()) == 1:
            raise ValueError("At least one active Admin is required")
        updated = self.store.update("users", user_id, changes, actor["id"])
        if updated.get("status") != "Active": self._revoke_user_sessions(user_id)
        return updated

    def reset_password(self, user_id: str, password: Any, actor_id: str) -> dict[str, Any]:
        if not self.store.get("users", user_id): raise KeyError("User not found")
        password = validate_new_password(password)
        updated = self.store.update("users", user_id, {"password_hash": hash_password(password), "must_change_password": True}, actor_id)
        self._revoke_user_sessions(user_id)
        return updated
