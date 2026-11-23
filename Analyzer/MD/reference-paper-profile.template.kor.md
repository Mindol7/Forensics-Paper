---
subject: "논문 제목"
identifier: "{{시스템 내 식별자}}"
classification:
  # 이 논문이 다루는 기술(Level 3). 해당되는 줄만 남기고 나머지는 삭제
  # PREP 준비
  - PREP-01-01  # 조사 계획 수립 / Investigation Planning
  - PREP-01-02  # 법적 권한 확보 / Securing Legal Authority
  # IDEC 식별
  - IDEC-01-01  # 현장 수색 및 디지털 기기 탐지 / On-scene Search and Digital Device Detection
  - IDEC-01-02  # 네트워크 자원 식별 / Network Resource Identification
  - IDEC-02-01  # 물리적 기기 우선순위 분류 / Physical Device Prioritization
  # PRCP 보전 (수집 전)
  - PRCP-01-01  # 현장 기기 접근 통제 / On-scene Device Access Control
  - PRCP-01-02  # 활성상태 시스템 격리 / Live System Isolation
  - PRCP-01-03  # 신호 차단 및 쓰기 방지 / Signal Isolation and Write Blocking
  - PRCP-01-04  # 보전 요청 / Preservation Request
  # COLL 수집
  - COLL-01-01  # 포렌식 데이터 우선순위 분류 / Forensic Data Triage
  - COLL-01-02  # 물리적 데이터 수집 / Physical Data Acquisition
  - COLL-01-03  # 논리적 데이터 수집 / Logical Data Acquisition
  - COLL-02-01  # 네트워크 패킷 및 로그 수집 / Network Packet and Log Collection
  - COLL-02-02  # 공개출처정보(OSINT) 수집 / Open Source Intelligence (OSINT) Collection
  - COLL-02-03  # 익명 네트워크 및 다크웹 데이터 수집 / Anonymous Network and Dark Web Data Collection
  - COLL-02-04  # 클라우드 데이터 수집 / Cloud Data Collection
  - COLL-03-01  # 포렌식 이미지 포맷 / Forensic Image Format
  # PSCP 보전 (수집 후)
  - PSCP-01-01  # 증거 무결성 검증 / Evidence Integrity Verification
  - PSCP-01-02  # 물리적 보관 환경 및 매체 관리 / Physical Storage Environment and Media Management
  - PSCP-01-03  # 보관 기간 및 법적 요건 관리 / Retention Period and Legal Requirement Management
  - PSCP-01-04  # 폐기 관리 / Disposal Management
  # EXAM 조사
  - EXAM-01-01  # 데이터 정규화 및 인덱싱 / Data Normalization and Indexing
  - EXAM-01-02  # 데이터 필터링 및 선별 / Data Filtering and Reduction
  - EXAM-02-01  # 가상화 시스템 조사 / Virtualization System Examination
  - EXAM-02-02  # 컨테이너 시스템 조사 / Container System Examination
  - EXAM-03-01  # 파티션 및 볼륨 조사 / Partition and Volume Examination
  - EXAM-03-02  # 파일시스템 조사 / File System Examination
  - EXAM-03-03  # 운영체제 아티팩트 조사 / Operating System Artifact Examination
  - EXAM-03-04  # 운영체제 레벨 메모리 조사 / OS-level Memory Examination
  - EXAM-04-01  # 애플리케이션 아티팩트 조사 / Application Artifact Examination
  - EXAM-04-02  # 애플리케이션 레벨 메모리 조사 / Application-level Memory Examination
  - EXAM-04-03  # 파일 저장 포맷 조사 / File Storage Format Examination
  - EXAM-05-01  # 네트워크 패킷 및 로그 조사 / Network Packet and Log Examination
  - EXAM-05-02  # 공개출처정보(OSINT) 조사 / OSINT Examination
  - EXAM-05-03  # 익명 네트워크 및 다크웹 데이터 조사 / Anonymous Network and Dark Web Data Examination
  - EXAM-05-04  # 클라우드 데이터 조사 / Cloud Data Examination
  # ANYS 분석
  - ANYS-01-01  # 연관 콘텐츠 분석 / Relevant Content Analysis
  - ANYS-01-02  # 통합 타임라인 분석 / Integrated Timeline Analysis
  - ANYS-01-03  # 다차원 관계 분석 / Multi-dimensional Relationship Analysis
  - ANYS-01-04  # 이벤트 재구성 / Event Reconstruction
  # REPT 보고
  - REPT-01-01  # 포렌식 정보 시각화 / Forensic Information Visualization
  - REPT-01-02  # 포렌식 보고서 작성 / Forensic Report Writing
  - REPT-01-03  # 증거 허용성 검토 / Evidence Admissibility Review
  # CMNF 공통 기반 기술
  - CMNF-01-01  # 기기 손상 대응 / Damaged Device Handling
  - CMNF-01-02  # 암호 해독 / Cryptographic Decryption
  - CMNF-01-03  # 보호 메커니즘 대응 / Protection Mechanism Bypass
  - CMNF-01-04  # 안티-포렌식 행위 지표 식별 / Anti-forensic Indicator Identification
  - CMNF-01-05  # 삭제/손상 데이터 복구 / Deleted/Damaged Data Recovery
  - CMNF-01-06  # 위변조 탐지 / Forgery and Tampering Detection
  - CMNF-01-07  # 은닉 탐지 / Concealment Detection
  - CMNF-01-08  # 비익명화 / De-anonymization
  - CMNF-01-09  # 신뢰실행환경 활용 및 대응 / Trusted Execution Environment (TEE) Handling
  - CMNF-02-01  # 참조 데이터 세트 / Reference Data Sets
  - CMNF-02-02  # 디지털 증거 공통 표현 / Common Representation of Digital Evidence
  - CMNF-02-03  # 포렌식 도구 검증 / Forensic Tool Validation
  - CMNF-02-04  # 포렌식 준비도 / Forensic Readiness
  - CMNF-02-05  # 포렌식 교육 및 역량 체계 / Forensic Education and Competency Framework
  - CMNF-02-06  # 실험실 구축 / Laboratory Establishment
  # CVGF 융합 포렌식
  - CVGF-01-01  # 이벤트 모니터링, 기록 및 분석 / Event Monitoring, Logging and Analysis
  - CVGF-01-02  # 사이버 위협 인텔리전스(CTI) 활용 / Cyber Threat Intelligence (CTI) Utilization
  - CVGF-01-03  # 악성코드 대응 / Malware Response
  - CVGF-01-04  # 랜섬웨어 대응 / Ransomware Response
  - CVGF-01-05  # 기술 유출 대응 / Technology Leakage Response
  - CVGF-01-06  # 개인정보 침해 대응 / Personal Data Breach Response
  - CVGF-01-07  # 저작권 침해 대응 / Copyright Infringement Response
  - CVGF-02-01  # 미디어 출처 분석 / Media Source Analysis
  - CVGF-02-02  # 미디어 위변조 탐지 및 무결성 검증 / Media Tampering Detection and Integrity Verification
  - CVGF-02-03  # 멀티모달 콘텐츠 분석 / Multimodal Content Analysis
  - CVGF-03-01  # 온체인 거래 추적 / On-chain Transaction Tracing
  - CVGF-03-02  # 온체인 난독화 대응 / On-chain Obfuscation Countermeasures
  - CVGF-03-03  # 오프체인 거래 추적 / Off-chain Transaction Tracing
  - CVGF-04-01  # 모빌리티(드론, 자동차, 선박) 포렌식 / Mobility (Drone, Vehicle, Vessel) Forensics
  - CVGF-04-02  # 로봇 포렌식 / Robot Forensics
  - CVGF-04-03  # 헬스케어 포렌식 / Healthcare Forensics
  - CVGF-04-04  # 제어시스템 포렌식 / Industrial Control System (ICS/SCADA) Forensics
  - CVGF-04-05  # 항공·우주 포렌식 / Aerospace Forensics
  - CVGF-05-01  # 지능형 포렌식 분석 자동화 / Intelligent Forensic Analysis Automation
  - CVGF-05-02  # 인공지능 시스템 대상 포렌식 / Forensics of AI Systems
  - CVGF-05-03  # 인공지능 생성물 식별 및 악용 범죄 대응 / AI-generated Content Identification and Abuse Response
