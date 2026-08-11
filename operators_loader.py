from __future__ import annotations

from typing import Dict, List

import yaml


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_operators_yaml(path: str = "operators.yml", include_unmonitored: bool = False) -> Dict[str, Dict]:
    """Читает operators.yml и возвращает словарь в формате, ожидаемом
    MonitorService: {name: {"id": op_id, "tg": ..., "project": ..., ...}}.

    op_id = login в Kcell — строгий ключ матчинга, без эвристик по имени.
    Операторы с monitored: false (админы кабинета) по умолчанию исключаются
    из словаря целиком.
    """
    raw = _read(path)
    entries = raw.get("operators", []) if isinstance(raw, dict) else (raw or [])

    operators: Dict[str, Dict] = {}
    for entry in entries:
        if not include_unmonitored and entry.get("monitored", True) is False:
            continue
        name = entry["name"]
        operators[name] = {
            "id": str(entry["op_id"]),
            "tg": entry.get("tg", ""),
            "monitored": entry.get("monitored", True),
            "project": entry.get("project", ""),
            "kcell": entry.get("kcell", {}) or {},
        }
    return operators


def load_all_operator_entries(path: str = "operators.yml") -> List[Dict]:
    raw = _read(path)
    return raw.get("operators", []) if isinstance(raw, dict) else (raw or [])


def load_project_rops(path: str = "operators.yml") -> Dict[str, str]:
    """Читает верхнеуровневый словарь project_rops (проект -> @handle РОПа)."""
    raw = _read(path)
    return raw.get("project_rops", {}) if isinstance(raw, dict) else {}
