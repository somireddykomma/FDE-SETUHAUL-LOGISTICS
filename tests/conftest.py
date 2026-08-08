import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE_DB = ROOT / "data" / "setuhaul_freight_operations.db"


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """A private copy of the seeded DB so tests never mutate shared dev data
    and can run concurrently without stepping on each other."""
    test_db = tmp_path / "test.db"
    shutil.copyfile(BASE_DB, test_db)
    monkeypatch.setenv("SETUHAUL_DB_PATH", str(test_db))
    return test_db
