from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from dmr_mapper.config import load_config
from dmr_mapper.db import Store

REPO = Path(__file__).resolve().parent.parent
SAMPLES = REPO / "samples"
CAPPLUS_LOG = SAMPLES / "capplus" / "dsd_fme_168863_20260509_163344.log"
SURVEYOR_ROOT = SAMPLES / "surveyor" / "decodes_20260713"
SYNTHETIC = REPO / "tests" / "fixtures" / "synthetic"
TZ = ZoneInfo("Asia/Jerusalem")


@pytest.fixture(scope="session")
def cfg() -> dict:
    c = load_config(REPO / "config.example.yaml")
    c["timezone"] = "Asia/Jerusalem"
    return c


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture(scope="session")
def capplus_store(tmp_path_factory: pytest.TempPathFactory, cfg: dict) -> Store:
    """The Cap+ sample ingested once per session (it takes ~0.5 s)."""
    from dmr_mapper.ingest import ingest_log_file

    s = Store(tmp_path_factory.mktemp("capplus") / "capplus.db")
    stats = ingest_log_file(s, CAPPLUS_LOG, cfg)
    s.stats = stats  # type: ignore[attr-defined]
    yield s
    s.close()


@pytest.fixture(scope="session")
def surveyor_store(tmp_path_factory: pytest.TempPathFactory, cfg: dict) -> Store:
    from dmr_mapper.ingest import ingest_surveyor

    s = Store(tmp_path_factory.mktemp("surveyor") / "surveyor.db")
    s.results = ingest_surveyor(s, SURVEYOR_ROOT, cfg)  # type: ignore[attr-defined]
    yield s
    s.close()
