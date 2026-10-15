# personaload (B안): AIPerf 기반 캐릭터챗 워크로드 벤치마크

## 0. 작업 지시 (Claude Code용)

- 이 문서는 `SPEC_2.md`를 **대체**한다. M0 조사 결과([docs/prior-art.md](docs/prior-art.md))에 따라 부하 생성과 측정은 **AIPerf 0.12.0**에 맡긴다. personaload는 다음 네 가지만 구현한다.
  - 워크로드 생성
  - 단계 실행 오케스트레이션
  - 리포트
  - mock 서버
- 마일스톤은 **B1 → B5 순서**로 진행한다. 각 마일스톤이 끝나면 완료 기준을 직접 실행해 검증하고, 결과를 보고한 뒤 다음으로 넘어간다.
- 명세가 모호하거나 충돌하면 추측으로 진행하지 말고 질문한다.
- 새 의존성을 추가할 때는 이유를 커밋 메시지에 남긴다.
- 이 문서가 전제하는 AIPerf 동작의 근거는 [docs/aiperf-v0.12.0-facts.json](docs/aiperf-v0.12.0-facts.json)에 있다. 소스 라인과 실측 로그가 담겨 있다.
  - 본문의 `파일:라인` 표기는 모두 AIPerf `v0.12.0` 태그 기준이다. PyPI 설치본은 이 태그와 바이트 단위로 동일함을 확인했다.
  - 이 문서는 반박 검토(지적 44건 중 35건 확인)를 거쳐 수정됐다.
  - facts 파일과 이 문서가 충돌하면 **이 문서가 우선**한다. 예: fixed-schedule 요청 수(§8.2), drain 로그(§8.4).

## 1. 목적

OpenAI 호환 API(`/v1/chat/completions`, SSE 스트리밍)를 제공하는 서빙 엔진(vLLM, SGLang, llama.cpp server, MLX server 등)에 **AI 캐릭터 채팅 서비스의 트래픽 패턴**으로 부하를 걸어 다음을 산출한다.

1. 지연: TTFT, ITL, E2E (p50/p90/p95/p99)
2. 처리량: 출력 tokens/s, 완료 요청/s, 활성 세션 수
3. **원가: SLO를 만족하는 최대 처리량 단계 기준 `$ / 1M 출력 토큰`**

캐릭터챗 특성은 워크로드 데이터셋으로 표현한다. AIPerf는 이 데이터셋을 그대로 재생한다. 대상 특성은 다음과 같다.

- 멀티턴 히스토리 누적
- 공유 페르소나 system 프롬프트 + Zipf 인기 쏠림
- 턴마다 새로 샘플하는 think time
- 세션 단위 도착

## 2. 비목표

- 서빙 엔진 설치나 배포 자동화. B5에서 예시 스크립트만 둔다.
- 응답 품질 평가.
- GUI/대시보드. Prometheus + Grafana로 대체한다.
- OpenAI 호환 외 프로토콜.
- **AIPerf 수정, 포크, 번들 재배포.** personaload는 AIPerf를 별도 프로세스 CLI로만 호출한다. 이렇게 하면 라이선스 의무도 발생하지 않는다(prior-art.md 부록 C).

## 3. 언어 결정: Python

| 기준 | Python | Go |
|---|---|---|
| **측정 hot path 성능** | AIPerf(Python, 멀티프로세스 + ZMQ)가 수행한다 | **동일하다.** 래퍼 언어와 무관하게 AIPerf가 부하를 건다 |
| 래퍼 오프라인 처리량 | 데이터셋 생성과 리포트 집계뿐이다. 단계당 데이터셋 수~수백 MB, 요청 레코드 수천~10만 줄이다. `orjson`(AIPerf 의존성)으로 수 초 이내 | 3~5배 빠르겠지만, 단계 하나가 수 분이라 체감 차이가 없다 |
| AIPerf 입력 스키마 검증 | `aiperf.dataset.loader.models.MultiTurn`으로 생성 데이터를 테스트에서 직접 검증한다 | 스키마를 손으로 복제해야 하고, AIPerf 버전이 오르면 어긋날 위험이 있다 |
| mock 서버 | `aiohttp`(AIPerf 의존성)를 쓰므로 추가 설치가 없다 | 표준 `net/http`로 충분하다 |
| 설치·배포 | `pip install` 한 번으로 personaload와 AIPerf가 함께 설치된다 | Go 바이너리 + Python 환경(AIPerf), 두 벌이 필요하다 |
| 시드 재현성 | 표준 `random.Random` + 직접 구현한 역CDF 변환. 버전 간에도 안정적이다 | `math/rand/v2` PCG. 동등하다 |

**결론은 Python이다.** 부하와 측정의 성능은 어느 쪽이든 AIPerf가 결정하므로 성능은 차별점이 되지 않는다. 개발 용이성에서는 AIPerf 모델로 직접 검증할 수 있고 런타임이 하나라는 점에서 Python이 확실히 유리하다. 원래 스펙의 "단일 바이너리" 장점은 AIPerf가 Python이라 어차피 사라진다.

**디스크와 메모리 주의**: AIPerf는 multi_turn 데이터셋이면 프로파일링 전에 `inputs.json`을 메모리에서 만들어 기록한다.

- 턴마다 페르소나 전체를 포함하고 indent 2로 저장한다. 끄는 옵션은 없다(`dataset_manager.py:253-287, 500-502`).
- 크기는 대략 `dataset.jsonl × E[T]`다. 페르소나 500개처럼 큰 단계는 수백 MB~GB가 될 수 있다.
- personaload는 이 크기를 validate 추정치에 포함하고, 단계가 끝나면 파일을 삭제한다(§8.6).

## 4. 기술 제약

- Python `>=3.11,<3.14`(AIPerf 제약). 패키지명 `personaload`, src 레이아웃, `pyproject.toml`(setuptools).
- 런타임 의존성:
  - `aiperf==0.12.0`으로 **고정**한다.
  - 직접 import하는 패키지는 명시적으로 선언한다: `pydantic>=2.10,<3`, `ruamel.yaml`, `aiohttp`, `orjson`. 모두 AIPerf가 이미 설치하므로 **실제로 새로 설치되는 패키지는 없다**.
- 개발 의존성: `pytest`, `pytest-cov`, `ruff`.
- CLI: 표준 `argparse`. 로그: 표준 `logging`과 `--log-level`.
- AIPerf 호출 방식:
  - `subprocess.Popen([sys.executable, "-m", "aiperf", "profile", ...], start_new_session=True)`로 **별도 세션·프로세스 그룹**에서 띄운다.
  - 같은 venv의 AIPerf를 보장하고, 터미널 Ctrl+C가 AIPerf에 직접 가지 않게 하기 위해서다(§8.5).
- 실행 전 `importlib.metadata.version("aiperf") == "0.12.0"`을 확인하고, 다르면 중단한다.
- macOS(arm64), Linux(amd64, arm64)에서 동작해야 한다. Linux arm64에서는 AIPerf 의존성 `crick`을 빌드하려면 C 컴파일러가 필요하다.

## 5. 구조

```
시나리오 YAML ──▶ workload ──▶ stage_NN/dataset.jsonl
                                  │
                                  ▼
                 runner ──▶ python -m aiperf profile ... (단계당 1 프로세스)
                                  │
                                  ▼
                   stage_NN/aiperf/profile_export.jsonl (요청별 레코드)
                                  │
                                  ▼
                 report ──▶ stage_NN/stage.json ──▶ report.json / report.md

mockserver: 테스트·로컬 개발용 OpenAI 호환 서버 (prefix cache 흉내)
```

```
pyproject.toml
src/personaload/
  __main__.py
  cli.py                    # validate | gen | run | report | mockserver
  config.py                 # 시나리오 스키마(pydantic), 기본값, 검증, 기간 문자열 파싱
  workload/
    rng.py                  # SHA-256 파생 시드, 난수 스트림
    dists.py                # uniform / lognormal / exponential / constant / zipf, 클램프, 클램프 후 기대값
    text.py                 # ko/en 합성 문장 (시드 결정적)
    personas.py             # 합성 페르소나 / 파일 로드
    sessions.py             # 단계별 세션 생성, 세션 수 산정, 도착 시각
    dataset.py              # AIPerf multi_turn JSONL 직렬화
  runner/
    plan.py                 # 시나리오+단계 → AIPerf argv/env
    process.py              # 서브프로세스 실행, 2단계 워치독, SIGINT 전달
    stages.py               # 단계 순차 실행, 결과 디렉터리
  report/
    records.py              # profile_export.jsonl → 요청 레코드 (누락 필드 허용)
    aggregate.py            # 구간, 백분위, 처리량, cache hit%, 활성 세션, lag
    judge.py                # SLO 판정, 원가, 헤드라인
    render.py               # stage.json / report.json / report.md
    compare.py              # 여러 결과 디렉터리 비교 (stage.json 기반)
  mockserver/
    app.py                  # aiohttp 라우트, SSE
    cache.py                # 메시지 경계 prefix 해시
scenarios/                  # 예시 5종 (B5, RAG 예시 추가)
testdata/                   # 페르소나 샘플, AIPerf 0.12.0 실제 산출물 픽스처
scripts/                    # llama.cpp(macOS), vLLM(Linux) 기동 예시 (B5)
tests/
docs/
```

