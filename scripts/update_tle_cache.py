#!/usr/bin/env python3
"""Update a small public TLE cache from CelesTrak.

The script intentionally uses only Python's standard library so it can run on
GitHub Actions without extra dependencies.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
CATALOG_DIR = DOCS_DIR / "catalog"
TRACKED_SATS_FILE = ROOT / "tracked_sats.json"

USER_AGENT = "SatelliteMap-TLE-Cache/1.0 (+https://github.com/)"
REQUEST_TIMEOUT_SECONDS = 60
MIN_SUCCESS_INTERVAL_SECONDS = 2 * 60 * 60

GROUP_CATALOGS = {
    "active": "https://celestrak.org/NORAD/elements/gp.php?GROUP=ACTIVE&FORMAT=TLE",
    # STARLINK is optional for the first emergency rollout. Leave it enabled if
    # your client actually needs it; otherwise remove it from this dictionary.
    "starlink": "https://celestrak.org/NORAD/elements/gp.php?GROUP=STARLINK&FORMAT=TLE",
}


class TleValidationError(ValueError):
    pass


class TleFetchError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise TleFetchError(f"Unexpected HTTP status {status}.")
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raise TleFetchError(f"HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise TleFetchError(str(exc.reason)) from exc


def normalize_tle_text(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [line for line in lines if line.strip()]
    return "\n".join(lines) + "\n"


def validate_tle_text(text: str) -> int:
    normalized = normalize_tle_text(text)
    lines = normalized.splitlines()

    if len(lines) < 3:
        raise TleValidationError("TLE content has fewer than 3 non-empty lines.")
    if len(lines) % 3 != 0:
        raise TleValidationError(f"TLE content has {len(lines)} lines; expected a multiple of 3.")

    satellite_count = len(lines) // 3
    for index in range(0, len(lines), 3):
        name = lines[index].strip()
        line1 = lines[index + 1].rstrip()
        line2 = lines[index + 2].rstrip()

        if not name:
            raise TleValidationError(f"Satellite name is empty at record {index // 3 + 1}.")
        if not line1.startswith("1 "):
            raise TleValidationError(f"Line 1 is invalid for {name!r}: {line1[:40]!r}")
        if not line2.startswith("2 "):
            raise TleValidationError(f"Line 2 is invalid for {name!r}: {line2[:40]!r}")
        if len(line1) < 69 or len(line2) < 69:
            raise TleValidationError(f"TLE lines are too short for {name!r}.")

        catnr1 = line1[2:7].strip()
        catnr2 = line2[2:7].strip()
        if not catnr1 or catnr1 != catnr2:
            raise TleValidationError(f"Catalog number mismatch for {name!r}: {catnr1!r} vs {catnr2!r}.")

    return satellite_count


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as temp:
        temp.write(text)
        temp_path = Path(temp.name)
    os.replace(temp_path, path)


def load_previous_status() -> dict[str, Any]:
    status_path = DOCS_DIR / "status.json"
    if not status_path.exists():
        return {}
    try:
        with status_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def should_skip_fetch(previous_result: dict[str, Any] | None, output_path: Path) -> tuple[bool, str | None]:
    if not previous_result or not previous_result.get("ok") or not output_path.exists():
        return False, None

    updated_at = parse_utc_iso(previous_result.get("updatedUtc"))
    if updated_at is None:
        return False, None

    elapsed_seconds = (datetime.now(timezone.utc) - updated_at).total_seconds()
    if elapsed_seconds < MIN_SUCCESS_INTERVAL_SECONDS:
        remaining_seconds = int(MIN_SUCCESS_INTERVAL_SECONDS - elapsed_seconds)
        return True, f"Last successful update is still fresh; next upstream fetch allowed in {remaining_seconds} seconds."

    return False, None


def skipped_result(url: str, output_path: Path, previous_result: dict[str, Any], reason: str) -> dict[str, Any]:
    updated_at = parse_utc_iso(previous_result.get("updatedUtc"))
    next_allowed = None
    if updated_at is not None:
        next_allowed = datetime.fromtimestamp(
            updated_at.timestamp() + MIN_SUCCESS_INTERVAL_SECONDS,
            timezone.utc,
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    result: dict[str, Any] = {
        "ok": True,
        "skipped": True,
        "url": url,
        "path": output_path.relative_to(DOCS_DIR).as_posix(),
        "reason": reason,
        "keptPreviousFile": True,
        "updatedUtc": previous_result.get("updatedUtc"),
        "checkedUtc": utc_now_iso(),
    }
    if "satelliteCount" in previous_result:
        result["satelliteCount"] = previous_result["satelliteCount"]
    if next_allowed:
        result["nextAllowedFetchUtc"] = next_allowed
    return result


def update_one_tle(url: str, output_path: Path, previous_result: dict[str, Any] | None = None) -> dict[str, Any]:
    started = time.time()
    skip, skip_reason = should_skip_fetch(previous_result, output_path)
    if skip and skip_reason and previous_result:
        return skipped_result(url, output_path, previous_result, skip_reason)

    try:
        raw_text = fetch_text(url)
        normalized = normalize_tle_text(raw_text)
        satellite_count = validate_tle_text(normalized)
        previous_path = output_path.with_suffix(output_path.suffix + ".previous")
        if output_path.exists():
            shutil.copyfile(output_path, previous_path)
        atomic_write(output_path, normalized)
        return {
            "ok": True,
            "url": url,
            "path": output_path.relative_to(DOCS_DIR).as_posix(),
            "satelliteCount": satellite_count,
            "updatedUtc": utc_now_iso(),
            "elapsedSeconds": round(time.time() - started, 3),
        }
    except (TleFetchError, TimeoutError, TleValidationError, OSError) as exc:
        return {
            "ok": False,
            "url": url,
            "path": output_path.relative_to(DOCS_DIR).as_posix(),
            "error": str(exc),
            "keptPreviousFile": output_path.exists(),
            "checkedUtc": utc_now_iso(),
            "elapsedSeconds": round(time.time() - started, 3),
        }


def load_tracked_sats() -> list[dict[str, str]]:
    if not TRACKED_SATS_FILE.exists():
        return []
    with TRACKED_SATS_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, list):
        raise ValueError("tracked_sats.json must contain a JSON array.")

    satellites: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict) or "catnr" not in item:
            raise ValueError("Each tracked satellite must be an object with a catnr field.")
        satellites.append({
            "name": str(item.get("name", item["catnr"])),
            "catnr": str(item["catnr"]),
        })
    return satellites


def update_tracked_satellites(previous_status: dict[str, Any]) -> dict[str, Any]:
    satellites = load_tracked_sats()
    combined_parts: list[str] = []
    records: list[dict[str, Any]] = []
    previous_satellites = {
        str(item.get("catnr")): item
        for item in previous_status.get("tracked", {}).get("satellites", [])
        if isinstance(item, dict) and item.get("catnr") is not None
    }

    for sat in satellites:
        catnr = sat["catnr"]
        url = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={catnr}&FORMAT=TLE"
        output_path = CATALOG_DIR / f"{catnr}.tle"
        result = update_one_tle(url, output_path, previous_satellites.get(catnr))
        result["name"] = sat["name"]
        result["catnr"] = catnr
        records.append(result)

        if result["ok"] and output_path.exists():
            combined_parts.append(output_path.read_text(encoding="utf-8").strip())
        elif output_path.exists():
            combined_parts.append(output_path.read_text(encoding="utf-8").strip())

    if combined_parts:
        combined_text = "\n".join(part for part in combined_parts if part) + "\n"
        try:
            satellite_count = validate_tle_text(combined_text)
            atomic_write(DOCS_DIR / "formosat.tle", combined_text)
            combined_status = {
                "ok": True,
                "path": "formosat.tle",
                "satelliteCount": satellite_count,
                "updatedUtc": utc_now_iso(),
            }
        except TleValidationError as exc:
            combined_status = {
                "ok": False,
                "path": "formosat.tle",
                "error": str(exc),
                "keptPreviousFile": (DOCS_DIR / "formosat.tle").exists(),
                "checkedUtc": utc_now_iso(),
            }
    else:
        combined_status = {
            "ok": False,
            "path": "formosat.tle",
            "error": "No tracked satellites are configured.",
            "keptPreviousFile": (DOCS_DIR / "formosat.tle").exists(),
            "checkedUtc": utc_now_iso(),
        }

    return {
        "combined": combined_status,
        "satellites": records,
    }


def write_index(status: dict[str, Any]) -> None:
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SatelliteMap TLE Cache</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; line-height: 1.5; }}
    code {{ background: #f2f2f2; padding: 0.1rem 0.3rem; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>SatelliteMap TLE Cache</h1>
  <p>Last check: <code>{status["generatedUtc"]}</code></p>
  <ul>
    <li><a href="active.tle">active.tle</a></li>
    <li><a href="starlink.tle">starlink.tle</a></li>
    <li><a href="formosat.tle">formosat.tle</a></li>
    <li><a href="status.json">status.json</a></li>
  </ul>
</body>
</html>
"""
    atomic_write(DOCS_DIR / "index.html", html)


