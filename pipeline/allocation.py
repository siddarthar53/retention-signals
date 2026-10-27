"""The single, versioned rule that places an approved case in the conversation or comparison group.

Used by the simulated reviews (pipeline/run.py) and by the live API (backend/app/gate.py), so every
approved case in a run is allocated the same way, whichever route created it. The arm depends only on the
rule version, the run and the employee: it cannot be re-drawn by retrying a request."""
from __future__ import annotations

import hashlib

ALLOCATION_VERSION = "arm-v1"
CONTROL_SHARE = 0.20                     # agreed 80/20 treatment/control split


def assign_arm(employee_id: str, score_run_id: str) -> str:
    h = hashlib.sha256(f"retention-signals:{ALLOCATION_VERSION}:{score_run_id}:{employee_id}".encode()).digest()
    return "control" if int.from_bytes(h[:8], "big") / 2 ** 64 < CONTROL_SHARE else "treatment"
