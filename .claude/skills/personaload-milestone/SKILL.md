---
name: personaload-milestone
description: "personaload(SPEC_B.md) 마일스톤을 구현→병렬 리뷰(SPEC 준수 + 실제 AIPerf 실측)→반박 검증→수정→완료 기준 검증→커밋까지 진행하는 오케스트레이터. 'B3 진행', '다음 마일스톤', 'SPEC_B 구현', 'personaload 기능 추가/수정/보완', '리뷰 다시', '리뷰 결과 반영', '완료 기준 재검증', 'mock/실측 다시 돌려' 같은 요청에는 반드시 이 스킬을 쓴다. personaload 코드에 대한 단순 질문이나 한 줄 수정은 직접 처리한다."
---

# personaload-milestone — 마일스톤 오케스트레이터

personaload는 AIPerf 0.12.0 위에 올린 캐릭터챗 워크로드 벤치마크다. 사양은 `SPEC_B.md`이고, AIPerf가 조용히 다르게 동작하는 규칙들의 근거는 `docs/aiperf-v0.12.0-facts.json`에 있다. 이 스킬은 마일스톤 하나를 "완료 기준을 직접 실행해 확인한 상태"까지 끌고 간다.

**실행 모드:** 서브 에이전트 병렬. 리뷰어끼리 대화할 필요 없이 결과만 받으면 되고, 이 환경에는 에이전트 팀 도구가 없다.

## Phase 0: 컨텍스트 확인
1. `git log --oneline`, `git status`로 어디까지 커밋됐는지 확인한다. SPEC_B §14에서 대상 마일스톤을 정한다.
2. `_workspace/`에 해당 마일스톤 산출물이 있는지 본다.
   - 없음 → **초기 실행**: Phase 1부터.
   - 있음 + 사용자가 "리뷰 다시/반영" 요청 → **부분 재실행**: Phase 2 또는 3부터.
   - 있음 + 새 마일스톤 → `_workspace/`를 `_workspace_prev/`로 옮기고 초기 실행.
3. SPEC이 모호하거나 충돌하면 추측하지 말고 사용자에게 묻는다(SPEC_B §0).

## Phase 1: 구현 (메인 세션)
- SPEC_B의 해당 절과 §14 완료 기준을 먼저 전부 읽는다.
- 비자명한 로직은 테스트를 먼저 쓴다. 테스트는 규칙을 **정확한 값으로** 고정한다(골든 값, 수식 재계산). 코드끼리만 비교하는 테스트는 뮤테이션에 뚫린다. B1 리뷰에서 실제로 뚫렸다.
- ponytail 원칙을 따른다. 표준 라이브러리와 이미 설치된 의존성(aiperf가 끌어오는 pydantic, ruamel.yaml, aiohttp, orjson)을 먼저 쓰고, 옵션과 추상화는 SPEC이 요구할 때만 만든다.
- 게이트: `.venv/bin/ruff format src tests && .venv/bin/ruff check src tests && .venv/bin/python -m pytest -q --cov=personaload`가 전부 통과해야 한다.

## Phase 2: 병렬 리뷰 (서브 에이전트, `model: "opus"`)
두 에이전트를 동시에 띄운다. 사용자가 multi-agent 오케스트레이션을 허용했으면 Workflow로, 아니면 Agent 도구 `run_in_background: true`로 띄운다.
- `personaload-spec-reviewer`: 마일스톤 파일과 SPEC 절을 대조하고, 엣지 케이스 탐침과 뮤테이션으로 테스트를 점검한다.
- `personaload-live-verifier`: `scripts/live_smoke.py`로 실제 AIPerf를 돌린다. 마일스톤이 타이밍·리포트 수치를 다루면 부하 정확도도 잰다.
- 마일스톤 성격상 필요하면 관점을 하나 더 붙인다(예: B3는 SIGINT·워치독, B4는 퇴화 사례). 필요 없으면 붙이지 않는다.

각 리뷰어의 지적마다 **반박 검증** 에이전트를 하나 붙인다. 이 에이전트는 근거를 재현해 보고 real/refuted를 판정하고, 수정안이 틀렸으면 고친다. 결과는 `_workspace/{마일스톤}_{리뷰어}_findings.json`에 저장한다.

## Phase 3: 수정 (메인 세션)
- real로 판정된 지적만 반영한다. 원인이 있는 한 곳에서 고친다. 호출부마다 막지 않는다.
- 수정 하나당 회귀 테스트 하나. 그 수정이 깨지면 실패하는 가장 작은 테스트다.
- 회귀 테스트가 수정을 잡는지 뮤테이션으로 확인할 때는 `PYTHONDONTWRITEBYTECODE=1`을 쓰고 `__pycache__`를 지운다. 원본과 크기가 같은 변이를 같은 초에 쓰면 옛 .pyc가 재사용돼 거짓 SURVIVED가 나온다(B4에서 실제로 발생).
- SPEC을 명확히 하는 변경(키 형식, 예외 조항)은 SPEC_B에 반영하고 사용자 보고에 명시한다. 의미를 바꾸는 변경은 사용자에게 먼저 묻는다.
- Phase 1 게이트를 다시 통과시킨다.

## Phase 4: 완료 기준 검증
- §14의 해당 마일스톤 기준을 **하나씩 직접 실행**해 결과를 표로 남긴다. 테스트로 대신할 수 있으면 테스트 이름을 적는다.
- 수정 뒤 AIPerf 인자나 데이터셋 형식이 바뀌었으면 `live_smoke.py`를 한 번 더 돌린다.

## Phase 5: 보고와 커밋
- 사용자에게 보고한다: 완료 기준 표, 리뷰 지적 수(real/refuted), 주요 수정, SPEC 변경, 남은 위험.
- 커밋은 사용자가 요청하거나 이미 허락한 경우에만 한다.
  - 형식: `<type>: <description>`.
  - 새 의존성은 이유를 본문에 적는다(SPEC_B §0).
  - 사용자 설정상 attribution 줄은 넣지 않는다.
- 다음 마일스톤으로 넘어가기 전에 사용자에게 확인한다.

## 에러 핸들링
- 리뷰어가 실패하면 1회 재시도한다. 재실패하면 그 관점 없이 진행하고, 보고에 "미검증 관점"으로 명시한다.
- AIPerf나 mock이 멈추면 프로세스 그룹을 종료한다. 원인이 레코드 프로세서 경쟁 조건(§8.3)인지 로그로 확인한다.
- 리뷰 지적끼리 충돌하면(예: 수정안 두 개가 반대) 둘 다 보고서에 출처와 함께 남기고, 더 작은 수정을 택한 이유를 적는다.

## 테스트 시나리오
- **정상 흐름**: "B3 진행" → Phase 0에서 B2 커밋 확인 → Phase 1 runner 구현과 테스트 → Phase 2 두 리뷰어와 반박 검증 → Phase 3 real 지적 수정 → Phase 4 B3 기준 표(활성 세션 50±2, Ctrl+C partial, 워치독, 멈춤 없음) → Phase 5 보고, 사용자 확인 후 커밋.
- **에러 흐름**: live-verifier의 AIPerf 실행이 300초간 멈춤 → 프로세스 그룹 종료 → 로그에 `Dataset configuration not received` 확인 → `--record-processors` 누락을 지적으로 보고 → Phase 3에서 수정 → Phase 4에서 `live_smoke.py`로 재확인.