tags:
  # 해당되는 줄만 남기고 나머지는 삭제
  # DEV (기기)
  - DEV-PC            # PC
  - DEV-MOB           # Mobile
  # FS (파일 시스템)
  - FS-BLK            # 블록 장치 기반 파일시스템
  - FS-FLH            # 플래시 메모리 파일시스템
  - FS-NET            # 네트워크 파일시스템
  - FS-DST            # 분산 파일시스템
  - FS-OPT            # 광학매체 파일시스템
  - FS-ETC            # 특수 목적 및 기타
  # OS (운영체제)
  - OS-WIN            # Windows
  - OS-LNX            # Linux
  - OS-MAC            # macOS
  - OS-AND            # Android
  - OS-IOS            # iOS
  - OS-ETC            # 특수 목적 및 기타
  # APP (애플리케이션)
  - APP-WEB           # 웹 브라우저
  - APP-MES           # 메신저
  - APP-COL           # 협업
  - APP-SNS           # SNS
  - APP-EML           # 이메일
  - APP-GRW           # 그룹웨어
  - APP-ERP           # ERP
  - APP-CNT           # 전자계약 및 서명
  - APP-ACC           # 회계
  - APP-CST           # 클라우드 스토리지
  - APP-SHA           # 파일 전송 및 공유
  - APP-ARC           # 파일 압축 및 아카이빙
  - APP-REC           # 파일 백업 및 복구
  - APP-DOC           # 문서 보기 및 편집
  - APP-CCP           # 일정, 연락처 및 개인정보 관리
  - APP-PRJ           # 프로젝트 및 업무 관리
  - APP-MUL           # 멀티미디어 재생 및 편집
  - APP-GAM           # 게임
  - APP-CWA           # 가상자산 지갑
  - APP-PWD           # 비밀번호 관리 및 사용자 인증
  - APP-RMT           # 원격 접속 및 원격 제어
  - APP-VPN           # VPN, 프록시 및 익명화
  - APP-SEC           # 보안 및 엔드포인트 관리
  - APP-DEV           # 소프트웨어 개발
  - APP-GAI           # 생성형 AI 및 AI 에이전트
  - APP-WSV           # 웹 서버
  - APP-DSV           # 데이터베이스 서버
  - APP-ETC           # 특수 목적 및 기타
  # CLOUD (클라우드)
  - CLOUD-SaaS:MES    # 메신저
  - CLOUD-SaaS:COL    # 협업
  - CLOUD-SaaS:SNS    # SNS
  - CLOUD-SaaS:EML    # 이메일
  - CLOUD-SaaS:GRW    # 그룹웨어
  - CLOUD-SaaS:CST    # 스토리지
  - CLOUD-SaaS:ERP    # ERP
  - CLOUD-SaaS:CNT    # 전자계약 및 서명
  - CLOUD-SaaS:ACC    # 회계
  - CLOUD-SaaS:PRJ    # 프로젝트 및 업무 관리
  - CLOUD-SaaS:GAI    # 생성형 AI
  - CLOUD-DaaS
  - CLOUD-IaaS
