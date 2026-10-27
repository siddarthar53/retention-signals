"""Generate the synthetic dataset.

    python -m generator.run --scale 500 [--out data/out] [--load]

--load DELETES and recreates the 'retention' database (scripts/db_reset.py rules) and loads the
canonical and eval tables in one transaction, then runs the validator and writes the G2 report.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .core import ROOT, Streams, load_config
from .derived import build_labels, build_outcomes, build_potential_outcomes, build_snapshots
from .export import seed_sql, write_dataset
from .history import Simulator
from .population import build_initial_population, build_master
from .registry import Registry


@dataclass
class Dataset:
    reg: Registry
    cfg: dict
    gcfg: dict
    stats: dict
    snapshots: list = field(default_factory=list)
    outcomes: dict = field(default_factory=dict)
    labels: list = field(default_factory=list)
    potential_outcomes: list = field(default_factory=list)
    ground_truth: list = field(default_factory=list)
    fixtures: dict = field(default_factory=dict)


def load_generator_config(path: Path | None = None) -> dict:
    path = path or ROOT / "config" / "generator.yaml"
    g = yaml.safe_load(path.read_text(encoding="utf-8"))
    import hashlib
    g["_hash"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return g


def build(scale: int, seed: int | None = None, b0: float | None = None, with_injection: bool = True) -> Dataset:
    cfg = load_config()
    gcfg = load_generator_config()
    cfg["scale"]["employees"] = scale
    seed = int(cfg["seed"] if seed is None else seed)
    b0 = gcfg["hazard"]["voluntary"]["b0"] if b0 is None else b0
    if b0 is None:
        raise SystemExit("hazard.voluntary.b0 is not calibrated. Run: python -m generator.calibrate")
    streams = Streams(seed)
    reg = Registry()
    build_master(reg, cfg)
    stats = build_initial_population(reg, cfg, streams)
    stats.update(Simulator(reg, cfg, gcfg, streams, float(b0)).run())
    reg.assign_ids()
    ds = Dataset(reg, cfg, gcfg, stats)
    ds.snapshots = build_snapshots(reg)
    ds.outcomes = build_outcomes(reg)
    ds.labels = build_labels(reg, ds.outcomes)
    ds.potential_outcomes = build_potential_outcomes(ds.labels, streams["hazard"],
                                                     float(gcfg["treatment"]["prevention_prob"]))
    if with_injection:
        from .injection import inject
        inject(ds, streams["injection"])
    ds.stats["seed"] = seed
    return ds


def generate(scale: int, out_root: Path) -> tuple[Dataset, dict]:
    ds = build(scale)
    out_dir = out_root / str(scale)
    meta = {"seed": ds.stats["seed"], "scale": scale, "config_sha256": ds.cfg["_hash"],
            "generator_config_sha256": ds.gcfg["_hash"], "b0": ds.gcfg["hazard"]["voluntary"]["b0"]}
    manifest = write_dataset(ds, out_dir, meta, extra_files=ds.fixtures)
    (out_dir / "seed.sql").write_bytes(seed_sql(out_dir))
    return ds, manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=500)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "out")
    ap.add_argument("--load", action="store_true")
    a = ap.parse_args(argv)
    ds, manifest = generate(a.scale, a.out)
    out_dir = a.out / str(a.scale)
    for f, info in manifest["files"].items():
        if f.endswith(".csv") and "rows" in info:
            print(f"{info['rows']:>9}  {f}")
    if a.load:
        sys.path.insert(0, str(ROOT / "scripts"))
        import os
        import psycopg
        from dbutil import ensure_app_login, load_env, load_schema, recreate_database
        from .export import load
        from .report import write_report
        load_env()
        url = recreate_database("retention")
        load_schema(url)
        ensure_app_login(url, os.environ.get("RS_APP_PASSWORD"))
        with psycopg.connect(url) as conn:
            counts = load(conn, out_dir)
            conn.commit()
            path = write_report(conn, ds, manifest, out_dir)
        print(f"Loaded {sum(counts.values())} rows into 'retention' in one transaction. Report: {path}")
    print(f"Manifest: {out_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
