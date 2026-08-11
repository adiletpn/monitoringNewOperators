"""Генерирует operators.yml из GET /users?with=status (Kcell).

Запуск: python -m tools.sync_operators

Состав сотрудников (login/ext/role) всегда берётся из живого API — не
редактируй эти поля в operators.yml руками, перезапускай скрипт.

При ПЕРВОЙ генерации (когда operators.yml ещё не существует) проект и tg
подтягиваются из старого Sipuni-конфига operators_config.py по совпадению
имени (первое слово, регистронезависимо, только при однозначном совпадении
в обе стороны). Сотрудник, которого нет в Kcell, в operators.yml не
попадает — считается уволенным.

При ПОВТОРНОЙ генерации всё, что уже стоит в operators.yml (project, tg,
monitored, legacy.sipuni_id), сохраняется как есть — скрипт руками
никогда не трогает уже подтверждённые поля, только обновляет
login/email/extension/role из живого API и добавляет новых сотрудников.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from typing import Dict, List, Tuple

import yaml

from config import Config
from providers.kcell import KcellProvider


def _token(name: str) -> str:
    name = (name or "").strip()
    return name.split()[0].lower() if name else ""


def _load_existing(path: str) -> Dict[str, dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    entries = data.get("operators", []) if isinstance(data, dict) else (data or [])
    return {entry["op_id"]: entry for entry in entries if "op_id" in entry}


def _load_legacy() -> Tuple[dict, dict]:
    try:
        from operators_config import OPERATORS, PROJECT_ROPS
        return OPERATORS, PROJECT_ROPS
    except ImportError:
        return {}, {}


def build_entries(
    employees: List[dict],
    existing: Dict[str, dict],
    legacy: dict,
) -> Tuple[List[dict], List[str], List[str], List[str]]:
    kcell_by_token = defaultdict(list)
    for e in employees:
        kcell_by_token[_token(e.get("name") or e.get("login") or "")].append(e)

    legacy_by_token = defaultdict(list)
    for name, data in legacy.items():
        legacy_by_token[_token(name)].append((name, data))

    entries: List[dict] = []
    transferred: List[str] = []
    new_without_project: List[str] = []
    matched_legacy_names = set()

    for emp in sorted(employees, key=lambda e: (e.get("login") or "")):
        login = emp.get("login") or ""
        if not login:
            continue
        role = emp.get("role") or ""
        token = _token(emp.get("name") or login)
        prev = existing.get(login, {})

        legacy_match = None
        if len(legacy_by_token.get(token) or []) == 1 and len(kcell_by_token.get(token) or []) == 1:
            legacy_match = legacy_by_token[token][0]

        if prev:
            project = prev.get("project", "")
            tg = prev.get("tg", "")
            legacy_sipuni_id = (prev.get("legacy") or {}).get("sipuni_id", "")
        elif legacy_match:
            legacy_name, legacy_data = legacy_match
            project = legacy_data.get("project", "")
            tg = legacy_data.get("tg", "")
            legacy_sipuni_id = str(legacy_data.get("id", "") or "")
            matched_legacy_names.add(legacy_name)
            transferred.append(f"{legacy_name} -> {login} ({project or 'без проекта'})")
        else:
            project = ""
            tg = ""
            legacy_sipuni_id = ""
            new_without_project.append(f"{login} ({emp.get('name') or '—'})")

        monitored = prev.get("monitored")
        if monitored is None:
            monitored = role != "admin"  # админов кабинета по умолчанию не мониторим

        entry = {
            "name": emp.get("name") or login,
            "op_id": login,
            "project": project,
            "tg": tg,
            "monitored": monitored,
            "kcell": {
                "login": login,
                "email": emp.get("email") or "",
                "extension": emp.get("ext") or "",
                "role": role,
            },
            "legacy": {"sipuni_id": legacy_sipuni_id} if legacy_sipuni_id else {},
        }
        if role == "admin":
            entry["note"] = "role=admin в Kcell — по умолчанию не мониторим, реши сам(а), нужно ли"
        entries.append(entry)

    fired = sorted(name for name in legacy if name not in matched_legacy_names)
    return entries, transferred, fired, new_without_project


def main() -> int:
    cfg = Config()
    provider = KcellProvider(cfg, {})
    employees, err = provider.fetch_employees()
    if err:
        print(f"[sync_operators] ERROR: {err}", file=sys.stderr)
        return 1
    if not employees:
        print("[sync_operators] ERROR: /users вернул пустой список", file=sys.stderr)
        return 1

    existing = _load_existing(cfg.kcell_operators_yml)
    legacy_operators, legacy_project_rops = _load_legacy()
    entries, transferred, fired, new_without_project = build_entries(employees, existing, legacy_operators)

    out = {"operators": entries, "project_rops": legacy_project_rops}
    with open(cfg.kcell_operators_yml, "w", encoding="utf-8") as f:
        yaml.safe_dump(out, f, allow_unicode=True, sort_keys=False)

    monitored_count = sum(1 for e in entries if e.get("monitored", True))
    print(f"[sync_operators] wrote {cfg.kcell_operators_yml}: {len(entries)} сотрудников, {monitored_count} мониторится")

    print("\nПеренесены из старого конфига:")
    for line in transferred:
        print(f"  {line}")
    if not transferred:
        print("  (никого)")

    print("\nУволены (были в operators_config.py, нет в Kcell):")
    for name in fired:
        print(f"  {name}")
    if not fired:
        print("  (никого)")

    print("\nНовые в Kcell без проекта/tg — заполнить вручную в operators.yml:")
    for line in new_without_project:
        print(f"  {line}")
    if not new_without_project:
        print("  (никого)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
