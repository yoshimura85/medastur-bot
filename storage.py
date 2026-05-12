"""Encrypted credential and JSON state storage."""
import json
import os
from pathlib import Path
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

_CREDS   = DATA_DIR / "credentials.json"
_USERS   = DATA_DIR / "users.json"
_EARLIEST = DATA_DIR / "earliest.json"   # earliest slot per doctor per user


# ── Encryption ────────────────────────────────────────────────────────────────

def _fernet() -> Fernet:
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        key = Fernet.generate_key().decode()
        with open(Path(__file__).parent / ".env", "a") as f:
            f.write(f"\nENCRYPTION_KEY={key}\n")
    return Fernet(key.encode() if isinstance(key, str) else key)


# ── Credentials ───────────────────────────────────────────────────────────────

def save_credentials(tid: int, username: str, password: str) -> None:
    fn = _fernet()
    data = _load(_CREDS)
    data[str(tid)] = {
        "username": fn.encrypt(username.encode()).decode(),
        "password": fn.encrypt(password.encode()).decode(),
    }
    _save(_CREDS, data)


def load_credentials(tid: int) -> tuple[str, str] | None:
    entry = _load(_CREDS).get(str(tid))
    if not entry:
        return None
    fn = _fernet()
    return (fn.decrypt(entry["username"].encode()).decode(),
            fn.decrypt(entry["password"].encode()).decode())


def delete_credentials(tid: int) -> None:
    data = _load(_CREDS)
    data.pop(str(tid), None)
    _save(_CREDS, data)


# ── User config ───────────────────────────────────────────────────────────────

def save_user_config(tid: int, update: dict) -> None:
    data = _load(_USERS)
    data.setdefault(str(tid), {}).update(update)
    _save(_USERS, data)


def load_user_config(tid: int) -> dict:
    return _load(_USERS).get(str(tid), {})


def get_all_monitoring_users() -> list[int]:
    return [int(uid) for uid, cfg in _load(_USERS).items() if cfg.get("monitoring")]


# ── Earliest slots per doctor ─────────────────────────────────────────────────

def save_earliest(tid: int, earliest: dict[str, dict]) -> None:
    """earliest = {doctor_name: slot.to_dict()}"""
    data = _load(_EARLIEST)
    data[str(tid)] = earliest
    _save(_EARLIEST, data)


def load_earliest(tid: int) -> dict[str, dict]:
    """Returns {doctor_name: slot_dict} or {} if not set."""
    return _load(_EARLIEST).get(str(tid), {})


def clear_earliest(tid: int) -> None:
    data = _load(_EARLIEST)
    data.pop(str(tid), None)
    _save(_EARLIEST, data)


# ── JSON helpers ──────────────────────────────────────────────────────────────

def _load(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
