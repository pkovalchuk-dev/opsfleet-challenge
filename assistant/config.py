"""Configuration: environment, users (the access table), settings and the schema description."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_FALLBACK_MODEL = "gemini-3.1-flash-lite"


@dataclass(frozen=True)
class User:
    id: str
    name: str
    email: str
    role: str  # "manager" | "ceo"
    brands: tuple[str, ...]

    @property
    def is_ceo(self) -> bool:
        return self.role == "ceo"

    @property
    def has_scope(self) -> bool:
        return self.is_ceo or len(self.brands) > 0

    def allows_scope(self, recorded_scope: list[str] | None) -> bool:
        """Stored analytics must fit the current access, even after reassignment."""
        return bool(recorded_scope) and (self.is_ceo or set(recorded_scope).issubset(self.brands))

    @property
    def scope_label(self) -> str:
        if self.is_ceo:
            return "all brands"
        return ", ".join(self.brands) if self.brands else "no brands"


@dataclass(frozen=True)
class Limits:
    repairs: int = 2
    queries: int = 4
    timeout_seconds: float = 120.0


@dataclass
class AppConfig:
    project: str | None
    model: str
    api_key: str | None
    db_path: Path
    limits: Limits
    users: dict[str, User]
    schema_doc: str
    settings: dict
    known_brands: list[str]
    thinking_budget: int | None
    fallback_model: str | None = DEFAULT_FALLBACK_MODEL


def load_users(path: Path, known_brands: list[str] | None = None) -> dict[str, User]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    users: dict[str, User] = {}
    known = set(known_brands or [])
    for user_id, item in (raw.get("users") or {}).items():
        role = str(item.get("role", "manager")).lower()
        if role not in ("manager", "ceo"):
            raise ValueError(f"user {user_id}: role must be manager or ceo, got {role!r}")
        brands = tuple(str(b) for b in (item.get("brands") or []))
        if role == "ceo" and brands:
            raise ValueError(f"user {user_id}: a CEO must not list brands (role grants all brands)")
        if known and role == "manager":
            unknown = [b for b in brands if b not in known]
            if unknown:
                raise ValueError(f"user {user_id}: unknown brand(s) {unknown}; use exact products.brand values")
        users[user_id] = User(
            id=user_id,
            name=str(item.get("name", user_id)),
            email=str(item.get("email", f"{user_id}@example.com")),
            role=role,
            brands=brands,
        )
    if not users:
        raise ValueError(f"no users defined in {path}")
    return users


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {"company": {}, "users": {}}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {"company": raw.get("company") or {}, "users": raw.get("users") or {}}


def resolve_settings(settings: dict, user_id: str) -> dict:
    """Company default, overridden key by key by the user's own settings."""
    resolved = dict(settings.get("company") or {})
    resolved.update((settings.get("users") or {}).get(user_id) or {})
    return resolved


def load_known_brands(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [b for b in json.loads(path.read_text(encoding="utf-8")) if isinstance(b, str) and b]


def load_config(env_file: Path | None = None) -> AppConfig:
    load_dotenv(env_file or ROOT / ".env")
    known_brands = load_known_brands(CONFIG_DIR / "brands.json")
    thinking = os.environ.get("GEMINI_THINKING_BUDGET")
    return AppConfig(
        project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
        model=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL),
        api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"),
        db_path=Path(os.environ.get("ASSISTANT_DB", ROOT / "data" / "assistant.db")),
        limits=Limits(),
        users=load_users(CONFIG_DIR / "users.yaml", known_brands),
        schema_doc=(CONFIG_DIR / "schema.md").read_text(encoding="utf-8"),
        settings=load_settings(CONFIG_DIR / "settings.yaml"),
        known_brands=known_brands,
        thinking_budget=int(thinking) if thinking not in (None, "") else None,
        fallback_model=os.environ.get("GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL).strip() or None,
    )
