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

GROUP_CATALOGS = {
    "active": "https://celestrak.org/NORAD/elements/gp.php?GROUP=ACTIVE&FORMAT=TLE",
    # STARLINK is optional for the first emergency rollout. Leave it enabled if
    # your client actually needs it; otherwise remove it from this dictionary.
    "starlink": "https://celestrak.org/NORAD/elements/gp.php?GROUP=STARLINK&FORMAT=TLE",
}


class TleValidationError(ValueError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


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


def update_one_tle(url: str, output_path: Path) -> dict[str, Any]:
    started = time.time()
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
    except (urllib.error.URLError, TimeoutError, TleValidationError, OSError) as exc:
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


def update_tracked_satellites() -> dict[str, Any]:
    satellites = load_tracked_sats()
    combined_parts: list[str] = []
    records: list[dict[str, Any]] = []

    for sat in satellites:
        catnr = sat["catnr"]
        url = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={catnr}&FORMAT=TLE"
        output_path = CATALOG_DIR / f"{catnr}.tle"
        result = update_one_tle(url, output_path)
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

    status: dict[str, Any] = {
        "generatedUtc": utc_now_iso(),
        "source": "celestrak",
        "groups": {},
        "tracked": {},
    }

    for group_name, url in GROUP_CATALOGS.items():
        status["groups"][group_name] = update_one_tle(url, DOCS_DIR / f"{group_name}.tle")

    status["tracked"] = update_tracked_satellites()
    atomic_write(DOCS_DIR / "status.json", json.dumps(status, indent=2, ensure_ascii=False) + "\n")
    write_index(status)

    any_group_ok = any(group["ok"] for group in status["groups"].values())
    any_tracked_ok = status["tracked"].get("combined", {}).get("ok", False)
    if any_group_ok or any_tracked_ok:
        return 0

    print("No TLE file was updated successfully. Existing files, if any, were kept.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
