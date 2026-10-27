"""Calibrate the voluntary-hazard intercept b0 once, on development seeds, and freeze it in config.

    python -m generator.calibrate

Target: voluntary terminations in 2025-2026 / N = calibration.target_voluntary_termination_rate,
averaged over calibration.dev_seeds at calibration.dev_scale. The main seed and any test-period
outcome are never used. Writes b0 and its provenance into config/generator.yaml.
"""
from __future__ import annotations

import datetime as dt
import re
import sys

from .core import ROOT, Streams, load_config
from .history import Simulator
from .population import build_initial_population, build_master
from .registry import Registry
from .run import load_generator_config


def rate(b0: float, seeds, scale, cfg, gcfg) -> float:
    vals = []
    for seed in seeds:
        c = dict(cfg); c["scale"] = dict(cfg["scale"]); c["scale"]["employees"] = scale
        streams = Streams(seed)
        reg = Registry()
        build_master(reg, c)
        build_initial_population(reg, c, streams)
        st = Simulator(reg, c, gcfg, streams, b0).run()
        vals.append(st["voluntary_terminations"] / scale)
    return sum(vals) / len(vals)


def main() -> int:
    cfg, gcfg = load_config(), load_generator_config()
    cal = gcfg["calibration"]
    if int(cfg["seed"]) in cal["dev_seeds"]:
        raise SystemExit("dev seeds must differ from the main seed")
    target, seeds, scale = cal["target_voluntary_termination_rate"], cal["dev_seeds"], cal["dev_scale"]
    lo, hi = -9.0, -4.0
    r_lo, r_hi = rate(lo, seeds, scale, cfg, gcfg), rate(hi, seeds, scale, cfg, gcfg)
    for _ in range(12):
        mid = (lo + hi) / 2
        r = rate(mid, seeds, scale, cfg, gcfg)
        print(f"b0={mid:+.4f}  rate={r:.4f}")
        if r < target:
            lo, r_lo = mid, r
        else:
            hi, r_hi = mid, r
    b0 = round((lo + hi) / 2, 4)
    achieved = rate(b0, seeds, scale, cfg, gcfg)
    path = ROOT / "config" / "generator.yaml"
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(\n    b0: )[^\n#]*(\s*#[^\n]*)?", rf"\g<1>{b0}                 # calibrated, see provenance", text, count=1)
    prov = (f"\"b0={b0} gives {achieved:.4f} on dev seeds {seeds} at scale {scale} "
            f"(target {target}); bisection on [-9, -4], 12 steps; {dt.date.today().isoformat()}\"")
    text = re.sub(r"(\n  provenance: )[^\n]*", rf"\g<1>{prov}", text, count=1)
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"Frozen b0 = {b0} (dev-seed rate {achieved:.4f}) in {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
