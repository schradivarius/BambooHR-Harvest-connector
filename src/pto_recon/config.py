"""Configuration loading and startup validation.

Everything is read from the environment (populated from ``.env`` in local use,
from App Settings on Azure). A missing or malformed value fails at startup
with a message naming the variable -- never mid-run, half way through a set of
balance adjustments.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised for any missing or unusable configuration value."""


REQUIRED_VARS = (
    "BAMBOO_SUBDOMAIN",
    "BAMBOO_API_KEY",
    "HARVEST_ACCOUNT_ID",
    "HARVEST_TOKEN",
)


@dataclass(frozen=True)
class Config:
    bamboo_subdomain: str
    bamboo_api_key: str
    harvest_account_id: str
    harvest_token: str

    timeoff_types: tuple[str, ...]
    harvest_task_names: tuple[str, ...]
    harvest_project_names: tuple[str, ...]
    task_type_map: dict[str, str]

    tolerance_hours: Decimal
    max_auto_credit_hours: Decimal
    max_auto_debit_hours: Decimal

    ledger_path: Path
    log_dir: Path
    report_dir: Path

    http_timeout_seconds: float
    http_max_retries: int
    http_backoff_seconds: float
    http_user_agent: str

    @property
    def bamboo_base(self) -> str:
        return f"https://{self.bamboo_subdomain}.bamboohr.com/api/v1"

    @property
    def harvest_base(self) -> str:
        return "https://api.harvestapp.com/v2"

    def bamboo_type_for_harvest_task(self, task_name: str) -> str | None:
        """Which BambooHR balance a Harvest task's hours count against."""
        return self.task_type_map.get(task_name.strip().lower())


def _csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _decimal(env: dict[str, str], name: str, default: str) -> Decimal:
    raw = env.get(name, "").strip() or default
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < 0:
        raise ConfigError(f"{name} must not be negative, got {value}")
    return value


def _int(env: dict[str, str], name: str, default: str) -> int:
    raw = env.get(name, "").strip() or default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc


def _float(env: dict[str, str], name: str, default: str) -> float:
    raw = env.get(name, "").strip() or default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _parse_task_type_map(raw: str | None) -> dict[str, str]:
    """Parse ``harvest task=bamboo type`` pairs into a lookup."""
    mapping: dict[str, str] = {}
    for pair in _csv(raw):
        if "=" not in pair:
            raise ConfigError(
                f"TASK_TYPE_MAP entry {pair!r} is not in 'harvest task=bamboo type' form"
            )
        harvest_task, bamboo_type = pair.split("=", 1)
        harvest_task, bamboo_type = harvest_task.strip(), bamboo_type.strip()
        if not harvest_task or not bamboo_type:
            raise ConfigError(f"TASK_TYPE_MAP entry {pair!r} has an empty side")
        mapping[harvest_task.lower()] = bamboo_type
    return mapping