## 6. 시나리오 YAML

`SPEC_2` §5를 유지하되, 아래에 표시한 부분만 바꾼다.

```yaml
name: persona-shared-8k
seed: 42

target:
  base_url: http://localhost:8000/v1     # → --url (끝의 /v1 허용: aiohttp_transport.py:274)
  model: Qwen/Qwen3-8B                   # → --model
  api_key_env: OPENAI_API_KEY            # 선택. 값이 있으면 → --api-key
  timeout: 120s                          # → --request-timeout-seconds
  tokenizer: builtin                     # [신규] → --tokenizer
  max_tokens_field: max_completion_tokens  # [신규] max_tokens이면 --use-legacy-max-tokens
  extra_body: {}                         # → --extra-inputs '<JSON>' (엔진별 파라미터 그대로 전달)

personas:
  count: 20
  system_prompt_tokens: {min: 1000, max: 4000}   # dist 생략 = uniform
  source: synthetic                      # synthetic | file
  file: testdata/personas.jsonl          # source=file일 때. 각 줄 {"name": str, "system_prompt": str}

session:
  language: en                           # en | ko
  turns: {dist: uniform, min: 5, max: 30}
  user_input_tokens: {dist: lognormal, mean: 50, stddev: 30, min: 5, max: 300}
  max_output_tokens: {dist: uniform, min: 150, max: 500}
  think_time: {dist: exponential, mean: 10s, min: 1s, max: 60s}
  # retrieved_tokens: {min: 800, max: 2000}  # [신규] RAG: 턴마다 새 검색 문서를 user 입력 앞에 붙인다 (§7.3)
  persona_selection: zipf                # uniform | zipf
  zipf_s: 1.1
  # [제거] history_policy, history_last_n (§12-1)

load:
  mode: concurrency                      # concurrency | arrival_rate
  # drain_timeout: 120s                  # → --benchmark-grace-period. 기본값 = target.timeout (§12-13, D6)
  session_pool_factor: 3                 # [신규] 단계별 세션 수 여유 배수 (§7.5)
  stages:
    - {concurrency: 10,  duration: 3m, warmup: 30s}
    - {concurrency: 50,  duration: 3m, warmup: 30s}
    - {concurrency: 200, duration: 3m, warmup: 30s}
  # arrival_rate: stages: [{sessions_per_sec: 2, duration: 5m, warmup: 30s, max_concurrency: 500}]
  # 선택: 단계에 sessions: <정수>를 주면 §7.5 공식 대신 그 수만큼 세션을 만든다 (concurrency 모드)

slo:
  ttft_p95: 1s
  itl_p95: 100ms                         # 청크 간격 기준 (§9.3, 결정 D2)
  error_rate_max: 0.01                   # 분수 (0~1)

cost:
  gpu_hourly_usd: 1.09
  gpu_count: 1
  label: "RunPod L40S"

output:
  dir: results/
  prometheus_port: 9090                  # [변경] listen 문자열 → 포트. AIPerf --api-port. 생략하면 비활성
  prometheus_host: 127.0.0.1             # [신규] → --api-host
  record_requests: true                  # false면 단계 종료 후 요청 레코드 파일 삭제 (stage.json은 유지)
```

### 6.1 기본값

아래 표에 없는 필드는 필수다: `name`, `target.base_url`, `target.model`, `personas.count`와 `system_prompt_tokens`(synthetic일 때), `personas.file`(file일 때), `session`의 네 분포, `load.mode`, `load.stages`, `slo.*`, `cost.gpu_hourly_usd`.

| 필드 | 기본값 |
|---|---|
| `seed` | 0 |
| `target.api_key_env` | 없음 |
| `target.timeout` | 120s |
| `target.tokenizer` | `builtin` (tiktoken o200k_base) |
| `target.max_tokens_field` | `max_completion_tokens` |
| `target.extra_body` | `{}` |
| `personas.source` | `synthetic` |
| `session.language` | `en` |
| `session.persona_selection` | `uniform` |
| `session.zipf_s` | 1.0 |
| `session.retrieved_tokens` | 없음 (RAG 끔) |
| `load.drain_timeout` | `target.timeout`와 같음 (D6) |
| `load.session_pool_factor` | 3 |
| 단계 `warmup` | 0s |
| `cost.gpu_count` / `cost.label` | 1 / `""` |
| `output.dir` | `results/` |
| `output.prometheus_port` | 없음 (비활성) |
| `output.prometheus_host` | `127.0.0.1` |
| `output.record_requests` | true |
| 분포의 `dist` 생략 | `uniform` |

### 6.2 분포 정의

- **클램프**: 클립은 클램프다. `x ← min(max(x, min), max)`. min이나 max가 없으면 그쪽은 열려 있다.
- **기간 분포**: ms 단위 실수로 계산한다.
- **uniform**:
  - 기간이면 `min + u·(max−min)`
  - 정수 양이면 `min + floor(u·(max−min+1))`
- **연속 분포로 정한 정수 양**(턴 수, 토큰 수): 클램프한 뒤 `floor(x+0.5)`로 정수화한다.
- **lognormal**:
  - `σ² = ln(1+stddev²/mean²)`, `μ = ln(mean) − σ²/2`, `X = exp(μ+σZ)`
  - `Z = sqrt(−2·ln(1−u1))·cos(2π·u2)`. 표본마다 u1, u2를 이 순서로 새로 뽑고 사인 쪽 값은 버린다.
  - `stddev = 0`이면 X = mean이다. 이때도 u1, u2는 소비한다(난수 스트림 위치 유지).
- **정수 양의 결정적 값**: CountDist에서 `min`, `max`, constant의 `value`, stddev=0 lognormal의 `mean`은 정수여야 한다.
- **exponential**: `X = −mean·ln(1−u)`.
- **constant**: `value`. 값은 [min, max] 안이어야 한다.
- **zipf**(페르소나 선택):
  - `w_r = r^−s` (r=1..K), `cum`은 w의 누적합
  - `r = bisect_right(cum, u·cum[−1], 0, K−1) + 1`
  - 순위 r은 페르소나 인덱스 r−1이다. P0001이 가장 인기 있다.
- **클램프 후 기대값** `E[·]`: §7.5의 세션 수 산정과 B1 평균 테스트의 기대값으로 쓴다.
  - uniform 정수: `(min+max)/2`
  - constant: 값 그대로
  - exponential: `a + m(e^{−a/m} − e^{−b/m})` (a=min, b=max, m=mean)
  - lognormal: `a·Φ(α) + e^{μ+σ²/2}[Φ(β−σ) − Φ(α−σ)] + b(1−Φ(β))`. 여기서 `α=(ln a−μ)/σ`, `β=(ln b−μ)/σ`이고, Φ는 `statistics.NormalDist().cdf`다.
  - 한쪽 경계가 없으면 그 항을 뺀다.
- validate는 설정 평균과 클램프 후 평균을 함께 출력하고, 둘이 5% 넘게 다르면 경고한다.

### 6.3 검증 규칙 (`personaload validate`)

**오류**:

- 모든 분포: `min ≤ max`. exponential은 `mean > 0`, lognormal은 `mean > 0`이고 `stddev ≥ 0`. constant의 value는 [min, max] 안.
- 최솟값: `turns.min ≥ 1`, `user_input_tokens.min ≥ 1`, `max_output_tokens.min ≥ 1`, `system_prompt_tokens.min ≥ 1`, `retrieved_tokens.min ≥ 1`(있을 때). AIPerf는 `output_length=0`과 빈 텍스트를 거부한다.
- `think_time`: `max`는 필수이고 `min ≥ 0`. 단, `dist: constant`는 값이 고정이라 `max`가 없어도 된다.
- 페르소나: `personas.count ≥ 1`. source=file이면 system_prompt가 비어 있지 않은 줄이 1개 이상 있어야 한다.
- zipf: `persona_selection=zipf`이면 `zipf_s > 0`.
- 단계: `duration > 0`, `warmup ≥ 0`. `concurrency`와 `max_concurrency`는 정수 ≥ 1. `sessions_per_sec > 0`. `mode`에 맞는 필드만 허용한다.
- 세션 수: `session_pool_factor ≥ 1`. §7.5의 `L < 1s`이면(단계에 `sessions`가 없을 때) 오류.
- slo: `ttft_p95 > 0`, `itl_p95 > 0`, `0 ≤ error_rate_max ≤ 1`.
- cost: `gpu_hourly_usd ≥ 0`, `gpu_count`는 정수 ≥ 1.
- 기타: `target.timeout > 0`, `load.drain_timeout ≥ 0`.
- 기간 문자열은 `^\d+(\.\d+)?(ms|s|m|h)$` 형식만 허용한다.
- 예상 `dataset.jsonl`이 단계당 1 GiB를 넘으면 오류.

**경고**:

- `think_time.max > 60s`. AIPerf도 60000ms를 넘는 delay가 있으면 로드할 때 한 번 경고한다(`_delay_cap.py:19`). 거부하지는 않는다.
- `drain_timeout < target.timeout`. drain 뒤 취소되는 요청이 생길 수 있고, 그 요청은 레코드가 남지 않아 검열된다(§8.4).
- 설정 평균과 클램프 후 평균의 차이가 5%를 넘는 경우.

