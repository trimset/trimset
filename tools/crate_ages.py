"""Every crate in proxy/Cargo.lock, with its publish date: the 7-day supply-chain hold for
Rust (docs/05-testing.md §7). Exits 1 when one is newer than the cutoff.

    python3 tools/crate_ages.py 2026-11-04

Pin an offender back with: cargo update -p <name> --precise <older version>
"""

import json
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

LOCK = Path(__file__).resolve().parents[1] / "proxy" / "Cargo.lock"
HEADERS = {"User-Agent": "trimset-build (github.com/trimset/trimset)"}


def published(name: str, version: str) -> str:
    url = f"https://crates.io/api/v1/crates/{name}/{version}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS)) as r:
        return str(json.load(r)["version"]["created_at"])[:10]


def main(cutoff: str) -> int:
    packages = tomllib.loads(LOCK.read_text())["package"]
    too_new = []
    for p in packages:
        if p.get("source", "").startswith("registry+"):
            day = published(p["name"], p["version"])
            if day >= cutoff:
                too_new.append(f"{p['name']} {p['version']} ({day})")
            time.sleep(1)  # crates.io asks crawlers for one request a second
    print("\n".join(too_new) or f"all {len(packages) - 1} crates published before {cutoff}")
    return 1 if too_new else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
