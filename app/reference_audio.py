from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

METADATA_FILE_NAME = "metadata.json"


@dataclass(frozen=True)
class ReferenceAudioRecord:
    id: str
    path: Path
    expires_at: int


class ReferenceAudioStore:
    def __init__(self, root_dir: Path, signing_key: str, ttl_seconds: int):
        self.root_dir = root_dir
        self._signing_key = signing_key.encode("utf-8")
        self.ttl_seconds = ttl_seconds

    def initialize(self) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cleanup_expired()

    def allocate(self, extension: str) -> tuple[str, Path]:
        audio_id = str(uuid.uuid4())
        audio_dir = self.root_dir / f".upload-{audio_id}"
        audio_dir.mkdir(parents=False, exist_ok=False, mode=0o700)
        return audio_id, audio_dir / f"audio{extension}"

    def commit(self, audio_id: str, audio_path: Path) -> ReferenceAudioRecord:
        expires_at = int(time.time()) + self.ttl_seconds
        final_dir = self.root_dir / audio_id
        metadata_path = audio_path.parent / METADATA_FILE_NAME
        metadata_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "id": audio_id,
                    "file_name": audio_path.name,
                    "expires_at": expires_at,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        audio_path.chmod(0o600)
        metadata_path.chmod(0o600)
        audio_path.parent.rename(final_dir)
        return ReferenceAudioRecord(id=audio_id, path=final_dir / audio_path.name, expires_at=expires_at)

    def get(self, audio_id: str, expires_at: int) -> ReferenceAudioRecord | None:
        record = self._read_record(audio_id)
        if not record or record.expires_at != expires_at or not record.path.is_file():
            return None
        return record

    def sign(self, audio_id: str, expires_at: int) -> str:
        message = f"reference-audio:{audio_id}:{expires_at}".encode()
        return hmac.new(self._signing_key, message, hashlib.sha256).hexdigest()

    def signature_is_valid(self, audio_id: str, expires_at: int, signature: str) -> bool:
        return hmac.compare_digest(self.sign(audio_id, expires_at), signature)

    def delete(self, audio_id: str) -> None:
        if not valid_reference_audio_id(audio_id):
            return
        shutil.rmtree(self.root_dir / audio_id, ignore_errors=True)
        shutil.rmtree(self.root_dir / f".upload-{audio_id}", ignore_errors=True)

    def cleanup_expired(self, now: int | None = None) -> int:
        cutoff = int(time.time()) if now is None else now
        removed = 0
        if not self.root_dir.exists():
            return removed
        for item in self.root_dir.iterdir():
            if item.name.startswith(".upload-") and item.is_dir() and not item.is_symlink():
                try:
                    modified_at = int(item.stat().st_mtime)
                except FileNotFoundError:
                    continue
                if cutoff - modified_at >= max(self.ttl_seconds, 60 * 60):
                    shutil.rmtree(item, ignore_errors=True)
                    removed += 1
                continue
            if item.is_symlink() or not item.is_dir() or not valid_reference_audio_id(item.name):
                _remove_entry(item)
                removed += 1
                continue
            record = self._read_record(item.name)
            if not record or record.expires_at <= cutoff or not record.path.is_file():
                shutil.rmtree(item, ignore_errors=True)
                removed += 1
        return removed

    def _read_record(self, audio_id: str) -> ReferenceAudioRecord | None:
        if not valid_reference_audio_id(audio_id):
            return None
        audio_dir = self.root_dir / audio_id
        try:
            payload = json.loads((audio_dir / METADATA_FILE_NAME).read_text(encoding="utf-8"))
            if payload.get("version") != 1 or payload.get("id") != audio_id:
                return None
            file_name = str(payload.get("file_name", ""))
            expires_at = int(payload["expires_at"])
        except (OSError, UnicodeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if Path(file_name).name != file_name or not file_name.startswith("audio."):
            return None
        return ReferenceAudioRecord(id=audio_id, path=audio_dir / file_name, expires_at=expires_at)


def valid_reference_audio_id(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _remove_entry(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