**출력**: 기본값을 적용한 시나리오, 분포별 설정 평균과 클램프 후 평균, 단계별 예상 세션 수 n, 예상 `dataset.jsonl` 바이트, 예상 AIPerf `inputs.json` 바이트, AIPerf 명령 미리보기.

## 7. 워크로드 생성

### 7.1 난수와 재현성

- 컴포넌트마다 자식 시드를 쓴다: `int.from_bytes(sha256(key.encode()).digest()[:8], "big")`로 `random.Random`을 만든다. 키는 정확히 다음 세 형식이다.
  - 페르소나 i(0부터): `f"{seed}:persona:{i}"`. 풀은 시나리오 단위라 단계가 없다.
  - 단계 k(1부터)의 세션 j(1부터): `f"{seed}:session:{k}:{j}"`
  - 단계 k의 도착 시각: `f"{seed}:arrival:{k}"`
  - 단계 k의 세션 j의 검색 문서(RAG): `f"{seed}:retrieval:{k}:{j}"`. 따로 두는 이유: RAG를 켜고 끈 두 실행이 페르소나·턴 수·질문·출력 길이·think time을 공유해야 비교가 공정하다. `retrieved_tokens`가 없으면 이 키는 쓰이지 않고 기존 데이터셋은 바이트 단위로 그대로다.
  - 컴포넌트를 추가하거나 순서를 바꿔도 다른 시퀀스가 변하지 않는다.
- 분포는 `rng.random()`만 써서 §6.2대로 변환한다.
- workload 코드는 **set 순회와 내장 `hash()`를 쓰지 않는다.** 둘 다 `PYTHONHASHSEED`에 따라 결과가 달라진다.
- 실수 기록 형식:
  - `delay`: `round(x, 1)`(ms)
  - 도착 시각: 초 단위 실수로 누적한 뒤, 기록할 때 `round(t×1000, 1)`(ms)
- source=file이면 페르소나 파일의 sha256을 `scenario.resolved.yaml`에 기록한다.
- **같은 seed와 같은 시나리오(같은 페르소나 파일)면 모든 단계의 `dataset.jsonl`이 바이트 단위로 동일하다.** 페르소나, 턴 수, 입력 텍스트, think time, 도착 시각이 모두 같다.

### 7.2 페르소나

- **합성**: 시드로 결정되는 이름, 성격, 말투, 세계관 문단을 반복·조합해 목표 토큰 수(`system_prompt_tokens` 분포)를 맞춘다.
  - 페르소나 고유 문자열 `[Persona P0042 · <name>]`로 시작한다. 페르소나 사이에 공통 prefix가 우연히 길어지지 않게 하기 위해서다.
- **토큰 근사**: 글자 수 ÷ 언어별 상수(en 4.0, ko 1.5). 측정과 리포트는 서버 `usage`를 쓴다.
- **파일**: JSONL의 각 줄은 `{"name", "system_prompt"}`다. 파일 앞에서부터 min(`count`, 줄 수)개를 쓴다.
- 페르소나 풀은 시나리오 단위다. 모든 단계가 같은 풀을 공유한다(실서비스처럼 캐시가 단계를 넘어 유지된다).

### 7.3 세션

단계 k의 세션 j는 다음과 같이 만든다.

- 페르소나: `persona_selection`에 따라 선택한다(§6.2 zipf).
- 턴 수: `T ~ turns`.
- 각 user 턴 t(0..T-1):
  - 사용자 입력 텍스트: `user_input_tokens` 분포와 언어에 맞춰 합성한다.
  - `output_length ~ max_output_tokens`
  - t ≥ 1이면 `delay ~ think_time`(ms). AIPerf는 turn 0의 delay를 무시한다.
  - `retrieved_tokens`가 있으면(RAG) 검색 문서 스트림에서 `retrieved_tokens` 길이의 문서를 만들어 입력 앞에 붙인다. ko: `[참고 문서]\n{문서}\n\n[질문]\n{입력}`, en: `[Retrieved documents]\n{문서}\n\n[Question]\n{입력}`.
    - 문서는 user 메시지 안에 있으므로 AIPerf가 다음 턴 히스토리에 그대로 다시 보낸다. 지난 검색 결과를 히스토리에서 빼는 앱보다 입력이 크게 잡힌다.

### 7.4 AIPerf multi_turn JSONL 형식

```json
{"session_id":"s01-000042","turns":[{"role":"system","text":"[Persona P0007 · Mira] ..."},{"text":"Hi, who are you?","output_length":312},{"text":"Tell me more.","delay":8423.1,"output_length":187}]}
```

반드시 지킬 규칙(위반하면 AIPerf가 조용히 다르게 동작한다):

| 규칙 | 이유 (근거) |
|---|---|
| system 턴에는 `role`과 `text`**만** 둔다. `delay`·`timestamp`·`output_length`·`extra` 금지 | 하나라도 있으면(`delay: 0` 포함) system_message로 hoist되지 않고 별도 요청으로 전송된다 (`multi_turn.py:285-329`) |
| user 턴은 `role`을 생략한다 | None이면 user로 전송된다 |
| `session_id`는 전역 고유. 형식 `s{stage:02d}-{index:06d}`. 단계당 세션이 100만 개 이상이면 자릿수를 늘린다 | 같은 id의 줄은 한 세션으로 병합된다 (`multi_turn.py:167-178`) |
| 턴별 최대 출력은 `output_length`(≥1)로 준다. `max_tokens` 키는 무시된다 | `models.py:47-164`. 전송 필드는 `max_completion_tokens`, 또는 legacy 옵션일 때 `max_tokens` |
| assistant 턴을 넣지 않는다 | 모든 턴이 요청으로 전송된다. 히스토리는 AIPerf가 실제 응답으로 채운다 |
| arrival_rate: 첫 user 턴에 `timestamp`(ms), 같은 값을 **줄 최상위 `timestamp`**에도 둔다 | 최상위 키가 없으면 AIPerf가 기동 직후 설정 해석 단계에서 실패한다 (`resolver.py:342-385`, 실측) |
| 키 순서: 줄은 `session_id`, [`timestamp`], `turns`. 턴은 `role`, `text`, [`timestamp`], [`delay`], [`output_length`] | 바이트 단위 재현성 |
| 직렬화는 `json.dumps(obj, ensure_ascii=False, separators=(",",":"))`**로만** 한다 | 바이트 단위 재현성 |

- 텍스트는 수정 없이 전송된다(재토큰화·절단 없음).
- system 프롬프트는 **매 턴 맨 앞에 재전송**된다(`worker.py:1031-1050`, `openai_chat.py:83-108`, 실측).
- AIPerf 레코드의 `turn_index`는 hoist된 system 턴을 제외하고 센다. 따라서 **`turn_index` = 우리 데이터셋의 user 턴 인덱스**다.

### 7.5 단계별 세션 수

- **concurrency**: 단계에 `sessions: <정수>`가 있으면 그 값을 쓴다. 없으면 다음 공식을 쓴다.
  - `L = (E[T]−1)·E[think]`(초, §6.2의 클램프 후 기대값). `L < 1s`이면 validate 오류다.
  - `n = ceil(session_pool_factor × N × (warmup+duration) / L) + N`
  - E2E를 0으로 보고 계산한 상한 근사다.
  - AIPerf는 데이터셋을 다 쓰면 처음부터 다시 재생한다(순차 샘플링, `dataset_samplers.py:32-86`). 그래서 리포트에서 같은 `conversation_id`가 2개 이상의 세션 인스턴스(`x_correlation_id`)로 나타나면 **"데이터셋 재사용 발생"** 경고를 낸다.
- **arrival_rate**: [0, warmup+duration) 구간에서 포아송 과정(λ=`sessions_per_sec`)으로 세션 시작 시각을 만든다.
  - 첫 세션은 0으로 고정한다. 지연 기준점을 정확히 잡기 위해서다(§9.2).
  - 생성된 세션 수가 곧 데이터셋 크기다(최소 1).
- 단계마다 서로 다른 세션 집합을 쓴다. 시드에 단계 번호가 들어간다.

## 8. 실행

### 8.1 단계당 AIPerf 프로세스 1개

AIPerf의 멀티 phase YAML을 쓰지 않고 단계마다 별도 프로세스를 띄운다. 이유는 다음과 같다.

- AIPerf warmup phase는 system 메시지 앞에 `"warmup\n"`을 붙인다(`worker.py:1341-1369`). 그러면 warmup이 **페르소나 prefix cache를 데우지 못한다**. 그래서 warmup은 AIPerf에 profiling phase 하나로 `warmup+duration`만큼 돌리고, personaload가 집계 단계에서 제외하는 방식으로 구현한다(§9.2).
- 멀티 phase에서는 샘플러를 공유해서 phase 경계마다 세션 1개가 버려진다. 또 profiling phase가 2개 이상이면 CLI 부하 플래그가 금지된다. fixed-schedule은 스윕도 할 수 없다.
- 프로세스가 하나면 SIGINT 전달, 워치독, 부분 결과 보존이 단순해진다.

