# personaload

**English** | [한국어](README.ko.md)

personaload load-tests OpenAI-compatible LLM servers (vLLM, SGLang, llama.cpp server, ...) with **chatbot-shaped traffic**: AI character chat and RAG assistants. It reports latency, throughput, prefix-cache hit rate and **cost at your latency SLO**: `$/1M output tokens`, `$/1K turns` and `$/session` at the fastest stage that still passes the SLO.

Why another benchmark? Real chat traffic is not a stream of independent prompts. Many sessions share the same system prompt (a persona, or an assistant's instructions), history grows every turn, users pause between turns, and RAG apps put a new retrieved document in every turn. These patterns decide how much prefix caching saves, and so what a conversation actually costs. personaload builds that workload and turns the result into a unit cost you can put in a budget.

- **Workload:**
  - multi-turn sessions with real response history
  - a shared persona/system-prompt pool with Zipf popularity
  - per-turn think time
  - session-level arrivals (fixed concurrency or Poisson)
  - optional per-turn retrieved documents (RAG)
- **Measurement:** [AIPerf](https://github.com/ai-dynamo/aiperf) 0.12.0 generates the load and records every request. personaload generates the datasets, runs the stages, computes the report from per-request records, and ships a mock server for testing.
- **Example result:** on an Apple M5 Pro with vLLM Metal, enabling prefix caching cut `$/1M out` at the same SLO by 47–73% (Qwen3 0.6B–4B, 5 shared personas). With 500 distinct personas it still cut it by 38–53%. See [docs/cache-savings-m5.md](docs/cache-savings-m5.md) (Korean).
- **Language note:** the generated report (`report.md`), CLI messages and the design spec ([SPEC_B.md](SPEC_B.md)) are currently in Korean. Numbers and table headers are the same in any language.

## Install

Requires Python 3.11–3.13 (an AIPerf constraint). Runs on macOS (arm64) and Linux (amd64, arm64).

```bash
python3.12 -m venv .venv            # any of 3.11–3.13 (macOS: brew install python@3.12)
.venv/bin/pip install -e .          # for development: pip install -e ".[dev]"
source .venv/bin/activate
```

- AIPerf 0.12.0 is installed with it. `personaload run` refuses to start with any other version.
- The first run downloads the tiktoken `o200k_base` encoding once for token counting. Offline, pre-fill `TIKTOKEN_CACHE_DIR`.
- On Linux arm64, AIPerf's dependency `crick` is built from source, so a C compiler is needed.

## Quick start: one minute with the mock server

Use two terminals and run `source .venv/bin/activate` in each first.

```bash
personaload mockserver                    # 127.0.0.1:8000, TTFT 200ms, 40 tok/s per stream
```

```bash
personaload validate scenarios/smoke.yaml # resolved defaults, sessions and file sizes per stage, AIPerf command
personaload run scenarios/smoke.yaml      # about 50 seconds
```

When it finishes, `results/smoke-<timestamp>/report.md` holds the stage table and the headline. The `**헤드라인**` line is the headline.

```
**헤드라인**: stage 02: $0.000/1M out, $0.0000/1K turns, $0.000000/session (out tok/s 63.2, SLO PASS)

| stage | conc | active sess | req/s | out tok/s | TTFT p50/p95/p99 | ITL p50/p95 | E2E p95 | err% | cache hit% | SLO | $/1M out | $/1K turns | $/session |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 01 | 2 | 2.0 (2–2) | 0.73 | 39.3 | 201/202/202 | 25.0/26.0 | 2089 | 0.0% | 68.6% (cached 보고 11/11건) | PASS | $0.000 | $0.0000 | $0.000000 |
| 02 | 4 | 4.0 (4–4) | 1.33 | 63.2 | 201/202/202 | 25.0/26.0 | 2284 | 0.0% | 84.5% (cached 보고 20/20건) | PASS | $0.000 | $0.0000 | $0.000000 |
```

Costs are `$0` here because `smoke.yaml` sets `gpu_hourly_usd: 0` for a local machine. `cached 보고 11/11건` means "11 of 11 requests reported cached tokens".

Mock server options: `--ttft`, `--tps`, `--jitter`, `--error-rate` (HTTP 500 share), `--cache-ttft-factor` (TTFT multiplier on a prefix-cache hit), `--no-usage`, `--seed`. The same seed and request always give the same output length and error outcome.

## llama.cpp on a Mac

```bash
brew install llama.cpp
scripts/llama-cpp-macos.sh               # downloads a ~0.8 GB model from Hugging Face on first run
```

When the server is up at `http://127.0.0.1:8000/v1` as `local-model`, run this in another terminal (activate the venv first):

```bash
personaload run scenarios/smoke.yaml
```

- Change the model with `MODEL=<HF repo[:quant]>`. The default is `ggml-org/gemma-3-1b-it-GGUF`.
- `PARALLEL` sets the request slots (default 4) and `CTX_PER_SLOT` the context per slot (default 4096). With fewer slots than the scenario's peak concurrency, requests queue inside the server and TTFT rises.
- `smoke.yaml` uses `max_tokens_field: max_tokens`, which llama.cpp, vLLM and the mock all accept.

## vLLM on a GPU server

```bash
python -m venv ~/vllm && ~/vllm/bin/pip install vllm    # a separate venv from personaload
PATH=~/vllm/bin:$PATH scripts/vllm-linux.sh              # default Qwen/Qwen3-8B, max-model-len 32768
```

- The script enables `--enable-prompt-tokens-details`, so responses report cached tokens and the report can show cache hit%.
- It binds to `127.0.0.1` by default. To load it from another machine, start it with `HOST=0.0.0.0`. There is no authentication, so use a trusted network only.
- Qwen3 thinks by default and can spend a short `max_tokens` entirely on reasoning. For chat traffic, add `extra_body: {chat_template_kwargs: {enable_thinking: false}}` to the scenario (the examples have it as a comment).

On Apple Silicon, the same script works with [vllm-metal](https://github.com/vllm-project/vllm-metal), vLLM's Metal/MLX plugin, using MLX-format models (`mlx-community/*`):

```bash
brew tap vllm-project/vllm-metal https://github.com/vllm-project/vllm-metal
brew install vllm-project/vllm-metal/vllm-metal        # vLLM + Metal plugin, about 1.9 GB
MODEL=mlx-community/Qwen3-0.6B-4bit MAX_LEN=8192 scripts/vllm-linux.sh
```

## Example scenarios

| Scenario | What it models | Time |
|---|---|---|
| [smoke.yaml](scenarios/smoke.yaml) | 3 personas, 2–4 turns, concurrency 2 and 4 | ~1 min |
| [persona-shared.yaml](scenarios/persona-shared.yaml) | 5 personas shared with Zipf popularity, 5–30 turns, concurrency 10/50/200, Prometheus on 9090 | ~12 min |
| [persona-diverse.yaml](scenarios/persona-diverse.yaml) | 500 personas used evenly; otherwise the same as persona-shared | ~12 min |
| [long-context.yaml](scenarios/long-context.yaml) | 30–50 turns, history grows into the 10k-token range, concurrency 8 and 32 | ~11 min |
| [rag-assistant.yaml](scenarios/rag-assistant.yaml) | RAG support chatbot: one shared instruction prompt, a 800–2000-token retrieved document every turn, `$/session` as cost per conversation | ~11 min |

## Comparing runs and handing off to a coding agent

```bash
personaload report results/persona-shared-20260923-101500 results/persona-diverse-20260923-103000
```

This reads only `stage_NN/stage.json` in each directory and prints the headlines and stage rows in one table. It also works on runs whose request records were deleted (`record_requests: false`). If the dataset sha256 of the same stage number differs, a "workload differs" warning goes above the table.

Add `--handoff handoff.md` to also write a work order for a coding agent in another session. Based on the last directory, it lists:
- failed stages, SLO violations, and cache or usage problems
- for each problem: the evidence numbers, likely causes, what to change, and a done condition
- the re-measure commands, with absolute paths

```bash
personaload report results/before-20260923-101500 results/after-20260924-093000 --handoff handoff.md
```

## Prometheus

With `output.prometheus_port` in the scenario, `http://<prometheus_host>:<port>/metrics` is live during the run (AIPerf `--api-port`).

```bash
curl -s http://127.0.0.1:9090/metrics | grep aiperf_time_to_first_token_ms
```

- **Metric names** follow AIPerf:
  - `aiperf_time_to_first_token_ms` (a summary with quantile labels)
  - `aiperf_inter_token_latency_ms`, `aiperf_request_latency_ms`
  - `aiperf_output_token_throughput_avg_tps`, `aiperf_request_throughput_avg_requests_per_sec`
  - `aiperf_request_error_rate_avg_percent`, `aiperf_overall_usage_prompt_cache_read_pct_avg_percent`
- **Updates and stages:** values update every 5 seconds. Each stage runs its own AIPerf process, so the endpoint drops briefly between stages; the `benchmark_id` label tells the stages apart. If the port is already taken, the run stops before the first stage.
- **Differences from the report:** live values include the warmup. The live inter-token latency is a per-request mean, unlike the report's pooled chunk gaps.

## Scenario fields and defaults

The full example is in [SPEC_B.md §6](SPEC_B.md). Durations look like `500ms`, `30s`, `3m`, `1h`. A distribution is `{dist: uniform|lognormal|exponential|constant, ...}`; without `dist` it is uniform. Values are clamped to `min`/`max`.

| Field | Meaning | Default |
|---|---|---|
| `name`, `seed` | results directory name; workload seed (same seed, byte-identical datasets) | `name` required, `seed` 0 |
| `target.base_url`, `target.model` | OpenAI-compatible endpoint and model name | required |
| `target.api_key_env` | environment variable holding the API key (masked as `***` in result files) | none |
| `target.timeout` | request timeout | 120s |
| `target.tokenizer` | AIPerf tokenizer | `builtin` (o200k_base) |
| `target.max_tokens_field` | `max_completion_tokens` or `max_tokens` | `max_completion_tokens` |
| `target.extra_body` | engine-specific parameters copied into every request body | `{}` |
| `personas.count`, `personas.system_prompt_tokens` | number of synthetic personas and their system-prompt length distribution | required (synthetic) |
| `personas.source`, `personas.file` | `synthetic`, or `file` (JSONL lines `{"name", "system_prompt"}`) | `synthetic` |
| `session.turns`, `user_input_tokens`, `max_output_tokens`, `think_time` | the four session-shape distributions | required |
| `session.language` | language of the synthetic text, `en` / `ko` | `en` |
| `session.retrieved_tokens` | RAG: a retrieved document of this length in front of every user turn; it stays in the history | none (RAG off) |
| `session.persona_selection`, `zipf_s` | how sessions pick a persona, `uniform` / `zipf` | `uniform`, 1.0 |
| `load.mode` | `concurrency` (fixed session slots) or `arrival_rate` (Poisson session arrivals) | required |
| `load.stages` | concurrency: `{concurrency, duration, warmup, sessions?}`; arrival_rate: `{sessions_per_sec, duration, warmup, max_concurrency}` | required, `warmup` 0s |
| `load.drain_timeout` | how long to wait for in-flight requests after `duration` | same as `target.timeout` |
| `load.session_pool_factor` | headroom factor for the number of sessions in concurrency stages | 3 |
| `slo.ttft_p95`, `slo.itl_p95`, `slo.error_rate_max` | SLO limits (error rate as a 0–1 fraction) | required |
| `cost.gpu_hourly_usd`, `gpu_count`, `label` | cost inputs | `gpu_hourly_usd` required, 1, `""` |
| `output.dir` | base results directory (`run --out` overrides it) | `results/` |
| `output.prometheus_port`, `prometheus_host` | live `/metrics` | none (off), `127.0.0.1` |
| `output.record_requests` | if false, request record files are deleted after each stage (`stage.json` stays) | true |

`personaload validate` reports rule violations as errors and suspicious settings as warnings. Examples of warnings: `think_time.max > 60s`, or `drain_timeout < target.timeout`.

## How it measures

The report is computed from **per-request records** (`profile_export.jsonl`), not from AIPerf's aggregate JSON. Full rules: [SPEC_B.md §9](SPEC_B.md).

- **Window:**
  - `t0` is the first request's issue time (for arrival_rate, the minimum after subtracting the scheduled offset).
  - `W = t0 + warmup`, `E = W + duration`, and `E_eff = min(E, Ctrl+C or watchdog time)`.
  - Warmup traffic runs as real load and is only excluded from aggregation. AIPerf's own warmup phase would not warm the persona prefix cache.
- **Latency, error rate, cache hit%:** requests that **started** in `[W, E_eff)`. Latency percentiles use successful requests only.
- **Throughput (req/s, out tok/s):** successful requests that **ended** in `[W, E_eff)`, divided by `E_eff − W`.
- **TTFT, E2E:** AIPerf values, p50/p90/p95/p99 with linear interpolation.
- **ITL:** the pooled distribution of gaps between SSE chunks that carry data, across all included requests. AIPerf's per-request mean `inter_token_latency` goes into the JSON for reference only.
- **Error rate:** errors ÷ (successes + errors). Requests cancelled after the drain leave no record, so they are counted from the log and added in. Latency percentiles are then marked as censored lower bounds.
- **Cache hit%:** Σ cached tokens ÷ Σ prompt tokens over successful requests whose usage has prompt tokens. The report also shows how many requests reported cached tokens at all.
- **Servers without usage:** output tokens fall back to the chunk count, with a warning. Prompt tokens and cache hit% become N/A.
- **Active sessions:** the time-weighted mean, min and max of open session instances. A session runs from its turn-0 issue to its last turn's end.
- **SLO:** `TTFT p95 ≤ slo.ttft_p95`, `ITL p95 ≤ slo.itl_p95`, `error rate ≤ slo.error_rate_max`. Any known value over its limit gives FAIL. No violation but some value unknown gives N/A. Otherwise PASS.
- **Warnings:**
  - dataset reuse
  - think lag p99 > 100 ms (client bottleneck)
  - clock skew
  - AIPerf worker CPU warnings
  - drain and user cancellations
  - missing usage
- **arrival_rate stages** also report start lag p50/p95/p99, queued sessions (start lag > 100 ms) and sessions that never started. Sessions beyond `max_concurrency` wait; they are not dropped.

## Cost

```
$/1M out    = gpu_hourly_usd × gpu_count ÷ (out tok/s × 3600) × 1,000,000
$/1K turns  = gpu_hourly_usd × gpu_count ÷ (req/s × 3600) × 1,000
$/session   = $/1K turns ÷ 1,000 × E[session.turns]
```

One turn is one request. `$/1K turns` and `$/session` are the unit costs for budgeting a chatbot: cost per answer and per conversation. `E[turns]` is the expected turn count after clamping.

The **headline** comes from the complete, SLO-passing stage with the highest out tok/s. It reports that stage's `$/1M out`, `$/1K turns` and `$/session`; on a tie, the lower stage number wins. If no stage passes, the headline says so. With 0 out tok/s, the cost is N/A.

## Results directory

```
results/<name>-<YYYYmmdd-HHMMSS>/
  scenario.resolved.yaml      # scenario with defaults applied
  report.json / report.md     # stage table, headline, warnings
  stage_01/
    dataset.jsonl             # AIPerf multi_turn dataset (reproducible from the seed)
    aiperf.argv.json          # AIPerf command and environment (API keys as ***)
    aiperf.console.log        # AIPerf stdout
    aiperf/                   # AIPerf outputs: profile_export.jsonl, logs/aiperf.log, aggregate JSON
    stage.json                # stage summary (what `personaload report` reads)
```

- AIPerf's `inputs.json` (roughly dataset size × mean turns) is always deleted when a stage ends.
- If a stage fails (AIPerf exits abnormally, or the watchdog fires), the remaining stages are skipped. AIPerf 0.12.0 exits abnormally when every request of a stage fails, so a dead target stops the run at the first stage.
- **One Ctrl+C:**
  - sends SIGINT to AIPerf once
  - saves the partial results as a `partial` stage
  - skips the remaining stages, and exits with code 130
  - SIGTERM and SIGHUP behave the same.
- **A second Ctrl+C** kills AIPerf at once.

## Development

```bash
pip install -e ".[dev]"
ruff format src tests && ruff check src tests
pytest -q --cov=personaload            # integration tests start real AIPerf and the mock server (-m "not integration" to skip)
```

## License

personaload is licensed under [Apache-2.0](LICENSE).

It depends on [AIPerf](https://github.com/ai-dynamo/aiperf) 0.12.0 (NVIDIA, Apache-2.0):
- AIPerf is installed as a pip dependency, and personaload runs `python -m aiperf profile` as a separate process, using only its input and output files.
- personaload does not modify, copy or redistribute AIPerf code.
- The license review, including transitive dependencies, is in [docs/prior-art.md appendix C](docs/prior-art.md) (Korean).

Licenses of the models and tokenizers you benchmark are separate.
