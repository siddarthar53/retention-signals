"""Record (or --check) which UI source produced the committed frontend/dist build.

  python scripts/frontend_provenance.py          # after `npm run build`: write frontend/build-provenance.json
  python scripts/frontend_provenance.py --check  # fail if src/lockfile/config or dist changed since then
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

FRONT = Path(__file__).resolve().parents[1] / "frontend"
SOURCE = ["package.json", "package-lock.json", "index.html", "tsconfig.json", "vite.config.ts", "src"]
OUT = FRONT / "build-provenance.json"


def _files(root: Path, names) -> dict[str, str]:
    out = {}
    for n in names:
        p = root / n
        for f in sorted([p] if p.is_file() else p.rglob("*")):
            if f.is_file():
                out[f.relative_to(FRONT).as_posix()] = hashlib.sha256(f.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    return out


def current() -> dict:
    return {"source": _files(FRONT, SOURCE), "dist": _files(FRONT, ["dist"])}


def main() -> int:
    if "--check" in sys.argv:
        recorded = json.loads(OUT.read_text(encoding="utf-8"))
        if recorded != current():
            print("frontend/dist is out of date with its source: run `npm run build` and this script.")
            return 1
        print("frontend/dist matches the recorded source.")
        return 0
    OUT.write_text(json.dumps(current(), indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
