#!/usr/bin/env python3
"""Create or refresh a dedicated KDFS JAMS profile by manual login."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT))

from PaperPipeline.jams_browser import (  # noqa: E402
    JamsBrowserOperationError,
    default_jams_profile_dir,
    run_interactive_jams_login,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "한국디지털포렌식학회 JAMS 전용 Playwright 프로필을 열어 "
            "사용자가 직접 로그인/인증하도록 합니다. 인증정보는 "
            "프로그램이 받지 않습니다."
        )
    )
    parser.add_argument(
        "--profile-dir",
        default=(
            os.getenv("JAMS_BROWSER_PROFILE_DIR")
            or str(default_jams_profile_dir())
        ),
        help="저장소 밖의 KDFS JAMS 전용 프로필 절대경로",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    profile_dir = Path(str(args.profile_dir)).expanduser()
    try:
        run_interactive_jams_login(
            profile_dir,
            repository_root=REPOSITORY_ROOT,
        )
    except (KeyboardInterrupt, EOFError):
        print("KDFS JAMS 수동 인증을 취소했습니다.", file=sys.stderr)
        return 130
    except (ValueError, OSError, JamsBrowserOperationError) as exc:
        parser.error(str(exc))
    print(
        "KDFS JAMS 전용 프로필 인증 창을 종료했습니다. 실제 로그인 "
        "상태는 단일 논문 smoke test로 확인하세요."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