def load_config(env_file: str | os.PathLike[str] | None = None) -> Config:
    """Load and validate configuration, or raise ``ConfigError``."""
    if env_file is not None:
        load_dotenv(env_file, override=False)
    else:
        load_dotenv(override=False)

    env = dict(os.environ)

    missing = [name for name in REQUIRED_VARS if not env.get(name, "").strip()]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ".\nCopy .env.example to .env and fill them in, or set them as "
            "App Settings if running on Azure."
        )

    timeoff_types = _csv(env.get("TIMEOFF_TYPES")) or ("PTO",)
    harvest_task_names = _csv(env.get("HARVEST_TASK_NAMES")) or ("PTO",)
    task_type_map = _parse_task_type_map(env.get("TASK_TYPE_MAP"))

    # Default the map only in the unambiguous single-type case. With more than
    # one type, guessing which Harvest task feeds which balance is exactly the
    # kind of silent wrong answer this tool exists to prevent.
    if not task_type_map:
        if len(timeoff_types) == 1 and len(harvest_task_names) == 1:
            task_type_map = {harvest_task_names[0].lower(): timeoff_types[0]}
        else:
            raise ConfigError(
                "TASK_TYPE_MAP is required when TIMEOFF_TYPES or "
                "HARVEST_TASK_NAMES lists more than one entry. Set it to "
                "comma-separated 'harvest task=bamboo type' pairs so the tool "
                "knows which balance each Harvest task counts against."
            )

    unmapped = [t for t in harvest_task_names if t.lower() not in task_type_map]
    if unmapped:
        raise ConfigError(
            "These HARVEST_TASK_NAMES have no TASK_TYPE_MAP entry, so their "
            f"hours could not be attributed to a balance: {', '.join(unmapped)}"
        )

    known_types = {t.lower() for t in timeoff_types}
    stray = sorted(
        {v for v in task_type_map.values() if v.lower() not in known_types}
    )
    if stray:
        raise ConfigError(
            "TASK_TYPE_MAP points at BambooHR type(s) missing from "
            f"TIMEOFF_TYPES: {', '.join(stray)}"
        )

    tolerance = _decimal(env, "TOLERANCE_HOURS", "0.01")
    max_credit = _decimal(env, "MAX_AUTO_CREDIT_HOURS", "4.0")
    max_debit = _decimal(env, "MAX_AUTO_DEBIT_HOURS", "2.0")

    config = Config(
        bamboo_subdomain=env["BAMBOO_SUBDOMAIN"].strip(),
        bamboo_api_key=env["BAMBOO_API_KEY"].strip(),
        harvest_account_id=env["HARVEST_ACCOUNT_ID"].strip(),
        harvest_token=env["HARVEST_TOKEN"].strip(),
        timeoff_types=timeoff_types,
        harvest_task_names=harvest_task_names,
        harvest_project_names=_csv(env.get("HARVEST_PROJECT_NAMES")),
        task_type_map=task_type_map,
        tolerance_hours=tolerance,
        max_auto_credit_hours=max_credit,
        max_auto_debit_hours=max_debit,
        ledger_path=Path(env.get("LEDGER_PATH", "./data/ledger.db")).expanduser(),
        log_dir=Path(env.get("LOG_DIR", "./data/logs")).expanduser(),
        report_dir=Path(env.get("REPORT_DIR", "./data/reports")).expanduser(),
        http_timeout_seconds=_float(env, "HTTP_TIMEOUT_SECONDS", "30"),
        http_max_retries=_int(env, "HTTP_MAX_RETRIES", "5"),
        http_backoff_seconds=_float(env, "HTTP_BACKOFF_SECONDS", "1.0"),
        http_user_agent=env.get(
            "HTTP_USER_AGENT", "PTO Reconciliation (contact: IT)"
        ).strip(),
    )

    if config.max_auto_credit_hours < config.tolerance_hours:
        raise ConfigError(
            "MAX_AUTO_CREDIT_HOURS is below TOLERANCE_HOURS, so no credit "
            "could ever be applied. Check both values."
        )
    return config


def redacted(config: Config) -> dict[str, object]:
    """Config safe to write into a log file -- secrets replaced with lengths."""
    return {
        "bamboo_subdomain": config.bamboo_subdomain,
        "bamboo_api_key": f"<redacted len={len(config.bamboo_api_key)}>",
        "harvest_account_id": config.harvest_account_id,
        "harvest_token": f"<redacted len={len(config.harvest_token)}>",
        "timeoff_types": list(config.timeoff_types),
        "harvest_task_names": list(config.harvest_task_names),
        "harvest_project_names": list(config.harvest_project_names),
        "task_type_map": config.task_type_map,
        "tolerance_hours": str(config.tolerance_hours),
        "max_auto_credit_hours": str(config.max_auto_credit_hours),
        "max_auto_debit_hours": str(config.max_auto_debit_hours),
        "ledger_path": str(config.ledger_path),
    }