last_updated: "YYYY-MM-DDTHH:MM:SSZ"
author: "작성자"
---

# 논문 제목

## 1. 기본 정보
| 항목 | 내용 |
|---|---|
| DOI | |
| URL | |
| 유형 | Article / Conference Paper / Review / Thesis / Book Chapter |
| 논문지 | |
| 권호 | |
| 출판 기관 | |
| 출판 날짜 | YYYY. MM. |
| OA 여부 | O / X |
| 저자 | 쉼표로 구분 |
| 소속 기관 | 저자별로 <br> 로 줄바꿈 |

## 2. 초록
<div style="border:1px solid #ccc; padding:12px; border-radius:6px;">
원문 초록
</div>

## 3. 키워드
키워드1, 키워드2, 키워드3

## 4. 대상 애플리케이션
<!-- 등록된 대상이면 ID 기재, 미등록이면 공란 -->
| ID | 이름 |
|---|---|
| <!-- 시스템 내 level5 식별자 --> | <!-- 대상 애플리케이션 명 --> |

## 5. 참고문헌
<!-- 등록된 문헌이면 ID 기재, 미등록이면 공란. 웹 자료는 [제목](URL) -->
| ID | 제목 |
|---|---|
| <!-- 시스템 내 참고문헌 식별자 --> | <!-- 참고문헌 제목 --> |