단계는 순서대로 실행한다. 한 단계가 `failed`이면 남은 단계는 실행하지 않는다. 대상 서버나 설정에 문제가 있을 가능성이 크고, 그 뒤 단계의 결과는 비교할 수 없기 때문이다.

### 8.2 AIPerf 인자 매핑

공통 인자:

```
python -m aiperf profile
  --model <target.model> --url <target.base_url> --endpoint-type chat --streaming
  --input-file <stage>/dataset.jsonl --custom-dataset-type multi_turn
  --use-server-token-count --tokenizer <target.tokenizer>
  --record-processors <R>
  --benchmark-duration <warmup+duration 초> --benchmark-grace-period <drain_timeout 초>
  --request-timeout-seconds <target.timeout 초>
  --extra-inputs '<target.extra_body JSON>'          # 비어 있으면 생략
  --ui none --artifact-dir <stage>/aiperf
  [--api-key <env 값>] [--use-legacy-max-tokens]
  [--api-port <P> --api-host <H> --stats-interval 5]  # + 환경변수 AIPERF_UI_REALTIME_METRICS_ENABLED=true
```

모드별 추가 인자:

| 모드 | 추가 인자 |
|---|---|
| concurrency | `--concurrency <N>` |
| arrival_rate | `--fixed-schedule --fixed-schedule-start-offset 0 --concurrency <max_concurrency>` |

인자별 비고:

- `R = max(1, W // 4)`, `W = min(N, max(1, int(cpu×0.75) - 1), 32)`. AIPerf 기본 공식과 같은 값을 **명시적으로** 넘긴다(§8.3). arrival_rate에서는 N 대신 `max_concurrency`를 쓴다.
- 동시성 의미:
  - concurrency 모드의 `--concurrency`는 **세션 슬롯**이다. think time 동안에도 슬롯을 점유하고, 세션이 끝나면 약 0.6ms 뒤 새 세션이 시작된다(`concurrency.py:392-403`, 실측).
  - arrival_rate의 `--concurrency`는 세션 슬롯 상한이다. 초과한 세션은 **대기**한다(§12-2).
- `--fixed-schedule-start-offset 0`을 주면 auto_offset이 꺼져, 데이터셋 timestamp가 phase 시작 기준 절대 오프셋이 된다.
- fixed-schedule에서는 AIPerf가 요청 수 목표를 데이터셋의 총 턴 수로 덮어쓴다(`runner.py:139-149`, run2 실측). 그래서 `--request-count`는 넘기지 않는다.
- 두 모드의 argv 전체가 파싱되고 의도한 phase 설정으로 변환되는 것은 SPEC 검토 때 드라이런(설치된 aiperf 0.12.0의 CLI 파서, 변환기, resolver 호출)으로 확인했다.

### 8.3 AIPerf 알려진 문제 회피 (필수)

| 문제 | 대응 |
|---|---|
| `--record-processors`를 주지 않으면 레코드 프로세서가 데이터셋 준비 알림을 놓쳐서, 프로파일링 뒤 최대 300초 멈춘다. 실측 4회 중 3회 발생 | 항상 `--record-processors R`을 넘긴다(필수 서비스로 등록된다) |
| 멈춤·행에 대한 최후 방어. 기동 시간은 데이터셋 크기에 비례한다(`inputs.json` 생성, §3) | **2단계 워치독**. ① 기동 단계: `aiperf/logs/aiperf.log`에 `AIPerf System is PROFILING`이 600초 안에 나타나지 않으면 실패 ② 실행 단계: 그 시점부터 `warmup + duration + drain_timeout + 120s`를 넘기면 실패. 실패하면 프로세스 그룹에 SIGINT, 15초 뒤 SIGKILL을 보내고 단계를 실패로 처리한다 |
| `--use-server-token-count` + 파일 데이터셋이면 토크나이저 자동 대체가 없어 모델 이름으로 HF Hub 로드를 시도한다 | 항상 `--tokenizer`를 명시한다. 기본은 `builtin`. 최초 1회 o200k_base를 다운로드하고, 오프라인 환경은 `TIKTOKEN_CACHE_DIR`를 미리 채운다 |
| `--ui none`이면 실시간 메트릭이 발행되지 않는다(`aiperf_info`만 나옴) | Prometheus 활성 시 `AIPERF_UI_REALTIME_METRICS_ENABLED=true`를 설정한다 |
| AIPerf 인자·데이터셋 오류(예: 최상위 timestamp 누락)는 기동 직후 설정 해석 단계에서 실패한다 | 0이 아닌 종료 코드이거나 `profile_export.jsonl`이 없으면 단계 실패로 처리하고, `logs/aiperf.log` 끝부분을 오류 메시지에 포함한다 |
| phase가 정상 완료돼도 phase 전체에서 성공 0건·에러 1건 이상이면(`All N inference request(s) failed`) 또는 레코드가 0건이면(`No profile results to export`) exit 1로 끝난다(`system_controller.py:1113-1140`) | 위 규칙대로 단계 실패다. 대상이 죽었거나 전부 에러를 내는 서버에서 남은 단계를 돌리지 않기 위해서다(§8.1). 그래서 "전부 에러 → SLO FAIL"(§9.4)과 `no_data`(§9.2)는 phase의 일부인 [W, E_eff) 창 안에서만 생긴다 |

### 8.4 단계 종료(drain)

AIPerf의 duration 종료 시 동작은 SPEC_2 §6과 일치한다.

- 새 세션을 시작하지 않고, think time 대기 중인 다음 턴도 보내지 않는다.
- 진행 중 요청은 `drain_timeout`까지 기다린다.
- 그 뒤에 남은 요청은 취소된다. 취소된 요청은 **레코드가 남지 않는다**.

**drain_cancelled 파싱**: personaload는 `stage_NN/aiperf/logs/aiperf.log`에서 다음 NOTICE 줄을 찾는다(`--ui none`이면 stdout에는 나오지 않는다).

```
Phase profiling \(profiling\) complete \| completed=([\d,]+), cancelled=([\d,]+), errors=([\d,]+)
```

- 두 번째 그룹에서 쉼표를 빼고 `drain_cancelled`로 쓴다(최선 노력).
- 같은 줄에 `was_cancelled=True`가 있으면 drain이 아니라 사용자 취소(`partial` 단계)의 수로 표시한다.
- AIPerf 0.12.0은 Ctrl+C 뒤에 이 줄을 남기지 않고 `… sending complete | …` 줄만 남긴다. 이때는 `Sent cancellation requests for ([\d,]+) in-flight credits` INFO 줄의 수를 사용자 취소(`user_cancelled`)로 쓴다. 진행 중 요청이 0개였으면 이 줄도 없으므로 "미확인"(null)이다.
  - 사용자 취소도 레코드를 남기지 않는다(에러 레코드도 없다). 그래서 에러율에는 넣지 않고, 단계 경고에 수를 표시한다.
- `timed out, cancelling all credits` WARNING의 `cancelled=`와 `sessions: … cancelled=`는 쓰지 않는다. 앞의 값은 취소 전 스냅샷이다.
- 줄을 찾지 못하면 "drain 취소 미확인"으로 표시한다.
- 기본값 `drain_timeout = target.timeout`이면 E 이전에 시작한 요청은 grace 안에 성공하거나 타임아웃 에러 레코드로 끝난다. 그래서 drain 취소는 원칙적으로 0이다(D6).

### 8.5 SIGINT

- AIPerf는 별도 프로세스 그룹에서 실행되므로(§4), 터미널 Ctrl+C는 personaload만 받는다.
- **첫 Ctrl+C**: personaload가 `os.killpg(pgid, SIGINT)`를 **정확히 한 번** 보낸다. AIPerf는 두 번째 SIGINT를 강제 종료로 처리하기 때문이다(`system_controller.py:832-854`).
  - AIPerf가 부분 결과를 export한다(`was_cancelled: true`, 최대 약 10초).
  - personaload는 SIGINT 시각을 `t_stop`으로 기록한다.
  - 완료된 단계와 부분 단계(`partial` 표시)로 리포트를 쓰고, 남은 단계는 건너뛴다. 종료 코드는 130.
  - 첫 Ctrl+C만으로는 SIGKILL하지 않는다. export가 15초를 넘겨도 기다린다. SIGINT 15초 뒤 SIGKILL은 워치독 전용이다(§8.3).
- **두 번째 Ctrl+C**: `os.killpg(pgid, SIGKILL)`을 보내고, 가능한 범위의 리포트를 쓴 뒤 종료한다.
  - 두 번이 한 폴링 주기(0.2초) 안에 들어와 SIGINT 없이 SIGKILL하더라도 그 시각을 `t_stop`으로 기록한다.
  - SIGKILL 뒤에는 `profile_export.jsonl`의 마지막 줄이 잘릴 수 있다. 읽지 못한 줄은 건너뛰고 경고한다.
- **SIGTERM·SIGHUP**: Ctrl+C와 똑같이 처리한다(작업 러너의 kill, 터미널 닫힘). AIPerf는 별도 세션이라 이 신호를 받지 못하므로, 처리하지 않으면 고아가 되어 부하를 계속 건다. 단, 이미 무시 상태인 신호(`nohup`의 SIGHUP)는 그대로 둔다.
- 데이터셋 생성 중에 Ctrl+C가 오면 그 단계는 AIPerf를 띄우지 않고 건너뛴다(`stage.json` 없음).
- AIPerf가 스스로 종료한 **뒤에** 도착한 Ctrl+C는 그 단계를 중단한 것이 아니다. 단계 상태는 종료 결과대로 정하고, 남은 단계만 건너뛴다.

