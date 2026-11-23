from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "process_papers.py"


def test_blank_source_id_cannot_bypass_corpus_guard() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "download",
            "--dry-run",
            "--source-table",
            "kci_corpus",
            "--allow-corpus",
            "--source-id",
            "   ",
        ],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 2
    assert "반드시 --limit 또는 --source-id" in completed.stderr


def test_missing_sqlite_path_is_rejected_without_creating_file(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.sqlite"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "download",
            "--dry-run",
            "--database-url",
            f"sqlite:///{missing}",
            "--limit",
            "1",
        ],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 2
    assert "존재하지 않습니다" in completed.stderr
    assert not missing.exists()


def test_status_missing_directory_returns_nonzero(tmp_path: Path) -> None:
    missing = tmp_path / "missing-papers"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "status",
            "--papers-dir",
            str(missing),
        ],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 1
    assert "존재하지 않습니다" in completed.stderr


def test_corpus_batch_limit_is_bounded() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "download",
            "--dry-run",
            "--source-table",
            "kci_corpus",
            "--allow-corpus",
            "--limit",
            "10001",
        ],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 2
    assert "10000 이하여야" in completed.stderr
