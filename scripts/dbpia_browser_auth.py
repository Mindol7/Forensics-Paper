#!/usr/bin/env python3
"""Create/refresh a dedicated DBpia browser profile by manual login only."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT))

from PaperPipeline.dbpia_browser import (  # noqa: E402
    DbpiaBrowserOperationError,
    default_dbpia_profile_dir,
    run_interactive_dbpia_login,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "DBpia 전용 Playwright 프로필을 열어 사용자가 직접 로그인/기관 "
            "인증하도록 합니다. ID와 비밀번호는 프로그램이 받지 않습니다."
        )
    )
    parser.add_argument(
        "--profile-dir",
        default=(
            os.getenv("DBPIA_BROWSER_PROFILE_DIR")
            or str(default_dbpia_profile_dir())
        ),
        help="저장소 밖의 DBpia 전용 프로필 절대경로",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    profile_dir = Path(str(args.profile_dir)).expanduser()
    try:
        run_interactive_dbpia_login(
            profile_dir,
            repository_root=REPOSITORY_ROOT,
        )
    except (KeyboardInterrupt, EOFError):
        print("DBpia 수동 인증을 취소했습니다.", file=sys.stderr)
        return 130
    except (ValueError, OSError, DbpiaBrowserOperationError) as exc:
        parser.error(str(exc))
    print(
        "DBpia 전용 프로필 인증 창을 종료했습니다. 실제 로그인 상태는 "
        "단일 논문 smoke test로 확인하세요."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