### 8.6 결과 디렉터리

```
results/<name>-<YYYYmmdd-HHMMSS>/
  scenario.resolved.yaml    # 기본값 적용 결과, 페르소나 파일 sha256(source=file). personas.file은 절대 경로로 기록한다
  stage_01/
    dataset.jsonl
    aiperf.argv.json        # 실행한 명령과 환경변수 (API 키는 마스킹)
    aiperf/                 # AIPerf 산출물 (profile_export.jsonl, profile_export_aiperf.json, logs/…)
    stage.json              # 단계 집계 결과 (§9.7)
  report.json
  report.md
```

- 단계가 끝나면 `aiperf/inputs.json`을 **항상 삭제**한다. `dataset.jsonl`에서 재생성할 수 있기 때문이다. SIGKILL로 남은 쓰기 중 파일 `aiperf/inputs.tmp`도 함께 삭제한다.
- `record_requests: false`면 `profile_export.jsonl`(과 `_raw`)도 삭제한다. `stage.json`은 남는다.

## 9. 측정 정의

리포트 수치는 AIPerf의 집계 JSON이 아니라 **요청별 레코드(`profile_export.jsonl`)에서 직접 계산**한다. warmup 구간과 처리량 창을 정확히 통제하기 위해서다. AIPerf 집계 JSON은 참고용으로 보존한다.

### 9.1 레코드 매핑

`SPEC_2` §7의 `RequestRecord` 필드를 AIPerf 레코드에서 다음과 같이 가져온다.

| 필드 | AIPerf 레코드 |
|---|---|
| Stage | 단계 디렉터리 |
| SessionID | `metadata.x_correlation_id`: 세션 **인스턴스**. `session_num`은 phase별 요청 카운터이므로 쓰지 않는다 |
| PersonaID | `metadata.conversation_id` → 우리 데이터셋의 페르소나 |
| Turn | `metadata.turn_index` (= user 턴 인덱스, §7.4) |
| StartTime | `metadata.request_start_ns` |
| TTFT | `metrics.time_to_first_token.value` (ms) |
| E2E | `metrics.request_latency.value` (ms) |
| ITLs | `metrics.inter_chunk_latency.value`: **데이터가 있는 SSE 청크**(content·reasoning·tool_call) 사이 간격 목록, ms. 데이터 청크가 2개 미만이면 키가 없다 |
| PromptTokens | `metrics.usage_prompt_tokens.value` |
| OutputTokens | `metrics.usage_completion_tokens.value` (추론 토큰 포함, 결정 D4) |
| CachedTokens | `metrics.usage_prompt_cache_read_tokens.value` |
| StatusCode / Error | `error.code`, `error.type`, `error.message`. `error` 키가 없으면 성공이고 상태는 200이다 |

- TTFT 정의는 AIPerf를 따른다. 요청 시작부터 첫 content 또는 reasoning 청크까지다.
- **누락 필드**: `credit_issued_ns`, `request_ack_ns`, `conversation_id`, `x_correlation_id`, `turn_index`, `error.code`는 없을 수 있다. 워커 내부 오류로 합성된 레코드이거나 코드 없는 예외(예: 빈 200 스트림)인 경우다.
  - 이런 레코드는 에러율 계산에만 넣는다.
  - 세션 분석, lag 계산, t0 계산에서는 뺀다.
  - `error.code`가 없으면 상태 코드는 null로 둔다.
- **시계**: `credit_issued_ns`는 TimingManager가 phase 시작 벽시계에 perf_counter 경과를 더한 값이다. `request_*_ns`는 워커의 벽시계 기준값이다. 둘 다 같은 호스트의 ns 벽시계 축에 있으므로 personaload는 **차이만** 쓴다. NTP 보정이 일어나면 차이가 왜곡될 수 있다(§9.6 경고).

### 9.2 집계 구간

- **t0**:
  - concurrency 모드: `credit_issued_ns`가 있는 레코드 중 최솟값.
  - arrival_rate 모드: turn 0 레코드에 대해 `min(credit_issued_ns − timestamp_ms×10⁶)`. 시작 지연은 0 이상이므로 phase 시작의 최선 추정이다.
- `W = t0 + warmup`, `E = W + duration`.
- `E_eff = min(E, t_stop)`. `t_stop`은 personaload가 SIGINT를 보냈거나 워치독이 발동한 시각(`time.time_ns()`)이며 `stage.json`에 기록한다.
- 레코드가 0건이면 단계는 `no_data`다. 레코드가 있어도 t0를 정할 레코드가 없으면(모두 `credit_issued_ns` 누락, arrival_rate에서 turn 0 레코드 없음) `no_data`다. 단, 중단된 단계는 이런 경우에도 `partial`이다(§8.5). `E_eff ≤ W`이면 모든 지표가 N/A다.

| 지표 | 포함 대상 |
|---|---|
| 지연 백분위, 에러율 | `request_start_ns ∈ [W, E_eff)`인 요청. 지연 백분위는 **성공** 요청만. drain 중 완료된 요청도 포함 |
| 처리량(req/s, out tok/s) | `request_end_ns ∈ [W, E_eff)`인 **성공** 요청의 합 ÷ `(E_eff − W)` |
| 활성 세션 | 아래 정의. [W, E_eff) 구간의 시간가중 평균·최소·최대 |

**활성 세션**: 세션 인스턴스 i(`x_correlation_id`)의 구간은 `[s_i, e_i)`이다.

- `s_i` = turn 0 레코드의 `credit_issued_ns`.
- `e_i`:
  - 그 대화의 데이터셋상 마지막 턴(`turn_index = T_conv−1`) 레코드가 있으면 그 `request_end_ns`.
  - 없으면 +∞. 단계 종료로 잘린 세션이다. AIPerf는 think time 동안 슬롯을 유지하고 phase가 끝날 때 해제한다(`callback_handler.py:620-638`).
- 구간을 [W, E_eff)로 클립한 계단 함수의 시간가중 평균을 활성 세션으로 한다.
- turn 0 레코드가 없는 인스턴스는 관측할 수 없으므로 제외하고, 그 수를 경고에 표시한다.
- 최소·최대는 10ms 미만의 구간을 무시한 계단 함수에서 구한다. 세션 교대는 약 0.6ms 이하의 공백이나 겹침으로 나타난다. 겹침은 새 세션의 `credit_issued_ns`가 이전 세션의 마지막 `request_end_ns`보다 앞서는 경우로, 두 값의 시계가 달라서 생긴다(§9.1).
- arrival_rate에도 같은 규칙을 적용한다.

### 9.3 지표

- **TTFT, E2E**: p50/p90/p95/p99.
- **ITL**: 대상 성공 요청의 `inter_chunk_latency` 값을 **모두 모은 분포**로 p50/p90/p95/p99를 낸다. 스펙이 정의한 "청크 간 간격"이다(결정 D2).
  - AIPerf `inter_token_latency`는 요청별 평균값이라 정의가 다르다. 참고로 JSON에만 싣는다.
- **백분위 계산**: 선형 보간(numpy `linear`와 동일)이고, 표준 라이브러리로 구현한다. 표본이 0개면 null(표시 N/A), 1개면 그 값이다.
- **에러율**: 내부적으로 0~1 분수이고, SLO 비교도 분수로 한다. 표시할 때만 ×100 한다.
  - 기본: 에러 ÷ (성공 + 에러). 분모가 0이면 null이다.
  - `drain_cancelled = c > 0`이면 `(에러+c) ÷ (성공+에러+c)`로 계산하고, 지연 백분위 옆에 "c건 검열(하한값)"을 표시한다.
- **cache hit%**:
  - 대상: `request_start_ns ∈ [W, E_eff)`인 성공 요청 중 `usage_prompt_tokens`가 있는 것.
  - 분자: Σ `usage_prompt_cache_read_tokens`. 값이 없는 요청은 0으로 친다.
  - 분모: Σ `usage_prompt_tokens`. AIPerf `overall_usage_prompt_cache_read_pct`와 같은 정의다.
  - 대상 중 cached 필드가 있는 요청이 0건이거나 분모가 0이면 N/A다.
  - "cached 보고 m/n건"을 함께 표시한다.
- **usage가 없는 요청**: `metrics.usage_completion_tokens`가 없는 성공 요청을 뜻한다.
  - 출력 토큰은 `inter_chunk_latency`가 있으면 `len(value)+1`, 없으면 1로 대체한다.
  - 청크에는 reasoning 청크가 포함된다. 청크 하나에 토큰이 여러 개 담기는 서버에서는 과소 추정된다.
  - 리포트에 "usage 없음: 청크 수로 대체 (n건)"을 표시한다. n은 대체가 실제로 쓰이는 처리량 대상, 즉 `request_end_ns ∈ [W, E_eff)`인 성공 요청 중 usage가 없는 수다.
  - 입력 토큰과 cache hit%는 `N/A`로 둔다(결정 D5).
