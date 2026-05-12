"""Encrypted credential and state storage using JSON files."""
import json
import os
from pathlib import Path
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

CREDS_FILE = DATA_DIR / "credentials.json"
STATE_FILE = DATA_DIR / "state.json"
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
    data = _load_json(CREDS_FILE)
    data[str(telegram_id)] = {
        "username": fernet.encrypt(username.encode()).decode(),
        "password": fernet.encrypt(password.encode()).decode(),
    }
    _save_json(CREDS_FILE, data)


def load_credentials(telegram_id: int) -> tuple[str, str] | None:
    data = _load_json(CREDS_FILE)
    entry = data.get(str(telegram_id))
    if not entry:
        return None
    fernet = _get_fernet()
    username = fernet.decrypt(entry["username"].encode()).decode()
    password = fernet.decrypt(entry["password"].encode()).decode()
    return username, password


def delete_credentials(telegram_id: int) -> None:
    data = _load_json(CREDS_FILE)
    data.pop(str(telegram_id), None)
    _save_json(CREDS_FILE, data)


def save_user_config(telegram_id: int, config: dict) -> None:
    data = _load_json(USERS_FILE)
    data.setdefault(str(telegram_id), {}).update(config)
    _save_json(USERS_FILE, data)


def load_user_config(telegram_id: int) -> dict:
    data = _load_json(USERS_FILE)
    return data.get(str(telegram_id), {})


def get_all_monitoring_users() -> list[int]:
    data = _load_json(USERS_FILE)
    return [
        int(uid)
        for uid, cfg in data.items()
        if cfg.get("monitoring", False)
    ]


def save_known_appointments(telegram_id: int, appointments: list[dict]) -> None:
    data = _load_json(STATE_FILE)
    data[str(telegram_id)] = appointments
    _save_json(STATE_FILE, data)


def load_known_appointments(telegram_id: int) -> list[dict]:
    data = _load_json(STATE_FILE)
    return data.get(str(telegram_id), [])


def _load_json(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
