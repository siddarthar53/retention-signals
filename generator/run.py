"""Generate the synthetic dataset.

    python -m generator.run --scale 500 [--out data/out] [--load]

--load DELETES and recreates the 'retention' database (see scripts/db_reset.py) and loads the
canonical tables in one transaction.
Current stage (G2 scaffolding): master data and the population employed on 2025-01-01 with
pre-window careers. The monthly history follows once generator design v0.2 is agreed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import ROOT, Streams, load_config
from .export import load, write_canonical
from .population import build_initial_population, build_master
from .registry import Registry


def generate(scale: int, out_root: Path, cfg_path: Path | None = None) -> tuple[Registry, dict]:
    cfg = load_config(cfg_path)
    cfg["scale"]["employees"] = scale
    streams = Streams(int(cfg["seed"]))
    reg = Registry()
    build_master(reg, cfg)
    stats = build_initial_population(reg, cfg, streams)
    reg.assign_ids()
    meta = {"seed": cfg["seed"], "scale": scale, "config_sha256": cfg["_hash"],
            "stage": "g2-scaffold: master + initial population", "sizes": stats}
    out_dir = out_root / str(scale)
    manifest = write_canonical(reg, out_dir, meta)
    return reg, manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=500)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "out")
    ap.add_argument("--load", action="store_true")
    a = ap.parse_args(argv)
    _, manifest = generate(a.scale, a.out)
    out_dir = a.out / str(a.scale)
    for f, info in manifest["files"].items():
        print(f"{info['rows']:>8}  {f}")
    if a.load:
        sys.path.insert(0, str(ROOT / "scripts"))
        import os
        import psycopg
        from dbutil import ensure_app_login, load_env, load_schema, recreate_database
        load_env()
        url = recreate_database("retention")
        load_schema(url)
        ensure_app_login(url, os.environ.get("RS_APP_PASSWORD"))
        with psycopg.connect(url) as conn:
            counts = load(conn, out_dir)
        print("Loaded into 'retention' in one transaction:", sum(counts.values()), "rows")
    print(f"Manifest: {out_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