- **활성 세션**: 평균·최소·최대(§9.2). 단계 행에 표시한다.

### 9.4 SLO 판정

세 조건은 `TTFT p95 ≤ slo.ttft_p95`, `ITL p95 ≤ slo.itl_p95`, `에러율 ≤ slo.error_rate_max`다.

- 값이 있는 조건 중 하나라도 위반하면 **FAIL**이다. 예를 들어 전부 에러라 TTFT·ITL이 null이어도, 에러율이 상한을 넘으면 FAIL이다.
- 위반이 없고 null인 조건이 하나라도 있으면 **N/A**다.
- 그 밖에는 **PASS**다.

### 9.5 원가와 헤드라인

- `$/1M out = gpu_hourly_usd × gpu_count ÷ (out_tok_per_sec × 3600) × 1e6`. out tok/s가 0이거나 null이면 null이다.
- **헤드라인**:
  - 후보는 SLO PASS이면서 `partial`·실패가 아닌 단계다.
  - 후보 중 out tok/s가 가장 높은 단계의 `$/1M out`을 고른다. 동률이면 단계 번호가 작은 단계다.
  - 후보가 없으면 "SLO를 통과한 단계 없음"으로 명시한다.
- **건당 원가**(FinOps 단위): 턴 하나 = 요청 하나다.
  - `$/1K turns = gpu_hourly_usd × gpu_count ÷ (req_per_s × 3600) × 1000`. req/s가 0이거나 null이면 null이다.
  - `$/session = $/1K turns ÷ 1000 × E[turns]`. E[turns]는 시나리오 `session.turns`의 클램프 후 기대값(§6.2)이다.
  - 헤드라인 단계의 두 값도 헤드라인에 함께 싣는다. 표시는 `$/1K turns` 소수 4자리, `$/session` 소수 6자리다.

### 9.6 클라이언트 병목 감지

- **think lag**: 같은 세션 인스턴스의 턴 t−1, t 레코드가 모두 있고, t−1이 **성공**이며, `request_start_ns(t) ∈ [W, E_eff)`인 쌍만 계산한다.
  - `lag = request_start_ns(t) − request_end_ns(t−1) − delay_t×10⁶`
  - `delay_t`는 데이터셋 user 턴 목록의 `turn_index` 번째 값이다.
  - `request_start_ns`를 쓰므로 TimingManager 스케줄링, ZMQ 전달, 워커 대기가 모두 포함된다. 클라이언트 측 지연 전체를 잡기 위해서다.
  - 에러 턴 뒤의 쌍은 제외한다. HTTP 에러 레코드는 `request_end_ns = request_start_ns`라서 에러 응답 시간이 lag로 잡히기 때문이다. 제외한 수를 함께 표시한다.
  - **p99가 100ms를 넘으면 경고한다.** lag < −5ms인 쌍이 있으면 "시계 보정 의심"을 경고한다.
- **CPU 경고**: `stage_NN/aiperf/logs/aiperf.log`에 `AIPerf results may be inaccurate`(워커 CPU 85% 초과)가 있으면 경고하고 횟수를 표시한다.
- **arrival_rate 전용**: 대상은 예정 시각 `timestamp ∈ [warmup, warmup+duration)`(ms)인 세션이다.
  - 시작 지연 `request_start_ns(turn 0) − (t0 + timestamp×10⁶)`의 p50/p95/p99를 표시한다.
  - **대기 세션**: 대상 중 시작 지연이 100ms를 넘은 세션의 수.
  - **미시작/미기록**: 대상 중 레코드가 하나도 없는 세션의 수. drain 뒤 취소된 turn 0도 포함한다(§12-2).
  - `sess/s` 열은 `λ / 실측(대상 세션 수 ÷ duration) (max_concurrency)` 형식으로 표시한다.

### 9.7 stage.json과 리포트

- **`stage.json`**: 단계마다 다음을 저장한다.
  - `schema_version`, 단계 설정, 상태(`ok` / `partial` / `failed` / `no_data`)
  - t0, W, E_eff, `t_stop`
  - 아래 표의 모든 열, 활성 세션 평균·최소·최대
  - 헤드라인 계산 입력값(out tok/s, SLO 판정, $/1M out, $/1K turns, $/session)
  - `drain_cancelled`, 경고 목록
  - `dataset.jsonl`의 sha256
- **`report.json`**(기계용)과 **`report.md`**(사람용): 단계별 표 + 전체 요약(헤드라인) + 경고 목록.

| stage | conc | active sess | req/s | out tok/s | TTFT p50/p95/p99 | ITL p50/p95 | E2E p95 | err% | cache hit% | SLO | $/1M out | $/1K turns | $/session |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

- arrival_rate 모드에서는 `conc` 대신 `sess/s` 열(§9.6 형식)을 쓰고, `start lag p50/p95/p99`·`queued`·`not started` 열을 추가한다.
- `stage` 열은 `ok`가 아닌 단계에 상태를 붙인다(예: `02 (partial)`). `SLO` 열은 FAIL이면 위반한 조건을 붙인다(예: `FAIL: ttft_p95`).
- 경고 목록: 데이터셋 재사용(§7.5), turn 0 없는 인스턴스 수(§9.2), drain 취소 수 또는 미확인(§8.4), 사용자 취소 수(§8.4), usage 없음(§9.3), think lag p99와 제외한 쌍 수·시계 보정 의심(§9.6), CPU 경고 횟수(§9.6), 실패 단계의 원인 첫 줄.
- **`personaload report <dir1> <dir2> ...`**: 각 디렉터리의 `stage.json`만 읽어 비교 표를 만든다(재집계·재실행 없음). 디렉터리별 헤드라인과 단계 행을 출력한다.
  - 예: naive vs vLLM vs +prefix cache vs +FP8.
  - 같은 단계 번호의 `dataset.jsonl` sha256이 다르면 표 위에 "단계 k의 워크로드가 다름" 경고를 낸다.

### 9.8 코딩 에이전트 인계 문서 (`personaload report --handoff FILE`)

- `personaload report <dir>... --handoff FILE`은 비교 표를 출력하고, 다른 세션의 코딩 에이전트가 이 문서만 보고 작업할 수 있는 md를 FILE에 쓴다. `stage.json`과 `scenario.resolved.yaml`만 읽는다(`scenario.resolved.yaml`이 없으면 기준값은 N/A).
- 할 일은 **마지막 디렉터리** 결과 기준이다. 앞의 디렉터리는 결과 표와 비교(첫 디렉터리 대비 헤드라인 $/1M out 증감)에만 쓴다. 첫 디렉터리 원가가 $0이면(로컬 장비) 헤드라인 out tok/s 증감으로 비교한다.
- SLO 기준·대상 모델은 디렉터리마다 자기 `scenario.resolved.yaml`에서 읽는다.
- 구성:
  - 머리말: personaload가 무엇을 재는지, SLO의 뜻. 프롬프트는 personaload가 만든 것이므로 할 일은 **서버·엔진·모델 설정**을 가리킨다는 점.
  - 측정 대상: 모델, base_url, SLO 기준, 원가 기준.
  - 결과: 헤드라인, 비교, 단계 표. 워크로드(sha256)가 다르면 "이 비교는 공정하지 않다" 경고.
  - 할 일, 측정 품질 경고, 다시 재는 법(절대 경로 명령), 원자료 경로.
- 할 일 규칙. 아래 순서로 나열하고, 같은 규칙에 걸린 단계들의 근거는 한 항목에 모은다.

| 조건 | 항목 |
|---|---|
| 단계 `failed` | 단계 실패 (오류 첫 줄) |
| 단계가 `ok`가 아니거나 SLO 판정이 PASS·FAIL이 아님(N/A) | SLO를 판정할 수 없다 (측정 구간 요청 수, 기록 수) |
| SLO 위반 `ttft_p95` | 첫 토큰이 늦다 |
| SLO 위반 `itl_p95` | 생성이 느리다 |
| SLO 위반 `error_rate` | 에러율 초과 (코드별 건수) |
| cached 대상 요청이 있는데 보고 0건 | 서버가 cached 토큰을 보고하지 않는다 |
| cache hit < 50% | prefix cache가 잘 안 맞는다 |
| `usage_missing` > 0 | 서버가 usage를 주지 않는다 |

  - 항목마다 근거(단계, 수치, 기준, TTFT·ITL은 성공 요청 수), 원인 후보, 할 일, 확인(완료 조건)을 쓴다.
  - SLO 위반 항목의 완료 조건은 근거 단계가 **SLO 전체(TTFT·ITL·에러율)** PASS이고 헤드라인 $/1M out이 나빠지지 않는 것이다. 지표 하나만 좋아진 것은 완료가 아니다.
  - 마지막 결과가 2단계 이상이고 할 일이 있으면, 높은 단계의 FAIL은 포화점일 수 있으니 목표 부하를 모르면 사용자에게 물으라는 한 줄을 붙인다.
  - 할 일이 없다는 문장("모든 단계가 SLO를 통과")은 모든 단계가 ok이고 PASS일 때만 나온다.
