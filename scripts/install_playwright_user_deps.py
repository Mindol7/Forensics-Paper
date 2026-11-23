#!/usr/bin/env python3
"""Install the minimal Chromium shared libraries without sudo on Debian/Ubuntu."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path


PACKAGES = ("libnspr4", "libnss3", "libasound2t64")


def user_library_root() -> Path:
    configured = os.getenv("XDG_DATA_HOME")
    base = (
        Path(configured).expanduser()
        if configured and configured.strip()
        else Path.home() / ".local" / "share"
    )
    return (base / "forensics-paper" / "playwright-libs").resolve()


def main() -> int:
    if os.name != "posix" or platform.machine() not in {"x86_64", "amd64"}:
        raise SystemExit("이 무관리자 설치기는 Debian/Ubuntu x86_64 전용입니다")
    for executable in ("apt-get", "dpkg-deb"):
        if shutil.which(executable) is None:
            raise SystemExit(f"필요한 명령을 찾을 수 없습니다: {executable}")

    target = user_library_root()
    if target.is_symlink():
        raise SystemExit("사용자 Playwright library 경로가 symlink입니다")
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="forensics-paper-playwright-") as tmp:
        temporary = Path(tmp)
        subprocess.run(
            ["apt-get", "download", *PACKAGES],
            cwd=temporary,
            check=True,
        )
        packages = sorted(temporary.glob("*.deb"))
        if len(packages) != len(PACKAGES):
            raise SystemExit("Playwright dependency package 수가 예상과 다릅니다")
        for package in packages:
            subprocess.run(
                ["dpkg-deb", "-x", str(package), str(target)],
                check=True,
            )

    library_dir = target / "usr" / "lib" / "x86_64-linux-gnu"
    required = ("libnspr4.so", "libnss3.so", "libnssutil3.so", "libasound.so.2")
    missing = [name for name in required if not (library_dir / name).exists()]
    if missing:
        raise SystemExit("설치 후에도 library가 없습니다: " + ", ".join(missing))
    print(f"Playwright 사용자 library 설치 완료: {library_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
