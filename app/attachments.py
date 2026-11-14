"""Evidence and attachment files.

File bytes live under application-data/attachments/<xx>/<id><ext>; the metadata
row (who, when, what, checksum) lives in execution/Attachments.xlsx.  Only an
allow-list of file types is accepted, the content must match the extension, and
the stored content type always comes from the extension, never from the client.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import uuid
import zipfile
import io
from pathlib import Path
from typing import Any

from .storage import ExcelStorageService, _retry_io

MAX_BYTES = 10 * 1024 * 1024
OWNER_TYPES = {"test_run", "test_case"}
ALLOWED: dict[str, str] = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".pdf": "application/pdf", ".txt": "text/plain", ".log": "text/plain", ".csv": "text/csv", ".json": "application/json",
    ".har": "application/json", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".zip": "application/zip",
    ".mp4": "video/mp4", ".webm": "video/webm",
}
INLINE = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_UNSAFE_NAME = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')


def _content_matches(ext: str, data: bytes) -> bool:
    head = data[:16]
    if ext == ".png": return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in {".jpg", ".jpeg"}: return head.startswith(b"\xff\xd8\xff")
    if ext == ".gif": return head.startswith((b"GIF87a", b"GIF89a"))
    if ext == ".webp": return head[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if ext == ".pdf": return head.startswith(b"%PDF")
    if ext in {".zip", ".xlsx", ".docx"}:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive: archive.namelist()
            return True
        except zipfile.BadZipFile:
            return False
    if ext == ".mp4": return data[4:8] == b"ftyp"
    if ext == ".webm": return head.startswith(b"\x1a\x45\xdf\xa3")
    # Text formats: must decode as text and must not be an HTML/SVG document in disguise.
    try: text = data[:4096].decode("utf-8")
    except UnicodeDecodeError: return False
    return not re.match(r"\s*<(!doctype|html|svg|script)", text, re.IGNORECASE)


def clean_filename(name: Any) -> str:
    name = os.path.basename(str(name or "").replace("\\", "/")).strip()
    name = _UNSAFE_NAME.sub("_", name).strip(" .")
    stem, ext = os.path.splitext(name)
    return (stem[:120] or "file") + ext.lower()[:10]


class AttachmentService:
    def __init__(self, store: ExcelStorageService):
        self.store = store
        self.root = store.root / "attachments"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, record: dict[str, Any]) -> Path:
        stored = str(record.get("stored_name", ""))
        if not re.fullmatch(r"[0-9a-f]{2}/[0-9a-f-]{36}\.[a-z0-9]{2,5}", stored): raise ValueError("Attachment file reference is invalid")
        return self.root / stored

    def upload(self, owner_type: str, owner_id: str, project_id: str, filename: Any, encoded: Any, actor_id: str) -> dict[str, Any]:
        if owner_type not in OWNER_TYPES: raise ValueError("Unknown attachment owner")
        name = clean_filename(filename)
        ext = os.path.splitext(name)[1]
        if ext not in ALLOWED:
            raise ValueError("This file type is not allowed. Use images (PNG, JPG, GIF, WebP), PDF, text/log/CSV/JSON/HAR, Excel, Word, ZIP, MP4 or WebM.")
        if not isinstance(encoded, str): raise ValueError("The uploaded file is invalid")
        try: data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc: raise ValueError("The uploaded file is invalid") from exc
        if not data: raise ValueError("The file is empty")
        if len(data) > MAX_BYTES: raise ValueError("Files must be 10 MB or smaller")
        if not _content_matches(ext, data): raise ValueError(f"The file content does not match its {ext} extension")
        attachment_id = str(uuid.uuid4())
        stored = f"{attachment_id[:2]}/{attachment_id}{ext}"
        target = self.root / stored
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".part")
        try:
            temporary.write_bytes(data)
            _retry_io(lambda: os.replace(temporary, target))
        finally:
            temporary.unlink(missing_ok=True)
        try:
            return self.store.create("attachments", {
                "id": attachment_id, "project_id": project_id, "owner_type": owner_type, "owner_id": owner_id, "filename": name,
                "content_type": ALLOWED[ext], "size": str(len(data)), "sha256": hashlib.sha256(data).hexdigest(), "stored_name": stored,
                "uploaded_by": actor_id if self.store.get("users", actor_id) else "",
            }, actor_id)
        except Exception:
            target.unlink(missing_ok=True)  # no orphan file without its record
            raise

    def get(self, attachment_id: str) -> dict[str, Any]:
        record = self.store.get("attachments", attachment_id, include_archived=False)
        if not record: raise KeyError("Attachment not found")
        return record

    def read(self, record: dict[str, Any]) -> bytes:
        path = self._path(record)
        if not path.is_file(): raise FileNotFoundError("Attachment file not found")
        data = _retry_io(path.read_bytes)
        if hashlib.sha256(data).hexdigest() != record.get("sha256"): raise ValueError("The attachment file has been altered and cannot be served")
        return data

    def list(self, owner_type: str, owner_id: str) -> list[dict[str, Any]]:
        names = {u["id"]: u.get("name", "") for u in self.store.list("users", page_size=1_000_000, include_archived=True)["items"]}
        rows = self.store.list("attachments", filters={"owner_type": owner_type, "owner_id": owner_id}, page_size=1_000_000)["items"]
        return [{**self.public(r), "uploaded_by_name": names.get(r.get("uploaded_by", ""), "") or "—"} for r in sorted(rows, key=lambda r: str(r.get("created_at", "")))]

    @staticmethod
    def public(record: dict[str, Any]) -> dict[str, Any]:
        return {k: record.get(k, "") for k in ("id", "owner_type", "owner_id", "filename", "content_type", "size", "uploaded_by", "created_at")} | {"inline": os.path.splitext(record.get("filename", ""))[1].lower() in INLINE}

    def archive(self, attachment_id: str, actor_id: str) -> None:
        self.store.delete("attachments", self.get(attachment_id)["id"], actor_id)