- "측정 품질 경고(서비스 코드 수정 대상 아님)"에는 §9.7의 단계 경고와 함께 다음을 싣는다.
  - 측정 구간의 성공 요청이 20건 미만인 단계: p95가 상위 1~2건으로 정해져 판정이 우연에 좌우된다.
  - 가장 높은 단계까지 PASS: 포화점에 닿지 않아 헤드라인이 스윕 상한에서 잘린 값이다.
- 다시 재는 명령은 personaload를 실행한 인터프리터의 절대 경로(`<python> -m personaload`)와 shell 인용한 절대 경로로 쓴다. 다른 세션의 PATH에 personaload가 없어도, 경로에 공백이 있어도 그대로 실행된다. `scenario.resolved.yaml`이 없으면 원래 시나리오로 실행하라는 주석을 대신 쓴다.

## 10. Prometheus (실행 중 노출)

- `output.prometheus_port`가 설정되면 AIPerf `--api-port`로 `http://<host>:<port>/metrics`를 노출한다.
- 메트릭 이름은 **AIPerf 규칙**을 따른다. `SPEC_2` §9의 `personaload_*` 이름은 폐기한다. 주요 예:
  - `aiperf_time_to_first_token_ms`(summary, quantile 라벨)
  - `aiperf_inter_token_latency_ms`, `aiperf_request_latency_ms`
  - `aiperf_output_token_throughput_avg_tps`, `aiperf_request_throughput_avg_requests_per_sec`
  - `aiperf_request_error_rate_avg_percent`, `aiperf_overall_usage_prompt_cache_read_pct_avg_percent`
- 제약:
  - 단계마다 프로세스가 바뀌므로 단계 사이에 엔드포인트가 잠시 내려간다. `benchmark_id` 라벨로 단계를 구분한다.
  - 실시간 값은 personaload warmup 구간을 **포함**한다(§8.1).
  - `aiperf_request_error_rate_avg_percent`는 에러가 0이면 노출되지 않는다. 대시보드에서는 no data를 0으로 처리한다.
  - 실시간 `aiperf_inter_token_latency_ms`는 요청별 평균 ITL이라 리포트 ITL(D2, 청크 간격 풀링)과 정의가 다르다. 청크 간격은 실시간으로 노출되지 않는다.
  - 활성 세션 수는 실시간 메트릭이 아니다. 리포트에서 계산한다.
  - 포트가 이미 쓰이고 있으면 AIPerf는 API 서비스만 죽고 나머지는 기동 단계에서 멈춘다(로그에 포트 언급 없음). 그래서 personaload는 첫 단계 전에 `prometheus_host:prometheus_port`를 열어 보고, 실패하면 오류로 멈춘다.
  - 켜면 단계가 끝날 때 AIPerf가 API 프로세스를 SIGKILL로 정리하느라(`still alive after bus shutdown grace`) 단계마다 몇 초 더 걸린다.
- `deploy/grafana-dashboard.json`은 선택이다.

## 11. Mock 서버

```
personaload mockserver [--listen 127.0.0.1:8000] [--ttft 200ms] [--tps 40] [--jitter 0] \
  [--error-rate 0] [--cache-ttft-factor 1.0] [--seed 0] [--no-usage]
```

AIPerf에 딸린 mock 서버는 쓸 수 없어서 직접 만든다. 이유는 다음과 같다.

- cached_tokens가 해시로 만든 가짜 30~60%이고, 캐시 적중에 따른 TTFT 변화가 없다.
- prompt_tokens가 **user 메시지만** 센다(system과 히스토리 제외).
- PyPI 배포본에 포함되어 있지 않다.

**기본값**:

| 옵션 | 기본값 | 비고 |
|---|---|---|
| `--listen` | `127.0.0.1:8000` | `host:port`. 모든 인터페이스는 `0.0.0.0:8000` |
| `--ttft` | 200ms | |
| `--tps` | 40 | |
| `--jitter` | 0 | |
| `--error-rate` | 0 | |
| `--cache-ttft-factor` | **1.0** | 캐시 적중이 TTFT에 영향을 주지 않는다. SPEC_2와 같이 단축은 opt-in이다 |
| `--seed` | 0 | |

**인자 검증**: `0 ≤ jitter < 1`, `tps > 0`, `ttft ≥ 0`, `cache_ttft_factor ≥ 0`, `0 ≤ error_rate ≤ 1`. error_rate가 1을 넘으면 "비율(0~1)이다. AIPerf mock의 백분율과 다르다"는 오류로 거부한다.

**동작**:

- **라우트**: aiohttp. `POST /v1/chat/completions`(스트리밍/비스트리밍), `GET /v1/models`, `GET /health`.
- **토큰 수**: 메시지별 `ceil(len(content)/4)`의 합이다. **모든 role**을 센다.
  - 출력 토큰 하나는 고정 4글자 ASCII 조각이다. 그래서 히스토리로 돌아온 응답도 같은 규칙으로 다시 세어진다.
- **결정성**:
  - `req_hash = sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",",":")).encode())`. 내장 `hash()`는 쓰지 않는다.
  - 출력 길이와 에러 여부는 `Random(int.from_bytes(sha256(f"{seed}:{req_hash}:len" 또는 ":err").digest()[:8], "big"))`에서 뽑는다.
  - 지터는 전역 `Random(seed)`에서 뽑으며, 재현 대상이 아니다.
- **출력 길이**: 상한은 `max_completion_tokens`, 없으면 `max_tokens`, 둘 다 없으면 256.
  - `ignore_eos: true`면 정확히 상한만큼 낸다.
  - 아니면 [ceil(상한/2), 상한]에서 균등하게 뽑는다.
- **타이밍**:
  - TTFT = `ttft × U(1−jitter, 1+jitter)`
  - 토큰 간격 = `(1/tps) × U(1−jitter, 1+jitter)`
  - 송출은 절대 기한 방식이다: `deadline_k = t_first + Σ_{i≤k} interval_i`, `sleep(max(0, deadline_k − now))`.
  - SSE 청크 하나에 토큰 하나를 담는다.
- **prefix cache 흉내**:
  - 요청의 메시지 경계마다 prefix 해시(sha256)를 만든다: system, system+m1, …
  - 이전에 본 가장 긴 prefix의 토큰 수를 `usage.prompt_tokens_details.cached_tokens`로 돌려준다.
  - cached > 0이면 TTFT에 `cache_ttft_factor`를 곱한다.
  - **성공한** 요청의 prefix만 응답 후 등록한다. 프로세스 전역이고 퇴출(eviction)은 없다. 테스트와 로컬 용도라 메모리 증가를 허용한다.
- **에러**: `error_rate` 확률로 스트림 시작 전에 HTTP 500 `{"error": {...}}`를 반환한다.
- **usage**:
  - `stream_options.include_usage`가 참이면 마지막에 `choices: []` + `usage` 청크를 보낸다.
  - `--no-usage`면 usage를 생략한다. usage 누락 경로를 테스트하기 위한 옵션이다.

## 12. SPEC_2 대비 변경 사항

| # | SPEC_2 | B안 | 이유 |
|---|---|---|---|
| 1 | `history_policy: full \| last_n` | **full만 지원**. 필드 제거 | AIPerf multi_turn은 전체 히스토리만 누적한다(`DELTAS_WITHOUT_RESPONSES`) |
| 2 | arrival_rate에서 `max_concurrency` 초과 세션은 **드롭 + 카운트** | 초과 세션은 **대기**한다. 대기 세션 수와 시작 지연 분포를 표시하고, 단계 끝까지 시작 못 한 세션은 미시작 수로 센다(§9.6) | AIPerf fixed-schedule은 세션 슬롯을 대기로 처리하고 드롭하지 않는다(실측). 결정 D3 |
| 3 | 실패한 턴의 처리 미정 | **AIPerf 동작을 따른다.** 세션은 계속되고, HTTP 에러 턴은 assistant 응답이 없어 다음 요청에 user 메시지가 연속된다. 스트림 중간 예외는 에러 레코드로 집계되지만 부분 응답이 다음 턴 히스토리에 들어갈 수 있다(`worker.py:1147-1159`, 미검증) | 실측(run4). 에러율은 턴 단위로 센다 |
| 4 | reasoning_content의 히스토리 포함 여부 미정 | **AIPerf 동작**: content만 히스토리에 넣고, content가 비었을 때만 reasoning을 넣는다 | `base_endpoint.py:169-176` |
| 5 | warmup: 단계 내 구간 제외 | 동일하다. 단, AIPerf warmup phase는 쓰지 않고 집계 단계에서 제외한다 | §8.1 |
| 6 | 자체 Prometheus 메트릭 `personaload_*` | AIPerf 메트릭 이름(§10) | AIPerf 내장 |
| 7 | Go, cobra, HdrHistogram, 단일 바이너리, CGO 금지, go vet/staticcheck | Python 패키지, ruff | §3 |
| 8 | 표준 net/http로 SSE 직접 구현, 커넥션 풀을 동시성보다 크게 | AIPerf가 수행한다(워커당 HTTP 커넥션 한도 2500) | 측정 엔진 위임 |
| 9 | 시간 측정은 단조 시계 | 지연 값은 AIPerf가 측정한다. personaload는 같은 호스트의 ns 벽시계 축 타임스탬프 **차이**로 구간과 lag만 판정한다(§9.1) | AIPerf 레코드 형식 |
| 10 | 클라이언트 병목 = 러너 goroutine 지연 | think lag(§9.6) + AIPerf 워커 CPU 경고 | 러너가 AIPerf 내부에 있다 |
| 11 | drain 뒤 취소 요청의 처리 미정 | 레코드가 남지 않는다. 로그에서 `drain_cancelled`를 파싱해 에러율에 반영하고 검열을 표시한다(§8.4, §9.3) | AIPerf 동작 |
| 12 | `prometheus_listen: ":9090"` | `prometheus_port` + `prometheus_host` | AIPerf `--api-port`/`--api-host` |
| 13 | `drain_timeout` 기본 60s | 기본값 = `target.timeout`(기본 120s) | 60s이면 느린 요청이 레코드 없이 사라져 SLO가 검열된 데이터로 판정된다. 결정 D6 |
| 14 | `personaload report`는 결과 디렉터리를 비교 | `stage.json` 기반으로 비교한다. `record_requests: false`로 요청 레코드를 지워도 비교할 수 있다 | §9.7 |
| 15 | 클라이언트 단위 테스트(정상, 에러 코드, 스트림 중단, usage 없음) | 정상·에러·usage 없음은 mock과 리포트 테스트로 검증한다. 스트림 중단 처리는 AIPerf 담당이라 테스트하지 않는다(mock에 끊기 옵션 없음) | SSE 클라이언트가 AIPerf에 있다 |

