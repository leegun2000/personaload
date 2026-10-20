# personaload

[English](README.md) | **한국어**

OpenAI 호환 서빙 엔진(vLLM, SGLang, llama.cpp server 등)에 **캐릭터챗·RAG 상담 챗봇 형태의 트래픽**으로 부하를 걸고, 지연·처리량·prefix cache 적중률과 **SLO를 지키는 최대 처리량 기준 원가**(`$/1M 출력 토큰`, `$/1K turns`, `$/session`)를 내는 벤치마크다.

- 챗봇 특성은 워크로드 데이터셋으로 만든다: 멀티턴 히스토리 누적, 공유 페르소나(지시문) system 프롬프트와 Zipf 인기 쏠림, 턴마다 새로 뽑는 think time, 세션 단위 도착, 턴마다 붙는 검색 문서(RAG, 선택).
- 실측 예시: Apple M5 Pro + vLLM Metal, Qwen3 0.6B~4B(MLX 4비트), 32~96토큰의 짧은 답, 동시성 2/4/8/16, 조건마다 1회 측정. prefix caching을 **끄면** 같은 SLO에서 `$/1M out`이 페르소나 5개 공유일 때 1.9~3.8배, 페르소나 500개일 때 1.6~2.1배가 됐다(캐싱의 절감 효과로 보면 47~73%, 38~53%). vLLM은 prefix caching이 기본으로 켜져 있으니, 캐시가 꺼지거나 밀려날 때의 손해로 읽는다. 데이터센터 GPU에서는 차이가 더 작을 수 있다. 자세한 내용: [docs/cache-savings-m5.ko.md](docs/cache-savings-m5.ko.md).
- CLI 출력, `report.md`, 인계 문서는 영어다. 실측 기록은 영어·한국어 둘 다 있고, 설계 문서(SPEC_B.md)와 선행 도구 조사(docs/prior-art.md)는 한국어다.
- 부하 생성과 요청별 측정은 [AIPerf](https://github.com/ai-dynamo/aiperf) 0.12.0이 한다. personaload는 데이터셋 생성, 단계 실행, 리포트, 테스트용 mock 서버를 맡는다.
- 설계 문서: [SPEC_B.md](SPEC_B.md). AIPerf 동작의 근거: [docs/aiperf-v0.12.0-facts.json](docs/aiperf-v0.12.0-facts.json).

## 설치

Python 3.11~3.13이 필요하다(AIPerf 제약). macOS(arm64)와 Linux(amd64, arm64)에서 동작한다.

```bash
git clone https://github.com/leegun2000/personaload && cd personaload
python3.12 -m venv .venv            # 3.11~3.13 중 하나 (맥: brew install python@3.12)
.venv/bin/pip install -e .          # 개발: pip install -e ".[dev]"
source .venv/bin/activate
```

- 전용 가상환경을 쓴다. AIPerf가 의존성을 엄격히 고정하고 많이 끌어와 환경이 약 0.9 GB다.
- AIPerf 0.12.0이 함께 설치된다. 다른 버전이면 `personaload run`이 시작하지 않는다.
- 토큰 수 계산용 tiktoken `o200k_base`를 첫 실행 때 한 번 내려받는다. 오프라인 환경이면 `TIKTOKEN_CACHE_DIR`에 미리 채워 둔다.
- Linux arm64에서는 AIPerf 의존성 `crick`을 빌드하므로 C 컴파일러가 필요하다.

## 빠른 시작: mock 서버로 1분

터미널 두 개를 쓴다. 새 터미널마다 저장소 루트에서 `source .venv/bin/activate`를 먼저 실행한다.

```bash
personaload mockserver                    # 127.0.0.1:8000, TTFT 200ms, 스트림당 40 tok/s
```

```bash
personaload validate scenarios/smoke.yaml # 기본값 적용 결과, 단계별 세션 수·파일 크기 추정, AIPerf 명령
personaload run scenarios/smoke.yaml      # 약 1분 (첫 실행은 토크나이저 파일도 받는다)
```

단계마다 진행 줄이 출력된다. 끝나면 `results/smoke-<시각>/report.md`에 헤드라인과 단계별 표가 나온다. `target.base_url`에 아무것도 떠 있지 않으면 첫 단계 전에 멈추고 그렇게 알려 준다.

```
**Headline**: stage 02: $0.000/1M out, $0.0000/1K turns, $0.000000/session (out tok/s 63.2, SLO PASS)

| stage | conc | active sess | req/s | out tok/s | TTFT p50/p95/p99 (ms) | ITL p50/p95 (ms) | E2E p95 (ms) | err% | cache hit% | SLO | $/1M out | $/1K turns | $/session |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 01 | 2 | 2.0 (2–2) | 0.73 | 39.3 | 201/202/202 | 25.0/26.0 | 2089 | 0.0% | 68.6% (cached reported 11/11) | PASS | $0.000 | $0.0000 | $0.000000 |
| 02 | 4 | 4.0 (4–4) | 1.33 | 63.2 | 201/201/202 | 25.0/26.0 | 2283 | 0.0% | 84.5% (cached reported 20/20) | PASS | $0.000 | $0.0000 | $0.000000 |
```

mock 서버 옵션: `--ttft`, `--tps`, `--jitter`, `--error-rate`(HTTP 500 비율), `--cache-ttft-factor`(prefix cache 적중 시 TTFT 배율), `--no-usage`, `--seed`. 같은 seed와 같은 요청이면 출력 길이와 에러 여부가 항상 같다.

## 맥북에서 llama.cpp로 실행

아래 기동 스크립트도 8000번 포트를 쓰므로 mock 서버를 먼저 끈다.

```bash
brew install llama.cpp
scripts/llama-cpp-macos.sh               # 첫 실행 때 모델(약 0.8 GB)을 Hugging Face에서 내려받는다
```

서버가 `http://127.0.0.1:8000/v1`에 `local-model`이라는 이름으로 뜨면 다른 터미널에서 실행한다(`source .venv/bin/activate` 먼저).

```bash
personaload run scenarios/smoke.yaml
```

- 모델은 `MODEL=<HF repo[:quant]>`로 바꾼다. 기본값은 `ggml-org/gemma-3-1b-it-GGUF`다.
- 요청 슬롯은 `PARALLEL`(기본 4), 슬롯당 컨텍스트는 `CTX_PER_SLOT`(기본 4096)으로 바꾼다. 시나리오의 최대 동시성보다 슬롯이 적으면 서버 안에서 대기가 생겨 TTFT가 늘어난다.
- `smoke.yaml`은 `max_tokens_field: max_tokens`를 쓴다. llama.cpp, vLLM, mock이 모두 받는 필드다.
- 로컬 장비라 원가는 `gpu_hourly_usd: 0`으로 두었다. 헤드라인의 `$0.000`은 그 뜻이다.

## GPU 서버에서 vLLM으로 실행

Linux와 NVIDIA GPU를 가정한다.

```bash
python -m venv ~/vllm && ~/vllm/bin/pip install vllm    # personaload와 다른 venv
PATH=~/vllm/bin:$PATH scripts/vllm-linux.sh              # 기본 모델 Qwen/Qwen3-8B, max-model-len 32768
```

- `--enable-prompt-tokens-details`를 켜 두었다. 그래야 응답 usage에 cached 토큰이 실려 리포트에 cache hit%가 나온다.
- 기본 바인딩은 `127.0.0.1`이다. 다른 장비에서 부하를 걸려면 `HOST=0.0.0.0`으로 띄우고 시나리오의 `target.base_url`을 그 장비 주소로 바꾼다. 인증이 없으므로 신뢰하는 네트워크에서만 쓴다.
- Qwen3는 기본으로 thinking을 한다. 끄지 않으면 짧은 `max_tokens`를 추론에 다 써 버린다(smoke.yaml 실측: 응답 48/48이 상한에서 끊김, 끈 뒤 30/55). 캐릭터챗에서 끄려면 시나리오에 `extra_body: {chat_template_kwargs: {enable_thinking: false}}`를 넣는다(예시 시나리오에 주석으로 있다).

Apple Silicon 맥에서도 같은 `scripts/vllm-linux.sh`를(이름과 달리) [vllm-metal](https://github.com/vllm-project/vllm-metal)(vLLM의 Metal/MLX 플러그인)로 같은 스크립트를 쓸 수 있다. 모델은 MLX 형식(`mlx-community/*`)이다.

```bash
brew tap vllm-project/vllm-metal https://github.com/vllm-project/vllm-metal
brew install vllm-project/vllm-metal/vllm-metal        # vLLM + Metal 플러그인, 약 1.9 GB
MODEL=mlx-community/Qwen3-0.6B-4bit MAX_LEN=8192 scripts/vllm-linux.sh
```

## 예시 시나리오

| 시나리오 | 내용 | 시간 |
|---|---|---|
| [smoke.yaml](scenarios/smoke.yaml) | 페르소나 3개, 2~4턴, 동시성 2·4 | 약 1분 |
| [persona-shared.yaml](scenarios/persona-shared.yaml) | 페르소나 5개를 Zipf로 공유, 5~30턴, 동시성 10·50·200, Prometheus 9090 | 약 12분 |
| [persona-diverse.yaml](scenarios/persona-diverse.yaml) | 페르소나 500개를 고르게 사용, 나머지는 persona-shared와 같다 | 약 12분 |
| [long-context.yaml](scenarios/long-context.yaml) | 30~50턴, 히스토리가 평균 약 1.1만 토큰(최대 약 2.5만)까지 쌓인다, 동시성 8·32 | 약 11분 |
| [rag-assistant.yaml](scenarios/rag-assistant.yaml) | 검색(RAG) 상담 챗봇: 지시문 1개 공유, 턴마다 검색 문서 800~2000토큰, $/session으로 상담 건당 원가 | 약 11분 |

## 결과 비교

```bash
personaload report results/persona-shared-20260923-101500 results/persona-diverse-20260923-103000
```

각 디렉터리의 `stage_NN/stage.json`만 읽어 헤드라인과 단계 행을 한 표로 낸다. 요청 레코드를 지운 결과(`record_requests: false`)도 비교된다. 같은 단계 번호의 데이터셋 sha256이 다르면 표 위에 "단계 k의 워크로드가 다름" 경고가 나온다.

`--handoff handoff.md`를 붙이면 다른 세션의 코딩 에이전트에게 넘길 인계 문서를 함께 쓴다. 마지막 디렉터리 결과를 기준으로 단계 실패, SLO 위반, 캐시·usage 문제를 근거 수치·원인 후보·할 일·완료 조건과 함께 정리하고, 다시 재는 명령을 절대 경로로 넣는다.

```bash
personaload report results/before-20260923-101500 results/after-20260924-093000 --handoff handoff.md
```

## Prometheus

시나리오에 `output.prometheus_port`를 주면 실행 중 `http://<prometheus_host>:<port>/metrics`가 열린다(AIPerf `--api-port`).

```bash
curl -s http://127.0.0.1:9090/metrics | grep aiperf_time_to_first_token_ms
```

- 메트릭 이름은 AIPerf 규칙을 따른다: `aiperf_time_to_first_token_ms`(summary, quantile 라벨), `aiperf_inter_token_latency_ms`, `aiperf_request_latency_ms`, `aiperf_output_token_throughput_avg_tps`, `aiperf_request_throughput_avg_requests_per_sec`, `aiperf_request_error_rate_avg_percent`, `aiperf_overall_usage_prompt_cache_read_pct_avg_percent`.
- 5초마다 갱신된다. 켜면 단계가 끝날 때 AIPerf가 API 프로세스를 정리하느라 단계마다 몇 초 더 걸린다. 포트가 이미 쓰이고 있으면 첫 단계 전에 오류로 멈춘다.
- 단계마다 AIPerf 프로세스가 바뀌므로 단계 사이에 엔드포인트가 잠시 내려가고, `benchmark_id` 라벨로 단계를 구분한다.
- 실시간 값은 warmup 구간을 포함한다. 에러가 0이면 에러율 메트릭이 노출되지 않는다(대시보드에서 no data를 0으로 처리).
- 실시간 `aiperf_inter_token_latency_ms`는 요청별 평균이라, 청크 간격을 모두 모은 리포트 ITL과 정의가 다르다. 활성 세션 수는 리포트에서만 계산한다.

## 시나리오 필드와 기본값

전체 예시는 [SPEC_B.md §6](SPEC_B.md)에 있다. 기간은 `500ms`, `30s`, `3m`, `1h` 형식이다. 분포는 `{dist: uniform|lognormal|exponential|constant, ...}`이고 `dist`를 생략하면 uniform이다. 값은 `min`/`max`로 클램프한다.

| 필드 | 뜻 | 기본값 |
|---|---|---|
| `name`, `seed` | 결과 디렉터리 이름, 워크로드 시드(같은 시드면 데이터셋이 바이트 단위로 같다) | `name` 필수, `seed` 0 |
| `target.base_url`, `target.model` | OpenAI 호환 엔드포인트와 모델 이름 | 필수 |
| `target.api_key_env` | API 키를 담은 환경변수 이름(키는 결과 파일에 `***`로 가린다. 비어 있으면 `run`이 멈춘다) | 없음 |
| `target.timeout` | 요청 타임아웃 | 120s |
| `target.tokenizer` | AIPerf 토크나이저 | `builtin`(o200k_base) |
| `target.max_tokens_field` | `max_completion_tokens` 또는 `max_tokens` | `max_completion_tokens` |
| `target.extra_body` | 요청 본문에 그대로 넣을 엔진별 파라미터 | `{}` |
| `personas.count`, `personas.system_prompt_tokens` | 합성 페르소나 수와 system 프롬프트 길이 분포 | 필수(synthetic) |
| `personas.source`, `personas.file` | `synthetic` 또는 `file`(각 줄 `{"name", "system_prompt"}`) | `synthetic` |
| `session.turns`, `user_input_tokens`, `max_output_tokens`, `think_time` | 세션 모양 분포 네 개 | 필수 |
| `session.language` | 합성 문장 언어 `en` / `ko` | `en` |
| `session.retrieved_tokens` | RAG: 턴마다 이 길이의 검색 문서를 사용자 입력 앞에 붙인다. 문서는 히스토리에도 남는다 | 없음(RAG 끔) |
| `session.persona_selection`, `zipf_s` | 페르소나 고르는 방식 `uniform` / `zipf` | `uniform`, 1.0 |
| `load.mode` | `concurrency`(세션 슬롯 수 고정) 또는 `arrival_rate`(포아송 세션 도착) | 필수 |
| `load.stages` | concurrency: `{concurrency, duration, warmup, sessions?}`, arrival_rate: `{sessions_per_sec, duration, warmup, max_concurrency}` | 필수, `warmup` 0s |
| `load.drain_timeout` | duration이 끝난 뒤 진행 중 요청을 기다리는 시간 | `target.timeout`과 같음 |
| `load.session_pool_factor` | concurrency 단계의 세션 수 여유 배수 | 3 |
| `slo.ttft_p95`, `slo.itl_p95`, `slo.error_rate_max` | SLO 기준(에러율은 0~1 분수) | 필수 |
| `cost.gpu_hourly_usd`, `gpu_count`, `label` | 원가 계산 입력 | `gpu_hourly_usd` 필수, 1, `""` |
| `output.dir` | 결과 기본 디렉터리(`run --out`으로 덮어쓴다) | `results/` |
| `output.prometheus_port`, `prometheus_host` | 실시간 `/metrics` | 없음(비활성), `127.0.0.1` |
| `output.record_requests` | false면 단계가 끝난 뒤 요청 레코드 파일을 지운다(`stage.json`은 남는다) | true |

- `think_time`은 `constant`가 아니면 `max`가 필요하다.
- 상대 경로 `personas.file`은 시나리오 파일이 아니라 현재 디렉터리 기준이다.

`personaload validate`가 규칙 위반을 오류로, 의심스러운 설정(예: `think_time.max > 60s`, `drain_timeout < target.timeout`)을 경고로 알려 준다.

## 측정 정의

리포트는 AIPerf 집계 JSON이 아니라 **요청별 레코드**(`profile_export.jsonl`)에서 직접 계산한다. 자세한 규칙은 [SPEC_B.md §9](SPEC_B.md)에 있다.

- **구간**: `t0`는 첫 요청이 발행된 시각(arrival_rate는 예정 시각을 뺀 최솟값)이다. `W = t0 + warmup`, `E = W + duration`, `E_eff = min(E, Ctrl+C·워치독 시각)`. warmup은 AIPerf warmup phase를 쓰지 않고 집계에서만 뺀다. AIPerf warmup은 페르소나 prefix cache를 데우지 못하기 때문이다.
- **지연·에러율·cache hit%**: `[W, E_eff)` 안에서 **시작한** 요청. 지연 백분위는 성공 요청만 쓴다.
- **처리량(req/s, out tok/s)**: `[W, E_eff)` 안에서 **끝난** 성공 요청 ÷ `(E_eff − W)`.
- **TTFT, E2E**: AIPerf 값. p50/p90/p95/p99(선형 보간).
- **ITL**: 데이터가 있는 SSE 청크 사이 간격을 대상 요청 전체에서 모은 분포다. AIPerf `inter_token_latency`(요청별 평균)는 참고로 JSON에만 싣는다.
- **에러율**: 에러 ÷ (성공 + 에러). drain 뒤 취소된 요청 c건은 레코드가 없으므로 로그에서 세어 `(에러+c) ÷ (성공+에러+c)`로 넣고, 지연 백분위 옆에 "c건 검열(하한값)"을 붙인다.
- **cache hit%**: Σ cached 토큰 ÷ Σ 입력 토큰(usage에 입력 토큰이 있는 성공 요청). "cached 보고 m/n건"으로 서버가 cached 값을 보고한 요청 수를 함께 보인다.
- **usage가 없는 서버**: 출력 토큰을 청크 수로 대체하고 경고한다. 입력 토큰과 cache hit%는 N/A다.
- **활성 세션**: 세션 인스턴스마다 turn 0 발행부터 마지막 턴 종료까지를 구간으로 잡은 계단 함수의 시간가중 평균·최소·최대. 단계 끝에 잘린 세션은 끝까지 슬롯을 잡은 것으로 본다.
- **SLO**: `TTFT p95 ≤ slo.ttft_p95`, `ITL p95 ≤ slo.itl_p95`, `에러율 ≤ slo.error_rate_max`. 값이 있는 조건 하나라도 어기면 FAIL, 어긴 것 없이 N/A가 있으면 N/A, 나머지는 PASS다.
- **경고**: 데이터셋 재사용, think lag p99 > 100ms(클라이언트 병목 의심), 시계 보정 의심, AIPerf 워커 CPU 경고, drain·중단 취소 수, usage 없음.
- arrival_rate 단계는 시작 지연 p50/p95/p99, 대기 세션(시작 지연 > 100ms), 미시작 세션 수를 함께 낸다. `max_concurrency`를 넘은 세션은 버리지 않고 대기한다.

## 원가 계산식

```
$/1M out    = gpu_hourly_usd × gpu_count ÷ (out tok/s × 3600) × 1,000,000
$/1K turns  = gpu_hourly_usd × gpu_count ÷ (req/s × 3600) × 1,000
$/session   = $/1K turns ÷ 1,000 × E[session.turns]
```

`$/1K turns`와 `$/session`은 상담 1건·대화 1회당 원가로 보고 싶을 때 쓴다(턴 하나 = 요청 하나). E[turns]는 클램프 후 기대값이다.

**헤드라인**(그 단계의 `$/1K turns`, `$/session` 포함)은 SLO PASS이고 중단·실패가 아닌 단계 중 out tok/s가 가장 높은 단계의 `$/1M out`이다(같으면 단계 번호가 작은 쪽). 그런 단계가 없으면 "SLO를 통과한 단계 없음"이라고 쓴다. out tok/s가 0이면 원가는 N/A다.

## 결과 디렉터리

```
results/<name>-<YYYYmmdd-HHMMSS>/
  scenario.resolved.yaml      # 기본값 적용 결과
  report.json / report.md     # 단계별 표, 헤드라인, 경고
  stage_01/
    dataset.jsonl             # AIPerf multi_turn 데이터셋 (시드로 재생성 가능)
    aiperf.argv.json          # 실행한 AIPerf 명령과 환경변수 (API 키는 ***)
    aiperf.console.log        # AIPerf 표준 출력
    aiperf/                   # AIPerf 산출물: profile_export.jsonl, logs/aiperf.log, 집계 JSON
    stage.json                # 단계 집계 결과 (personaload report가 읽는다)
```

- AIPerf `inputs.json`(대략 데이터셋 × 평균 턴 수 크기)은 단계가 끝나면 항상 지운다.
- 단계가 실패하면(AIPerf 비정상 종료, 워치독) 남은 단계는 실행하지 않는다. AIPerf 0.12.0은 한 단계의 요청이 전부 실패해도 비정상 종료하므로, 대상 서버가 죽었으면 첫 단계에서 멈춘다.
- **Ctrl+C 한 번**: AIPerf에 SIGINT를 한 번 전달하고, 부분 결과를 `partial` 단계로 저장한 뒤 남은 단계를 건너뛴다(종료 코드 130). SIGTERM·SIGHUP도 같다. **두 번째 Ctrl+C**는 AIPerf를 즉시 종료한다.

## 그 밖의 명령

- `personaload gen <시나리오> --out DIR`: 단계별 AIPerf 데이터셋(`stage_NN/dataset.jsonl`)만 만든다.
- `personaload --version`, 그리고 하위 명령 앞에 `--log-level DEBUG|INFO|WARNING|ERROR`.

## 개발

```bash
pip install -e ".[dev]"
ruff format src tests && ruff check src tests
pytest -q --cov=personaload            # 통합 테스트는 실제 AIPerf와 mock 서버를 띄운다 (-m "not integration"으로 제외)
```

## 라이선스

personaload는 [Apache-2.0](LICENSE)이다.

personaload는 [AIPerf](https://github.com/ai-dynamo/aiperf)(NVIDIA, Apache-2.0) 0.12.0에 의존한다. AIPerf는 pip 의존성으로 설치되고, personaload는 `python -m aiperf profile`을 별도 프로세스로 실행해 그 입출력 파일만 다룬다. AIPerf 코드를 수정하거나 복제하거나 함께 재배포하지 않는다. 전이 의존성까지 포함한 라이선스 검토는 [docs/prior-art.md 부록 C](docs/prior-art.md)에 있다. 벤치마크 대상 모델과 토크나이저의 라이선스는 별도다.
