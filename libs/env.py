"""Environment variable helpers."""

from __future__ import annotations

import os


def read_int_env(name: str, fallback: int) -> int:
    value = os.getenv(name)
    if not value:
        return fallback
    try:
        return int(value)
    except ValueError:
        return fallback


def read_bool_env(name: str, fallback: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return fallback
    return value.strip().lower() not in {"0", "false", "no", "off"}


def read_float_env(name: str, fallback: float) -> float:
    value = os.getenv(name)
    if not value:
        return fallback
    try:
        return float(value)
    except ValueError:
        return fallback
