"""The committed UI build matches its source and carries no data or secrets."""
import re
from pathlib import Path

import frontend_provenance as fp

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def test_committed_build_matches_recorded_source():
    import json
    assert json.loads(fp.OUT.read_text(encoding="utf-8")) == fp.current()


def test_bundle_contains_no_data_or_secrets():
    text = "".join(f.read_text(encoding="utf-8", errors="ignore") for f in DIST.rglob("*") if f.is_file())
    assert not re.search(r"EMP-\d{6}", text), "employee IDs in bundle"
    for bad in ("postgresql://", "RS_APP_PASSWORD", "DATABASE_ADMIN_URL", "argon2$", "demo_credentials.txt\t"):
        assert bad not in text, bad