## 13. 결정 필요 사항

아래 권장안을 기본값으로 가정하고 작성했다. 다르게 원하면 알려준다.

| # | 항목 | 권장안 (기본값) | 대안 |
|---|---|---|---|
| D1 | `ignore_eos` 강제 여부 | **강제하지 않는다.** `max_output_tokens`는 상한으로만 쓰고, 필요하면 `extra_body: {ignore_eos: true}`로 켠다. 모르는 파라미터를 거부하는 서버가 있고, 캐릭터챗에서는 자연스러운 EOS가 현실적이다 | 엔진 간 공정 비교를 위해 기본 강제. 이 경우 출력 길이가 모델과 무관하게 같아진다 |
| D2 | ITL SLO 정의 | **청크 간격 풀링 분포**(SPEC_2 정의) | AIPerf 요청별 평균 ITL (E2E−TTFT)/(OSL−1) |
| D3 | arrival_rate 초과 세션 | **대기**(AIPerf 동작). 대기·미시작 수를 표시 | 드롭이 꼭 필요하면 AIPerf 우회 구현이 필요하다(범위 밖) |
| D4 | 원가 계산의 출력 토큰 | **usage.completion_tokens**(추론 토큰 포함, GPU가 실제로 생성한 양) | 추론 토큰 제외 |
| D5 | usage가 없을 때 입력 토큰 | **N/A**(추정하지 않음) | 글자 수 근사 |
| D6 | `drain_timeout` 기본값 | **`target.timeout`과 같게**(검열 없음. 단계가 최대 timeout만큼 길어질 수 있다) | SPEC_2대로 60s. 이 경우 느린 요청은 drain_cancelled로만 반영된다 |

## 14. 마일스톤

### B1. 설정 + 워크로드 생성기

- 범위:
  - `config.py`(스키마, 기본값, 검증), `workload/*`
  - `personaload validate`
  - `personaload gen <scenario> --out DIR`: 데이터셋만 생성한다.
- **완료 기준**:
  - `PYTHONHASHSEED`가 다른 별도 프로세스 2개에서 같은 seed로 `personaload gen`을 실행하면, 모든 `dataset.jsonl`이 바이트 단위로 같다. seed를 바꾸면 달라진다.
  - 분포 샘플의 평균이 §6.2의 클램프 후 기대값에 맞고 범위를 지킨다. uniform, lognormal, exponential, constant, zipf 순위 빈도를 확인한다.
  - 생성된 모든 줄이 `aiperf.dataset.loader.models.MultiTurn`으로 검증된다.
  - AIPerf 로더로 읽었을 때 모든 세션의 system 턴이 hoist된다(`system_message` 설정, 턴 수 = user 턴 수).
  - arrival_rate 데이터셋은 모든 줄에 최상위 `timestamp`가 있고, 그 값이 첫 user 턴의 timestamp와 같다.
  - session_id가 고유하고, 페르소나가 고유 문자열로 시작한다.
  - §6.3의 오류·경고 규칙마다 테스트가 있다.

### B2. Mock 서버

- 범위: `mockserver/*`, `personaload mockserver`.
- **완료 기준**:
  - 스트리밍과 비스트리밍 응답이 OpenAI 형식이다.
  - include_usage에 따라 usage 청크가 붙거나 빠지고, `--no-usage`가 동작한다.
  - 같은 system 프롬프트나 히스토리로 재방문하면 `cached_tokens > 0`이다. `cache_ttft_factor < 1`이면 TTFT가 그만큼 줄어든다.
  - `error_rate`, `ignore_eos`, 상한 준수, 인자 검증을 테스트로 확인한다.
  - 같은 seed와 같은 요청이면 출력 길이와 에러 여부가 프로세스 재시작 후에도 같다.
  - 실제 `aiperf profile`을 mock 대상으로 짧게 돌려 정상 종료한다.

### B3. 실행기

- 범위: `runner/*`, `personaload run`. 단계 순차 실행, 2단계 워치독, SIGINT, 결과 디렉터리.
- **완료 기준**:
  - mock 대상 `concurrency: 50, duration: 30s` 실행에서, 레코드로 계산한 활성 세션(§9.2)의 [W, E_eff) 시간가중 평균이 **50±2**이고, 그 구간 시간의 99% 이상에서 48~52다.
  - arrival_rate 단계가 실행되고 시작 지연, 대기, 미시작 수가 계산된다.
  - 터미널에서 실행 중 Ctrl+C를 **한 번** 누르면 AIPerf가 강제 종료되지 않고 부분 결과(`partial`)가 저장된다.
  - 워치독이 멈춘 프로세스를 정리한다(가짜 프로세스로 기동·실행 단계 모두 테스트).
  - 단계 종료 후 `inputs.json`이 삭제된다.
  - 3회 연속 실행에서 멈춤이 없다.

### B4. 집계 + 리포트

- 범위: `report/*`, stage.json, report.json/md, SLO, 원가, 헤드라인, 경고, `personaload report` 비교.
- **완료 기준**:
  - 기본 mock(`--cache-ttft-factor 1.0`) 실행에서 설정값(ttft 200ms, tps 40 → 토큰 간격 25ms)이 리포트 TTFT p50과 ITL p50에 **±10% 이내**로 반영된다.
  - `--cache-ttft-factor 0.3` 실행에서 cache hit% > 0이 표시되고, 기본 실행보다 TTFT p50이 감소한 것이 `personaload report` 비교 표에 나타난다.
  - mock `--error-rate 0.1`로 구간 내 요청 300건 이상을 실행한다(짧은 think·출력 시나리오).
    - 리포트 err%가 구간 내 에러 레코드 비율과 같고 5~15%다.
    - 그 단계는 SLO FAIL이다.
    - 이 실행의 실제 `profile_export.jsonl`을 에러 픽스처로 `testdata/`에 둔다.
  - `--no-usage` mock에서 청크 수 대체 표시가 나오고 cache hit%가 N/A다.
  - SLO FAIL 단계만 있을 때 헤드라인에 "SLO를 통과한 단계 없음"이 나온다.
  - `record_requests: false`로 만든 결과 디렉터리도 `personaload report`로 비교된다.
  - §9.2~9.5의 퇴화 사례(빈 구간, 레코드 0건, 전부 에러, out tok/s 0, 누락 필드 레코드)를 테스트로 확인한다.

### B5. Prometheus + 예시 시나리오 + 실서버 가이드

- 범위:
  - `scenarios/`: `smoke.yaml`(1분), `persona-shared.yaml`(페르소나 5개), `persona-diverse.yaml`(페르소나 500개), `long-context.yaml`(30턴 이상), `rag-assistant.yaml`(RAG 상담, 추가)
  - `scripts/`: llama.cpp server(macOS), vLLM(Linux GPU) 기동 예시
  - README
  - `deploy/grafana-dashboard.json`(선택)
- **완료 기준**:
  - 실행 중 `curl :9090/metrics`에서 `aiperf_time_to_first_token_ms`를 확인한다.
  - README만 보고 맥북에서 llama.cpp 서버 + `smoke.yaml` 실행이 재현된다.

## 15. 품질 기준

- `ruff check` 경고 0.
- `pytest` 전부 통과. `workload`, `report`, `mockserver` 커버리지 **80% 이상**.
- AIPerf 레코드 스키마는 AIPerf 버전에 결합된다. 고정 버전(0.12.0)의 실제 산출물로 만든 테스트 픽스처를 `testdata/`에 둔다.
- README 내용:
  - 설치(`pip install -e .`)
  - 빠른 시작: mock 서버로 1분 안에 결과 보기
  - 시나리오 필드와 기본값 설명
  - 측정 정의(§9)
  - 원가 계산식
  - AIPerf 의존과 라이선스 고지
- 라이선스: Apache-2.0.
