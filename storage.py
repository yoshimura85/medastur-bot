"""Encrypted credential and state storage."""
import json
import os
from pathlib import Path
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

CREDS_FILE = DATA_DIR / "credentials.json"
STATE_FILE = DATA_DIR / "slots.json"
USERS_FILE = DATA_DIR / "users.json"


def _get_fernet() -> Fernet:
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        key = Fernet.generate_key().decode()
        env_path = Path(__file__).parent / ".env"
        with open(env_path, "a") as f:
            f.write(f"\nENCRYPTION_KEY={key}\n")
    return Fernet(key.encode() if isinstance(key, str) else key)


def save_credentials(telegram_id: int, username: str, password: str) -> None:
    fernet = _get_fernet()
    data = _load(CREDS_FILE)
    data[str(telegram_id)] = {
        "username": fernet.encrypt(username.encode()).decode(),
        "password": fernet.encrypt(password.encode()).decode(),
    }
    _save(CREDS_FILE, data)


def load_credentials(telegram_id: int) -> tuple[str, str] | None:
    data = _load(CREDS_FILE)
    entry = data.get(str(telegram_id))
    if not entry:
        return None
    fernet = _get_fernet()
    return (fernet.decrypt(entry["username"].encode()).decode(),
            fernet.decrypt(entry["password"].encode()).decode())


def delete_credentials(telegram_id: int) -> None:
    data = _load(CREDS_FILE)
    data.pop(str(telegram_id), None)
    _save(CREDS_FILE, data)


def save_user_config(telegram_id: int, update: dict) -> None:
    data = _load(USERS_FILE)
    data.setdefault(str(telegram_id), {}).update(update)
    _save(USERS_FILE, data)


def load_user_config(telegram_id: int) -> dict:
    return _load(USERS_FILE).get(str(telegram_id), {})


def get_all_monitoring_users() -> list[int]:
    return [int(uid) for uid, cfg in _load(USERS_FILE).items()
            if cfg.get("monitoring")]


def save_known_slots(telegram_id: int, slots: list[dict]) -> None:
    data = _load(STATE_FILE)
    data[str(telegram_id)] = slots
    _save(STATE_FILE, data)


def load_known_slots(telegram_id: int) -> list[dict]:
    return _load(STATE_FILE).get(str(telegram_id), [])


def _load(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