def main() -> int:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write(DOCS_DIR / ".nojekyll", "")
    previous_status = load_previous_status()

    status: dict[str, Any] = {
        "generatedUtc": utc_now_iso(),
        "source": "celestrak",
        "minimumSuccessIntervalSeconds": MIN_SUCCESS_INTERVAL_SECONDS,
        "groups": {},
        "tracked": {},
    }

    for group_name, url in GROUP_CATALOGS.items():
        previous_group = previous_status.get("groups", {}).get(group_name, {})
        status["groups"][group_name] = update_one_tle(url, DOCS_DIR / f"{group_name}.tle", previous_group)

    status["tracked"] = update_tracked_satellites(previous_status)
    atomic_write(DOCS_DIR / "status.json", json.dumps(status, indent=2, ensure_ascii=False) + "\n")
    write_index(status)

    any_group_ok = any(group["ok"] for group in status["groups"].values())
    any_tracked_ok = status["tracked"].get("combined", {}).get("ok", False)
    if any_group_ok or any_tracked_ok:
        return 0

    any_previous_file_kept = any(group.get("keptPreviousFile") for group in status["groups"].values())
    any_previous_file_kept = any_previous_file_kept or status["tracked"].get("combined", {}).get("keptPreviousFile", False)
    if any_previous_file_kept:
        print("No upstream TLE fetch succeeded. Existing public files were kept.", file=sys.stderr)
        return 0

    print("No TLE file is available and no upstream fetch succeeded.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
