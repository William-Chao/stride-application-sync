#!/usr/bin/env python3
"""Log in to Speediance with email/password and save user id + token to .env.

Reads SPEEDIANCE_EMAIL, SPEEDIANCE_PASSWORD and SPEEDIANCE_REGION (Global|EU)
from the environment only. Never takes credentials on the command line.

Calls POST /api/app/v2/login/verifyIdentity, then POST /api/app/v2/login/byPass
(flow from the MIT-licensed hbui3/UnofficialSpeedianceWorkoutManager, see
NOTICE.md). On success it writes SPEEDIANCE_USER_ID, SPEEDIANCE_TOKEN,
SPEEDIANCE_REGION and SPEEDIANCE_UNIT into the pipeline .env (mode 600).
The password and email are never written or printed.

Exit codes: 0 connected, 1 auth/login rejected, 2 bad input, 3 network/server error.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from scripts.constants import ROOT
from scripts.speediance_client import (
    DEFAULT_VERSIONCODE,
    REGION_HOSTS,
    extract_weight_unit,
    normalize_region,
)
from scripts.timeutil import now_la, utc_offset_hhmm

ENV_PATH = ROOT / ".env"


def _headers(host: str) -> dict[str, str]:
    moment = now_la()
    return {
        "Host": host,
        "Timestamp": str(int(moment.timestamp() * 1000)),
        "Versioncode": DEFAULT_VERSIONCODE,
        "Content-Type": "application/json",
        "User-Agent": "Dart/3.9 (dart:io)",
        "App_type": "SOFTWARE",
        "Timezone": "America/Los_Angeles",
        "Utc_offset": utc_offset_hhmm(moment),
        "Accept-Language": "en",
    }


def _post(session: requests.Session, host: str, path: str, body: dict) -> tuple[int, dict]:
    try:
        resp = session.post(f"https://{host}{path}", json=body, headers=_headers(host), timeout=30)
    except requests.RequestException as exc:
        print(f"Speediance request failed ({exc.__class__.__name__}).", file=sys.stderr)
        sys.exit(3)
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    return resp.status_code, payload if isinstance(payload, dict) else {}


def _find(data, key: str, depth: int = 0):
    if not isinstance(data, dict) or depth > 4:
        return None
    if data.get(key) not in (None, ""):
        return data[key]
    for value in data.values():
        found = _find(value, key, depth + 1)
        if found not in (None, ""):
            return found
    return None


def write_env(values: dict[str, str], path: Path = ENV_PATH) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    seen: set[str] = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in seen]
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def main() -> int:
    email = os.environ.get("SPEEDIANCE_EMAIL", "").strip()
    password = os.environ.get("SPEEDIANCE_PASSWORD", "")
    if not email or not password:
        print("Set SPEEDIANCE_EMAIL and SPEEDIANCE_PASSWORD in the environment.", file=sys.stderr)
        return 2
    try:
        region = normalize_region(os.environ.get("SPEEDIANCE_REGION", "Global"))
    except Exception:
        print("SPEEDIANCE_REGION must be Global or EU.", file=sys.stderr)
        return 2
    host = REGION_HOSTS[region.lower()]
    session = requests.Session()

    status, payload = _post(session, host, "/api/app/v2/login/verifyIdentity",
                            {"type": 2, "userIdentity": email})
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if status >= 500:
        print(f"Speediance server error (HTTP {status}).", file=sys.stderr)
        return 3
    if status != 200:
        print(f"Login failed at identity check (HTTP {status}).", file=sys.stderr)
        return 1
    if data.get("isExist") is False:
        print("No Speediance account for that email in this region. Register in the official app or switch region.", file=sys.stderr)
        return 1
    if data.get("hasPwd") is False:
        print("Account has no password set. Set one in the official Speediance app.", file=sys.stderr)
        return 1

    status, payload = _post(session, host, "/api/app/v2/login/byPass",
                            {"userIdentity": email, "password": password, "type": 2})
    password = ""  # drop the reference as soon as it's sent
    if status >= 500:
        print(f"Speediance server error (HTTP {status}).", file=sys.stderr)
        return 3
    data = payload.get("data")
    token = _find(data, "token")
    user_id = _find(data, "appUserId")
    if status != 200 or not token or not user_id:
        code = payload.get("code")
        print(f"Login rejected (HTTP {status}, code {code}). Check email, password, and region.", file=sys.stderr)
        return 1

    unit_name = extract_weight_unit(data) if isinstance(data, dict) else None
    if unit_name in ("kg", "lb"):
        unit = "1" if unit_name == "lb" else "0"
    else:
        unit = os.environ.get("SPEEDIANCE_UNIT", "0").strip() or "0"
    write_env({
        "SPEEDIANCE_USER_ID": str(user_id),
        "SPEEDIANCE_TOKEN": str(token),
        "SPEEDIANCE_REGION": region,
        "SPEEDIANCE_UNIT": unit,
    })
    print("Connected.")
    print(f"Region: {region}")
    print(f"Unit: {'lb' if unit == '1' else 'kg'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
