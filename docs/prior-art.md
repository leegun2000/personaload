# 선행 조사 (M0): 기존 LLM 서빙 벤치마크의 캐릭터챗 워크로드 지원 범위

- 조사일: 2026-09-23. 각 저장소의 main HEAD 소스를 직접 클론해서 읽었다. 버전은 [부록 A](#부록-a-조사-버전)에 있다.
- 방법: 도구군마다 조사 에이전트 하나가 소스를 grep하고 판독한 뒤, 별도의 반박 검증 에이전트가 모든 판정을 다시 확인했다. 검증 단계에서 정정 58건(판정 변경, 근거 라인 수정, 누락 경로 보완)이 반영됐다.
- 판정: ● 네이티브 지원 / ◐ 일부 지원이거나 우회 필요 / ○ 미지원 (소스 grep으로 확인)

## 결론 요약

1. **스펙이 지목한 3종 중 vLLM `bench serve`는 여전히 단발 요청 전용이다.** 레거시 `benchmarks/benchmark_serving.py`는 이제 실행하면 `exit 1`로 끝나는 deprecation shim이다. vLLM에는 별도의 `benchmarks/multi_turn/` 스크립트가 있지만 공유 프리픽스가 1개로 고정돼 있고, 세션 도착 모델과 세션별 think time이 없다.
2. **GenAI-Perf는 단계적 폐기 중이고, 후속작 AIPerf가 personaload 요구사항 대부분을 네이티브로 충족한다.** 멀티턴과 실제 응답 히스토리, 프리픽스 풀, think time, 세션 슬롯 동시성, 다단계 phase/sweep, warmup 제외, cached-token %, goodput/SLA, 실행 중 Prometheus `/metrics`, 시드 재현성을 모두 갖췄다.
3. **GuideLLM(main)도 대부분 충족한다.** system-role 프리픽스 풀(가중 버킷), think time, 동시성/레이트 스윕, warmup, SLO/goodput을 지원한다. 단, `concurrent`의 동시성은 세션 수가 아니라 in-flight 요청 수다.
4. **SGLang `bench_serving`은 Zipf 페르소나 쏠림과 세션 단위 포아송 도착을 네이티브로 가진 유일한 도구다.** 대신 think time, stage, SLO가 없다.
5. **inference-perf**는 프리픽스 풀, think time(agentic 데이터젠 한정), stage/sweep, cached tokens, goodput을 지원한다. 세션 도착은 고정 간격이고 Zipf와 warmup은 없다.
6. **$/1M 출력 토큰 원가를 계산하는 도구는 하나도 없다.** 다만 사후 계산은 한 줄이면 된다.
7. **personaload 요구사항 전체를 네이티브로 충족하는 단일 도구는 없다.** 아무 도구도 한꺼번에 제공하지 않는 조합은 다음과 같다.
   - system-role 페르소나 풀 + Zipf 선택
   - 턴마다 다시 샘플하는 지수분포 think time
   - 세션 단위 포아송 도착 + `max_concurrency` 초과 드롭
   - SLO를 통과한 최대 처리량 단계의 `$/1M` 헤드라인
8. **다만 AIPerf에 커스텀 데이터셋 생성기와 원가 후처리를 붙이면 거의 전부 재현된다.** 스펙 M0의 "기존 도구가 요구사항을 대부분 충족" 조건에 해당하므로 진행 여부 결정이 필요하다. [선택지](#선택지)를 참고한다.

## 표 1. 주요 도구 지원 매트릭스

| 항목 | vLLM<br>bench serve | vLLM<br>multi_turn | GuideLLM | GenAI-Perf | AIPerf | SGLang<br>bench_serving | inference-perf |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| 멀티턴 (히스토리 누적) | ○ | ● | ● | ◐ | ● | ● | ● |
| 히스토리에 실제 응답 사용 | ○ | ● | ● | ● | ● | ● | ● |
| 공유 시스템 프롬프트 풀 | ◐ | ◐ | ● | ● | ● | ● | ● |
| 페르소나 인기 쏠림 (Zipf) | ○ | ○ | ◐ | ○ | ◐ | ● | ○ |
| 턴 간 think time | ○ | ◐ | ● | ● | ● | ○ | ● |
| 세션 단위 포아송 도착 | ○ | ○ | ◐ | ○ | ◐ | ● | ◐ |
| 동시성 + 다단계 stage | ◐ | ◐ | ● | ◐ | ● | ◐ | ● |
| warmup 제외 | ◐ | ● | ● | ◐ | ● | ◐ | ◐ |
| TTFT/ITL/E2E 백분위 | ● | ◐ | ● | ● | ● | ● | ● |
| 서버 usage 기반 토큰 | ● | ○ | ● | ○ | ● | ◐ | ● |
| cache hit% 리포트 | ○ | ◐ | ◐ | ○ | ● | ◐ | ● |
| SLO 판정 / goodput | ● | ○ | ● | ● | ● | ○ | ● |
| $/1M 토큰 원가 | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| 실행 중 Prometheus | ○ | ○ | ○ | ○ | ● | ○ | ○ |
| 시드 재현성 | ● | ◐ | ● | ● | ● | ● | ◐ |
| 클라이언트 lag 감지 | ◐ | ○ | ◐ | ◐ | ◐ | ○ | ◐ |

- vLLM `benchmarks/benchmark_serving.py`(레거시)는 모든 항목이 ○다. deprecation shim이라 아무것도 실행하지 않는다.
- GenAI-Perf는 README에 "being phased out"이 명시돼 있고 AIPerf로 안내한다.

## 표 2. 보조 도구 / 특수 목적 스크립트

| 항목 | vLLM bench sweep | SGLang hicache<br>bench_multiturn | SGLang hicache<br>bench_mix | SGLang<br>agentic-rollout | prod-stack<br>multi-round-qa | HF inference-<br>benchmarker | LLMPerf |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| 멀티턴 (히스토리 누적) | ○ | ● | ● | ● | ● | ◐ | ○ |
| 히스토리에 실제 응답 사용 | ○ | ● | ● | ● | ● | ● | ○ |
| 공유 시스템 프롬프트 풀 | ◐ | ○ | ○ | ○ | ◐ | ○ | ○ |
| 페르소나 인기 쏠림 (Zipf) | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| 턴 간 think time | ○ | ○ | ◐ | ● | ◐ | ○ | ○ |
| 세션 단위 포아송 도착 | ○ | ○ | ○ | ◐ | ◐ | ○ | ○ |
| 동시성 + 다단계 stage | ● | ◐ | ◐ | ◐ | ◐ | ● | ◐ |
| warmup 제외 | ◐ | ○ | ○ | ○ | ◐ | ● | ○ |
| TTFT/ITL/E2E 백분위 | ● | ◐ | ◐ | ◐ | ◐ | ● | ◐ |
| 서버 usage 기반 토큰 | ● | ● | ◐ | ◐ | ● | ○ | ○ |
| cache hit% 리포트 | ○ | ● | ◐ | ● | ○ | ○ | ○ |
| SLO 판정 / goodput | ● | ○ | ○ | ○ | ○ | ○ | ○ |
| $/1M 토큰 원가 | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| 실행 중 Prometheus | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| 시드 재현성 | ● | ◐ | ○ | ● | ◐ | ○ | ◐ |
| 클라이언트 lag 감지 | ◐ | ○ | ○ | ◐ | ◐ | ◐ | ○ |

- **vLLM bench sweep**: `vllm bench serve`를 반복 실행하는 오케스트레이터다. 멀티턴 스크립트와는 플래그가 맞지 않아 함께 쓸 수 없다.
- **SGLang hicache / agentic-rollout**: HiCache나 에이전틱 RL 롤아웃용 스크립트다. 대부분 SGLang 네이티브 `/generate` 전용이다.
- **production-stack multi-round-qa**: 취지는 personaload와 가장 비슷하다(사용자 × 라운드 채팅). 그러나 공유 프리픽스가 1개뿐이고, think time은 `num_users/qps`에서 유도된 고정값이다.
- **HF inference-benchmarker**: Rust/tokio 기반이다. 멀티턴이 히스토리를 user 메시지 하나로 평탄화해서 보낸다.
- **LLMPerf**: 저장소가 archived 상태다. 마지막 커밋은 2024-12이다.

## 핵심 요구사항별 상세

### 1. 멀티턴 + 실제 응답 히스토리

| 도구 | 방식 | 한계 |
|---|---|---|
| vLLM bench serve | 없음. ShareGPT, MT-Bench도 첫 user 턴만 사용한다 | — |
| vLLM multi_turn | 턴마다 `messages[:turn]`을 보내고, 데이터셋의 assistant 메시지를 실제 스트리밍 응답으로 덮어쓴다. 합성 설정에서 `num_turns` 분포를 지원한다 | `--max-turns`는 user와 assistant를 합친 메시지 수다 |
| GuideLLM | synthetic `turns=N`으로 대화 DAG를 만들고 실제 `res.text`를 assistant로 재전송한다 | 합성 데이터의 턴 수는 고정 정수이고 분포가 없다. 가변 턴 수는 파일 데이터셋으로만 가능하다 |
| GenAI-Perf | `--num-sessions`, `--session-turns-mean/stddev`. perf_analyzer가 `choices[0].message`를 히스토리에 넣는다 | 세션 콜백이 비스트리밍 JSON을 가정한다. 그래서 `--streaming`이면 TTFT/ITL을 측정할 수 없다(코드 판독 기준) |
| AIPerf | `--conversation-num`, `--conversation-turn-mean/stddev`. 기본 컨텍스트 모드가 실제 응답을 누적한다 | 턴 수 분포는 정규분포만 된다. YAML의 다른 분포는 평균값으로 축약된다 |
| SGLang bench_serving | GSP `--gsp-num-turns N` 또는 agentic-trace를 쓴다. 실제 `generated_text`를 append한다 | 턴 수가 고정이다. 실패한 턴도 빈 assistant 메시지로 계속 진행한다 |
| inference-perf | 경로가 셋이다. chat 멀티턴은 `synthetic_agentic`(에이전트형)이다 | `shared_prefix` 멀티턴은 매 라운드 같은 질문을 `/v1/completions`에 연결 텍스트로 재전송한다 |

### 2. 공유 페르소나 (풀 크기 · 역할 · 인기 쏠림)

| 도구 | 풀 크기 / 길이 | 메시지 역할 | 선택 분포 |
|---|---|---|---|
| vLLM bench serve | `prefix_repetition`: `--prefix-repetition-num-prefixes`, `-prefix-len` | user 메시지 안 | 균등 |
| vLLM multi_turn | `common_prefix_num_tokens`: **1개 고정** | 첫 user 메시지 안. system role은 미지원(assert로 막힘) | — |
| GuideLLM | `prefix_count`+`prefix_tokens` 또는 `prefix_buckets` | **system role**. 매 턴 재전송한다 | 버킷 정수 가중치. Zipf는 사용자가 직접 계산해 넣어야 한다 |
| GenAI-Perf | `--num-prefix-prompts`, `--prefix-prompt-length` | 첫 턴 앞 | 균등 |
| AIPerf | `--num-prefix-prompts`(풀) 또는 `--shared-system-prompt-length`(1개) | 풀은 user role이고, system role은 1개만 가능하다. 둘을 같이 쓸 수 없다 | 균등. system-role 풀이나 Zipf가 필요하면 커스텀 multi_turn JSONL을 만든다(선두 system 턴이 `system_message`로 hoist된다) |
| SGLang bench_serving | `--gsp-num-groups`, `--gsp-system-prompt-len` | 랜덤 토큰 텍스트를 첫 user 메시지에 붙인다 | **`--gsp-group-distribution zipf --gsp-zipf-alpha`** |
| inference-perf | `num_unique_system_prompts`, `system_prompt_len`(분포 가능) | 프리픽스 텍스트 | 균등 |

### 3. Think time (한 세션 내 턴 사이 대기)

| 도구 | 설정 | 분포 | 비고 |
|---|---|---|---|
| vLLM multi_turn | 세션별 설정이 없다. 클라이언트가 요청마다 `exp(1/--request-rate)`만큼 sleep한다 | 지수 | 턴 간격은 다른 대화들의 지연에서 부수적으로 생긴다 |
| GuideLLM | `delay`, `delay_stdev`, `delay_min/max` | 고정 / 균등 / 클램프된 정규 | **세션당 1회 샘플해서 모든 턴에 같은 값을 쓴다.** 지수분포는 없다 |
| GenAI-Perf | `--session-turn-delay-mean/stddev` | 정규 | — |
| AIPerf | `--conversation-turn-delay-mean/stddev` | 정규 | YAML이 lognormal 등을 받아도 합성 composer가 평균으로 축약한다. 임의 분포는 커스텀 데이터셋의 턴별 `delay`로 넣는다 |
| SGLang bench_serving | 없음 | — | 다음 턴을 즉시 보낸다 |
| inference-perf | `synthetic_agentic.user_think_time_sec` | fixed / normal / lognormal / uniform / poisson | 지수분포가 없다. `shared_prefix` 멀티턴에서는 쓸 수 없다 |

### 4. 세션 단위 도착 · 동시성의 의미

| 도구 | 도착 모델 | "동시성"이 세는 것 |
|---|---|---|
| vLLM multi_turn | closed-loop이고 도착 과정이 없다 | 클라이언트 프로세스 수(in-flight 1개씩) |
| GuideLLM | `poisson`/`constant`가 요청 단위 레이트다. 준비된 턴이 새 세션보다 우선한다 | **in-flight 요청 수**. think time 동안 열린 세션 수는 무제한이다 |
| GenAI-Perf | 세션 모드는 closed-loop뿐이다. `session_request_rate`는 설정 이름만 있고 구현되지 않았다 | `--session-concurrency` = 세션 수 |
| AIPerf | `--request-rate`가 모든 턴을 페이싱하고 이어지는 턴이 우선한다. 세션 시작이 독립 포아송 과정이 아니다. 우회: `--fixed-schedule`에 사전 생성한 첫 턴 타임스탬프를 넣는다 | **multi-turn에서는 세션 슬롯**(think time 포함). personaload의 concurrency 모드와 같은 의미다 |
| SGLang bench_serving | multi-turn 모드에서 `--request-rate`가 **세션 시작 포아송 레이트**가 된다 | `--max-concurrency` = 세션 수 |
| inference-perf | `trace_session_replay`의 `session_rate`는 **고정 간격**이다. replay-graph 데이터젠에서만 쓸 수 있다 | `concurrent_sessions` 상한 |

## AIPerf로 personaload 스펙을 재현할 때의 격차

가장 근접한 AIPerf 기준으로, 스펙 항목별 대응을 정리했다.

| personaload 스펙 | AIPerf 대응 | 추가 작업 |
|---|---|---|
| 멀티턴 + 실제 응답 히스토리 | 네이티브 | — |
| 페르소나 풀(system role) + Zipf 선택 | 커스텀 multi_turn JSONL(세션마다 선두 system 턴) | 데이터셋 생성기 |
| 턴 수 uniform 분포 | 합성은 정규분포만 지원한다 | 데이터셋 생성기 |
| user 입력 lognormal 길이 / ko·en 선택 | 합성 텍스트 | 데이터셋 생성기 |
| think time 지수분포, 턴마다 재샘플 | 합성은 정규분포만 지원한다. 커스텀 턴별 `delay` 사용 | 데이터셋 생성기 |
| concurrency 모드(활성 세션 N 유지) | `--concurrency` = 세션 슬롯 | — |
| arrival_rate 모드(세션 포아송 도착) | `--fixed-schedule` + 사전 생성 타임스탬프 | 데이터셋 생성기. `max_concurrency` 초과 시 드롭+카운트는 **미확인** |
| 다단계 stage + stage별 warmup | YAML phases, 콤마 리스트 sweep, `--warmup-*` | 설정 작성 |
| TTFT/ITL/E2E p50~p99, 서버 usage, cache hit% | 네이티브(`--use-server-token-count`) | — |
| SLO 판정 + "통과 단계 중 최대 처리량" 헤드라인 | `--goodput`, `--search-sla` 레시피 | 헤드라인 선택은 후처리 |
| `$/1M out` 원가 | 없음 | 후처리 1식 |
| 실행 중 Prometheus | `--api-port` → `/metrics` | 메트릭 이름이 스펙과 다르다 |
| 스케줄러 lag p99 > 100ms 경고 | fixed-schedule에서만 `replay_sched_lag`와 경고가 있고, request-rate 모드에는 없다 | concurrency 모드는 해당 없음 |
| 단일 Go 바이너리, CGO 없음 | Python, 멀티프로세스(ZMQ) | 대체 불가 |
| mock 서버 | 없음. GuideLLM에는 내장 mock 서버가 있다 | — |

## personaload를 만든다면 반영할 교훈

조사 중 발견한 기존 도구의 함정 가운데 personaload 설계에 바로 적용할 것들이다.

- **시드 분기**: AIPerf는 컴포넌트마다 `SHA-256("seed:identifier")`로 자식 RNG를 파생한다. 컴포넌트를 추가해도 다른 시퀀스가 바뀌지 않으므로 M2 재현성 설계에 그대로 쓸 만하다.
- **Lag 측정에서 think time 제외**: GuideLLM은 dispatch delay가 think-time 게이트 대기까지 포함하는 문제가 있다. personaload는 "think time 종료 후 예정 전송 시각 → 실제 전송 시각"으로 정의해야 한다.
- **cache hit% 분모**: SGLang은 멀티턴에서 모든 라운드가 첫 턴의 `prompt_len`을 물려받아 hit%가 100%를 넘을 수 있다. 분모는 요청별 `usage.prompt_tokens`의 합으로 한다(스펙과 일치).
- **ITL 정의**: GuideLLM과 AIPerf의 ITL은 요청별 평균을 토큰 수로 가중한 값이다. 청크 간 간격 분포가 아니다. 스펙의 "청크 간 간격 분포"는 이들과 수치가 다를 수 있으니 리포트에 정의를 명시한다.
- **usage 부재 시 대체**: GuideLLM은 입력 토큰 폴백이 히스토리와 시스템 프롬프트를 빼고 세어서 과소 집계된다. AIPerf는 폴백 없이 None이다. 스펙대로 경고 + 청크 수 대체 + 리포트 표시로 가되, 입력 토큰 폴백도 정의해야 한다(아래 질문 2).
- **실패 턴 처리**: SGLang은 실패한 턴의 빈 assistant를 히스토리에 넣고 계속 진행한다. hicache 스크립트는 빈 응답을 건너뛰어 user 메시지가 연속된다. personaload는 실패 시 세션을 어떻게 처리할지 정해야 한다(아래 질문 3).

### 구현 전에 정해야 할 스펙 모호점 (M1 착수 시 질문 예정)

1. **reasoning_content의 히스토리 포함 여부.** 첫 토큰 정의에는 reasoning_content가 들어간다. 그런데 다음 턴 히스토리의 assistant 메시지에도 넣을지는 스펙에 없다. AIPerf는 reasoning-only 청크까지 이어 붙이고, GuideLLM은 옵션으로 둔다.
2. **usage가 없을 때 입력 토큰(PromptTokens) 대체값.** 스펙은 출력만 "청크 수로 대체"라고 정했다.
3. **턴 실패(에러 상태코드, 스트림 중단) 시 세션 처리.** 세션 종료, 해당 턴 재시도, 빈 응답으로 계속 중 하나를 정해야 한다.
4. **`max_output_tokens` 강제 여부.** 대부분의 도구는 `ignore_eos` + `max_completion_tokens`로 출력 길이를 고정한다. 캐릭터챗의 자연스러운 EOS를 허용할지 정해야 한다. `extra_body`로 사용자가 넣게 할 수도 있다.

## 선택지

| 안 | 내용 | 장점 | 단점 |
|---|---|---|---|
| **A. 스펙대로 personaload 구현 (M1~M6)** | Go 단일 바이너리. 캐릭터챗 모델을 1급 기능으로 둔다 | 모든 요구를 네이티브 충족. 원가 헤드라인과 mock 서버 내장. 의존성 최소 | 구현량이 가장 크다. 기존 도구와 수치 정의 차이(ITL 등)를 직접 관리해야 한다 |
| **B. AIPerf 채택 + 얇은 래퍼** | 워크로드 생성기(Zipf 페르소나, 지수 think time, 포아송 세션 시작 → AIPerf multi_turn JSONL)와 리포트 후처리($/1M, SLO 헤드라인) | 가장 빠르다. 검증된 측정 엔진과 GPU 텔레메트리, 서버 메트릭 스크랩을 그대로 쓴다 | Python 런타임이 필요하다. arrival_rate 드롭 카운트는 미확인. 세션 도착은 fixed-schedule 우회에 의존한다 |
| **C. 업스트림 기여** | 예: AIPerf 프리픽스 풀에 Zipf와 system role 추가, SGLang에 think time 추가 | 생태계에 남는다 | 리뷰 일정을 통제할 수 없다 |

**권장**: 목적이 **원가 수치 산출**(naive vs vLLM vs +prefix cache vs +FP8 비교)이면 **B**가 가장 빠르다. **personaload라는 도구 자체가 산출물**(Go 단일 바이너리, 공개 도구, 포트폴리오)이면 **A**로 진행한다. 이 경우 위의 교훈과 질문 4개를 M1 전에 확정한다.

## 부록 A. 조사 버전

| 도구 | 저장소 | 커밋 (날짜) |
|---|---|---|
| vLLM bench serve / multi_turn / sweep / legacy | vllm-project/vllm | `0f2a15c9` (2026-09-23) |
| GuideLLM | vllm-project/guidellm | main `948ec6e6` (2026-09-22). 최신 릴리스 v0.7.4 (2026-09-16)도 대조 |
| GenAI-Perf | triton-inference-server/perf_analyzer | `ee519c63` (2026-09-15) |
| AIPerf | ai-dynamo/aiperf | `29e08b5b` (2026-09-23). pyproject 0.13.0, 최신 릴리스 v0.12.0 |
| SGLang (bench_serving, hicache, agentic-rollout) | sgl-project/sglang | `0b0f9472` (2026-09-23) |
| inference-perf | kubernetes-sigs/inference-perf | main `07eeba74` (2026-09-22). 최신 릴리스 v0.7.0 |
| production-stack multi-round-qa | vllm-project/production-stack | `691dffa7` (2026-09-22) |
| HF inference-benchmarker | huggingface/inference-benchmarker | `bad4f947` (2026-05-26) |
| LLMPerf | ray-project/llmperf | `f1d6bed4` (2024-12-08, archived) |

- **주의**: GuideLLM의 SLO/goodput, cached_tokens 파싱, dispatch delay 지표는 **main에만 있다**. 최신 릴리스 v0.7.4에는 없다.

## 부록 B. 핵심 판정 근거 (GitHub 고정 링크)

각 도구의 핵심 6개 항목에 대한 대표 근거. 16개 전 항목의 판정·상세·근거 링크와 검증 정정 내역은 [prior-art-evidence.json](prior-art-evidence.json)에 있다.

**vLLM bench serve**

- 멀티턴 ○: https://github.com/vllm-project/vllm/blob/0f2a15c9277f34c9afe141cf578d1f02b3bebdfe/vllm/benchmarks/datasets/datasets.py#L1335-L1422
- 실제 응답 히스토리 ○: https://github.com/vllm-project/vllm/blob/0f2a15c9277f34c9afe141cf578d1f02b3bebdfe/vllm/benchmarks/serve.py#L1036-L1092
- 공유 프롬프트 풀 ◐: https://github.com/vllm-project/vllm/blob/0f2a15c9277f34c9afe141cf578d1f02b3bebdfe/vllm/benchmarks/datasets/datasets.py#L4348-L4432
- Zipf ○: https://github.com/vllm-project/vllm/blob/0f2a15c9277f34c9afe141cf578d1f02b3bebdfe/vllm/benchmarks/datasets/datasets.py#L4376-L4432
- think time ○: https://github.com/vllm-project/vllm/blob/0f2a15c9277f34c9afe141cf578d1f02b3bebdfe/vllm/benchmarks/serve.py#L401-L515
- 세션 도착 ○: https://github.com/vllm-project/vllm/blob/0f2a15c9277f34c9afe141cf578d1f02b3bebdfe/vllm/benchmarks/serve.py#L438-L515

**vLLM multi_turn**

- 멀티턴 ●: https://github.com/vllm-project/vllm/blob/0f2a15c9277f34c9afe141cf578d1f02b3bebdfe/benchmarks/multi_turn/benchmark_serving_multi_turn.py#L396-L462
- 실제 응답 히스토리 ●: https://github.com/vllm-project/vllm/blob/0f2a15c9277f34c9afe141cf578d1f02b3bebdfe/benchmarks/multi_turn/benchmark_serving_multi_turn.py#L533-L558
- 공유 프롬프트 풀 ◐: https://github.com/vllm-project/vllm/blob/0f2a15c9277f34c9afe141cf578d1f02b3bebdfe/benchmarks/multi_turn/bench_dataset.py#L446-L454
- Zipf ○: https://github.com/vllm-project/vllm/blob/0f2a15c9277f34c9afe141cf578d1f02b3bebdfe/benchmarks/multi_turn/bench_dataset.py#L71-L83
- think time ◐: https://github.com/vllm-project/vllm/blob/0f2a15c9277f34c9afe141cf578d1f02b3bebdfe/benchmarks/multi_turn/benchmark_serving_multi_turn.py#L561-L568
- 세션 도착 ○: https://github.com/vllm-project/vllm/blob/0f2a15c9277f34c9afe141cf578d1f02b3bebdfe/benchmarks/multi_turn/benchmark_serving_multi_turn.py#L649-L670

**GuideLLM**

- 멀티턴 ●: https://github.com/vllm-project/guidellm/blob/948ec6e6eb6ce92b3bbbe774539d0e32fedf1590/src/guidellm/schemas/data/deserializers/synthetic.py#L327-L336
- 실제 응답 히스토리 ●: https://github.com/vllm-project/guidellm/blob/948ec6e6eb6ce92b3bbbe774539d0e32fedf1590/src/guidellm/backends/openai/request_handlers.py#L1016-L1038
- 공유 프롬프트 풀 ●: https://github.com/vllm-project/guidellm/blob/948ec6e6eb6ce92b3bbbe774539d0e32fedf1590/src/guidellm/schemas/data/deserializers/synthetic.py#L37-L52
- Zipf ◐: https://github.com/vllm-project/guidellm/blob/948ec6e6eb6ce92b3bbbe774539d0e32fedf1590/src/guidellm/data/deserializers/synthetic.py#L472-L501
- think time ●: https://github.com/vllm-project/guidellm/blob/948ec6e6eb6ce92b3bbbe774539d0e32fedf1590/src/guidellm/schemas/data/deserializers/synthetic.py#L301-L326
- 세션 도착 ◐: https://github.com/vllm-project/guidellm/blob/948ec6e6eb6ce92b3bbbe774539d0e32fedf1590/src/guidellm/scheduler/strategies.py#L584-L705

**GenAI-Perf**

- 멀티턴 ◐: https://github.com/triton-inference-server/perf_analyzer/blob/ee519c63382237fadd8b92216d667b65fa503d35/genai-perf/genai_perf/parser.py#L669-L718
- 실제 응답 히스토리 ●: https://github.com/triton-inference-server/perf_analyzer/blob/ee519c63382237fadd8b92216d667b65fa503d35/src/session_concurrency/request_handler.cc#L179-L211
- 공유 프롬프트 풀 ●: https://github.com/triton-inference-server/perf_analyzer/blob/ee519c63382237fadd8b92216d667b65fa503d35/genai-perf/genai_perf/parser.py#L474-L481
- Zipf ○: https://github.com/triton-inference-server/perf_analyzer/blob/ee519c63382237fadd8b92216d667b65fa503d35/genai-perf/genai_perf/inputs/retrievers/synthetic_prompt_generator.py#L204-L231
- think time ●: https://github.com/triton-inference-server/perf_analyzer/blob/ee519c63382237fadd8b92216d667b65fa503d35/genai-perf/genai_perf/parser.py#L694-L706
- 세션 도착 ○: https://github.com/triton-inference-server/perf_analyzer/blob/ee519c63382237fadd8b92216d667b65fa503d35/src/session_concurrency/session_concurrency_manager.cc#L104-L135

**AIPerf**

- 멀티턴 ●: https://github.com/ai-dynamo/aiperf/blob/29e08b5b65aa92515811d75bc5ea32caed0dd762/docs/cli-options.md#L754-L773
- 실제 응답 히스토리 ●: https://github.com/ai-dynamo/aiperf/blob/29e08b5b65aa92515811d75bc5ea32caed0dd762/src/aiperf/common/enums/enums.py#L166-L193
- 공유 프롬프트 풀 ●: https://github.com/ai-dynamo/aiperf/blob/29e08b5b65aa92515811d75bc5ea32caed0dd762/docs/cli-options.md#L858-L887
- Zipf ◐: https://github.com/ai-dynamo/aiperf/blob/29e08b5b65aa92515811d75bc5ea32caed0dd762/src/aiperf/dataset/generator/prompt.py#L748-L767
- think time ●: https://github.com/ai-dynamo/aiperf/blob/29e08b5b65aa92515811d75bc5ea32caed0dd762/docs/cli-options.md#L775-L791
- 세션 도착 ◐: https://github.com/ai-dynamo/aiperf/blob/29e08b5b65aa92515811d75bc5ea32caed0dd762/src/aiperf/timing/strategies/request_rate.py#L31-L86

**SGLang bench_serving**

- 멀티턴 ●: https://github.com/sgl-project/sglang/blob/0b0f94726555fe9434a48284603dc4d4d5465030/python/sglang/benchmark/serving.py#L1277-L1335
- 실제 응답 히스토리 ●: https://github.com/sgl-project/sglang/blob/0b0f94726555fe9434a48284603dc4d4d5465030/python/sglang/benchmark/serving.py#L1321-L1333
- 공유 프롬프트 풀 ●: https://github.com/sgl-project/sglang/blob/0b0f94726555fe9434a48284603dc4d4d5465030/python/sglang/benchmark/serving.py#L2626-L2673
- Zipf ●: https://github.com/sgl-project/sglang/blob/0b0f94726555fe9434a48284603dc4d4d5465030/python/sglang/benchmark/serving.py#L2685-L2715
- think time ○: https://github.com/sgl-project/sglang/blob/0b0f94726555fe9434a48284603dc4d4d5465030/python/sglang/benchmark/serving.py#L1306-L1335
- 세션 도착 ●: https://github.com/sgl-project/sglang/blob/0b0f94726555fe9434a48284603dc4d4d5465030/python/sglang/benchmark/serving.py#L1056-L1093

**inference-perf**

- 멀티턴 ●: https://github.com/kubernetes-sigs/inference-perf/blob/07eeba741f29a63fe26a7ef574019f0653d580bd/inference_perf/config/datagen/config.py#L85-L87
- 실제 응답 히스토리 ●: https://github.com/kubernetes-sigs/inference-perf/blob/07eeba741f29a63fe26a7ef574019f0653d580bd/inference_perf/apis/user_session.py#L94-L124
- 공유 프롬프트 풀 ●: https://github.com/kubernetes-sigs/inference-perf/blob/07eeba741f29a63fe26a7ef574019f0653d580bd/inference_perf/config/datagen/config.py#L48-L74
- Zipf ○: https://github.com/kubernetes-sigs/inference-perf/blob/07eeba741f29a63fe26a7ef574019f0653d580bd/inference_perf/datagen/synthetic/shared_prefix_datagen.py#L286-L296
- think time ●: https://github.com/kubernetes-sigs/inference-perf/blob/07eeba741f29a63fe26a7ef574019f0653d580bd/inference_perf/config/datagen/replay.py#L476-L490
- 세션 도착 ◐: https://github.com/kubernetes-sigs/inference-perf/blob/07eeba741f29a63fe26a7ef574019f0653d580bd/inference_perf/config/loadgen/config.py#L78-L116

## 부록 C. AIPerf 라이선스 검토 (B안 채택 전제)

검토일은 2026-09-23이다. 대상은 PyPI 릴리스 `aiperf==0.12.0`(태그 `v0.12.0`, `be53bf29`)과 main `29e08b5b`다.

| 항목 | 결과 |
|---|---|
| AIPerf 본체 | **Apache-2.0**. 근거: `LICENSE`, `pyproject.toml`의 `license = {text = "Apache-2.0"}`, GitHub API의 `license.spdx_id` |
| 전이 의존성 전체 | 129개 패키지. `pip install --dry-run --report aiperf==0.12.0`으로 해석만 하고 설치는 하지 않았다. 패키지 메타데이터의 license expression과 classifier로 분류했다 |
| GPL / AGPL / LGPL / SSPL / BUSL / 독점 라이선스 | **0개** |
| 약한 copyleft (MPL-2.0, 파일 단위) | 3개: `certifi`, `orjson`(MPL-2.0 AND (Apache-2.0 OR MIT)), `tqdm`(MPL-2.0 AND MIT). 해당 파일을 **수정해서 배포**할 때만 소스 공개 의무가 생긴다. 수정 없이 pip로 설치해 쓰는 경우는 해당 없음 |
| 나머지 126개 | MIT, BSD-2/3, Apache-2.0, PSF, ISC, 0BSD, Zlib, CC0 등 permissive. `nvidia-ml-py`도 BSD다 |
| 저장소의 `ATTRIBUTIONS-Python.md` | 129개 패키지 고지가 우리 해석 결과와 일치한다. 직접 의존성 중 `ruamel.yaml`(MIT) 하나만 빠져 있다 |
| AIPerf 공식 컨테이너 이미지 | FFmpeg 8.1.2가 **LGPL-2.1+**로 포함돼 있다(`--disable-gpl --disable-nonfree` 빌드). pip 설치로 쓰면 해당 없다. 이 이미지를 **재배포**할 때만 LGPL 고지와 소스 제공 의무가 생긴다 |

B안 사용 방식에 따른 결론은 다음과 같다.

- **이슈 없음.** 우리 래퍼는 `aiperf` CLI를 별도 프로세스로 호출하고, AIPerf가 읽을 JSONL을 만들고, AIPerf 결과 JSON을 읽는다. 링크나 코드 복제가 없으므로 우리 코드에는 라이선스 의무가 전파되지 않는다. 우리 쪽을 Apache-2.0으로 공개하는 데도 문제가 없다.
- AIPerf를 **수정하거나 번들해서 재배포**하면 Apache-2.0 §4를 따라야 한다. LICENSE를 동봉하고, 변경 사실을 표시하고, `ATTRIBUTIONS*.md`를 유지한다.
- Apache-2.0은 상표권을 허락하지 않는다(§6). 래퍼 이름이나 문서에서 NVIDIA나 AIPerf의 공식 도구로 오인될 표현은 피한다.
- 라이선스 외 주의점:
  - 벤치마크 대상 **모델과 토크나이저**의 라이선스(예: Llama 커뮤니티 라이선스)는 별개다.
  - AIPerf의 `--public-dataset` 다운로드 데이터셋도 각자 라이선스가 있다. B안은 자체 합성 데이터셋을 쓰므로 해당 없다.
  - AIPerf에 업스트림 기여를 하려면 DCO 서명이 필요하다(`DCO.md`).
- 기능 확인: B안에 필요한 AIPerf 기능이 릴리스 `v0.12.0`에도 모두 있다. 커스텀 multi_turn의 system 턴 hoist, 세션 슬롯 동시성, `--fixed-schedule`의 `delay_ms`/`timestamp_ms`, `/metrics` Prometheus, `prompt_tokens_details` 기반 cached tokens, `--shared-system-prompt-length`, `--search-sla`, `--use-server-token-count`를 확인했다. 따라서 PyPI 릴리스에 버전을 고정할 수 있다.
