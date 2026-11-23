# Forensics-Paper

KCI(한국학술지인용색인)에 등록된 국내 디지털포렌식 논문을 자동으로 수집·선별하고, 원문 PDF와 부가 정보를 모아
논문별 프로필(MD)과 웹(DF-Insight)용 데이터를 만드는 파이프라인입니다.
매달 실행하면 지난 실행 이후에 나온 논문만 이어서 처리합니다(증분 수집).

```
논문 정보 수집(KCI Open API) → 필터링(블랙리스트·키워드) → 원문 PDF 수집 → 논문 분석(Claude)
→ OA·특집호·참고문헌 수집 → 유형·분류 도출(Jev AI 결과 반영) → MD 프로필 생성 → 지식 그래프·웹 동기화
```

## 디렉터리 구조

```
Forensics-Paper/
├── main.py            # 실행 진입점 (전체 또는 단계별 실행)
├── config.py          # .env 설정 읽기
├── run_monthly.sh     # 월간 자동 실행 스크립트 (cron)
├── Collector/         # KCI 논문 정보·원문 수집, 브라우저 로그인
├── Processor/         # 블랙리스트·키워드 필터링 (규칙 파일: Processor/refs/)
├── PaperPipeline/     # 원문 PDF 탐색·다운로드(공개 저장소, DBpia, 교보 스콜라, JAMS 등), 본문 추출
├── Analyzer/          # 분석·도출: 분류(LV, JevAI), MD 프로필(MD), OA·특집호·참고문헌, 그래프, 웹 동기화
├── Scheduler/         # 단계 순서와 체크포인트(증분 수집) 관리
├── Core/ Storage/ Exporter/ Util/   # 공통 로직, DB, Excel 보고서, 로그·실행 잠금
├── scripts/           # 보조 CLI (사이트 로그인 프로필 생성, 통계 등)
└── tests/             # pytest 테스트
```

## 설치

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium     # 원문 PDF 수집용 브라우저
cp .env.example .env                      # KCI_OPEN_API_KEY 등 입력

# (선택) 웹 데이터 빌드용 저장소. 없으면 실행할 때 --skip-web-build
git clone https://github.com/dfrc-korea/DF-Insight.git web/DF-Insight
```

## 사용법

```bash
python3 main.py              # 전체 파이프라인 (처음이면 전수 수집, 이후에는 증분 수집)
python3 main.py --dry-run    # 수집·다운로드 없이 실행 계획만 확인
python3 main.py -h           # 전체 옵션 보기
```

옵션을 붙이면 해당 단계만 실행합니다. 여러 개를 주면 파이프라인 순서대로 실행됩니다.

| 옵션 | 실행 단계 |
| --- | --- |
| `-all` | KCI 논문 정보 수집(전수/증분) + 새 논문 필터링 |
| `-filter` | 바뀐 필터 규칙을 전체 논문에 다시 적용 |
| `-pdf` | 원문 PDF 수집 |
| `-analyze` | 새 PDF 분석(Claude) |
| `-oa` / `-special` / `-ref` | Open Access / 특집호 / 참고문헌 수집 |
| `-derive` | 논문 유형·키워드 정리, Jev AI 분류 반영, 결과 워크북 저장 |
| `-jev` | Jev AI 결과를 MD 입력으로 정리 |
| `-md` | 논문별 MD 프로필 생성 |
| `-renumber` | 논문 번호를 다시 정렬하고 결과물 재생성 |
| `-graph` | 지식 그래프 생성 + DF-Insight 동기화·웹 빌드 |

```bash
python3 main.py -all                     # 논문 정보만 수집
python3 main.py -pdf --browser-providers dbpia,kyobo,jams --browser-login always
                                         # 처음 한 번: 로그인 사이트의 브라우저 프로필을 만든 뒤 원문 수집
python3 main.py -jev -md -graph          # 새 Jev AI 결과를 MD·웹에 반영
./run_monthly.sh --non-interactive       # 월간 자동 실행 (cron 등록용)
```

## 참고

- **로그인 사이트(DBpia·교보 스콜라·JAMS)**: 브라우저 창에서 직접 로그인합니다. 비밀번호는 프로그램에 저장하지 않습니다.
- **논문 분석**: `.env`에 `ANTHROPIC_API_KEY`가 있으면 API를, 없으면 로그인된 Claude Code를 사용합니다
  (`--analysis-backend`로 지정 가능).
- **결과물**: DB, PDF, 워크북, MD 등 실행 결과는 저장소에 포함하지 않습니다(`.gitignore`).
- **테스트**: `pytest`
