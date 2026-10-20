# Prefix-caching savings, measured (M5 Pro, vLLM Metal)

[한국어](cache-savings-m5.ko.md)

Measured 2026-09-24 with a pre-release personaload build (before the public history starts), AIPerf 0.12.0.

## Summary

- On a character-chat workload, turning on the engine's prefix caching cut the cost per output token (`$/1M out`) at the same SLO by **47–73%** (Qwen3 0.6B–4B).
- 8B and 14B could not meet the SLO without caching even at the lowest concurrency. Comparing peak throughput with the SLO ignored, the gap is still at least 56–66%.
- With 500 distinct personas the saving was still **38–53%** (0.6B–4B). The first turn of a session barely hits the cache, but from the second turn on the session's own history does (see [500 personas](#500-personas-persona-diverse)).
- All of the saving is on the input side (prefill). Output tokens per request were the same with and without caching. The cost per output token fell because GPU time saved on prefill went into generating more output.
- These numbers are from Apple Silicon. On NVIDIA datacenter GPUs prefill is relatively cheaper, so the saving is likely smaller. To get numbers for a real server, run the same pair on the target GPU (see [Reproduce](#reproduce)).

## Terms

- **SLO (Service Level Objective):** a numeric target for the quality users see. personaload checks three conditions per stage:
  - `TTFT p95 ≤ slo.ttft_p95`: 95% of requests get their first token within this time, i.e. how long a user waits for the reply to start.
  - `ITL p95 ≤ slo.itl_p95`: 95% of gaps between streamed chunks are within this, i.e. how fast the reply is written. 100 ms is about 10+ tokens per second.
  - `error rate ≤ slo.error_rate_max`
  - Failing any one of them is a FAIL.
- **Why compare cost at an SLO:** raising concurrency keeps raising throughput (tok/s), but users wait longer and longer. A fair comparison is "how cheaply can it serve while keeping the same user experience". The personaload headline is the `$/1M out` of the highest-throughput stage that passed the SLO.
- The SLO in this experiment is TTFT p95 2 s, ITL p95 100 ms, error rate 1%.

## Setup

| Item | Value |
|---|---|
| Machine | MacBook Pro, Apple M5 Pro, 48 GB unified memory |
| Engine | vLLM 0.30.0 + [vllm-metal](https://github.com/vllm-project/vllm-metal) 0.30.0 (Homebrew) |
| Models | `mlx-community/Qwen3-{0.6B,1.7B,4B,8B,14B}-4bit` |
| Common server flags | `--served-model-name local-model --max-model-len 8192 --enable-prompt-tokens-details` |
| Cache on / off | `--enable-prefix-caching` / `--no-enable-prefix-caching` |

- For each model, the cache-on and cache-off servers got **the same dataset**. The seed is the same, so each stage's `dataset.jsonl` has the same sha256.
- To reduce laptop thermal effects, the on/off order alternated per model (0.6B, 4B, 14B ran cache-on first; 1.7B, 8B ran cache-off first).

## Workload

```yaml
name: sweep
seed: 3
target:
  base_url: http://127.0.0.1:8000/v1
  model: local-model
  timeout: 60s
  max_tokens_field: max_tokens
  extra_body: {chat_template_kwargs: {enable_thinking: false}}
personas:
  count: 5
  system_prompt_tokens: {min: 1500, max: 3000}
session:
  turns: {min: 4, max: 10}
  user_input_tokens: {dist: lognormal, mean: 40, stddev: 20, min: 5, max: 150}
  max_output_tokens: {min: 32, max: 96}
  think_time: {dist: exponential, mean: 2s, min: 500ms, max: 8s}
  persona_selection: zipf
  zipf_s: 1.1
load:
  mode: concurrency
  stages:
    - {concurrency: 2, duration: 30s, warmup: 10s}
    - {concurrency: 4, duration: 30s, warmup: 10s}
    - {concurrency: 8, duration: 30s, warmup: 10s}
    - {concurrency: 16, duration: 30s, warmup: 10s}
slo:
  ttft_p95: 2s
  itl_p95: 100ms
  error_rate_max: 0.01
cost:
  gpu_hourly_usd: 1.0   # $1/hour: multiply by your real hourly price
```

- Five personas share long system prompts and multi-turn history builds up, which is the shape of character chat.
- Replies are short (32–96 tokens), so input dominates the work.
- Qwen3 thinking was off. With it on, the model spends a short `max_tokens` on reasoning; in smoke.yaml 48 of 48 replies hit the limit.

## Results

**Per stage**

| Model | Cache | Output tok/s (concurrency 2/4/8/16) | TTFT p50 ms (2/4/8/16) | SLO (2/4/8/16) |
|---|---|---|---|---|
| 0.6B | on | 51.3 / 113.7 / 187.8 / 273.7 | 54 / 58 / 66 / 112 | PASS / PASS / PASS / PASS |
| 0.6B | off | 45.4 / 91.8 / 145.3 / 160.9 | 169 / 191 / 221 / 403 | PASS / PASS / PASS / FAIL |
| 1.7B | on | 40.1 / 89.2 / 144.3 / 195.9 | 77 / 79 / 95 / 141 | PASS / PASS / PASS / PASS |
| 1.7B | off | 40.2 / 66.9 / 83.3 / 73.4 | 325 / 574 / 656 / 1548 | PASS / PASS / FAIL / FAIL |
| 4B | on | 41.9 / 75.9 / 111.5 / 129.3 | 144 / 156 / 184 / 328 | PASS / PASS / PASS / FAIL |
| 4B | off | 29.6 / 42.4 / 48.6 / 54.5 | 950 / 1410 / 1628 / 1804 | PASS / FAIL / FAIL / FAIL |
| 8B | on | 33.5 / 56.7 / 70.3 / 94.0 | 172 / 220 / 324 / 552 | PASS / PASS / FAIL / FAIL |
| 8B | off | 18.5 / 24.8 / 22.8 / 9.8 | 1427 / 3068 / 3405 / 5783 | FAIL / FAIL / FAIL / FAIL |
| 14B | on | 20.3 / 39.9 / 46.1 / 57.6 | 454 / 493 / 524 / 804 | FAIL / PASS / FAIL / FAIL |
| 14B | off | 9.5 / 13.5 / 12.0 / 0 | 5029 / 6141 / 7113 / N/A | FAIL / FAIL / FAIL / N/A |

- The error rate was 0% in every run.
- Cache hit was 94.5–95.2% with caching on and 0% with it off.
- With caching off, SLO failures came mostly from TTFT p95 (8B 3.1–6.5 s, 14B 6.2–8.4 s). With caching on, ITL p95 was the cause at high concurrency.
- **14B, cache on, concurrency 2:** as the first stage, the cache was cold and the sample was small (13 requests), so TTFT p95 jumped to 2.5 s. At concurrency 4 it was 0.8 s and passed.
- **14B, cache off, concurrency 16:** the server was saturated and no request finished inside the measurement window, so the SLO is N/A.

**Headline cost (highest-throughput stage that passed the SLO, at $1/hour)**

| Model | Cache on | Cache off | Saving | Saving per 100M output tokens/month |
|---|---|---|---|---|
| 0.6B | $1.015/1M (concurrency 16) | $1.912/1M (concurrency 8) | 47% | $90 |
| 1.7B | $1.418/1M (concurrency 16) | $4.150/1M (concurrency 4) | 66% | $273 |
| 4B | $2.491/1M (concurrency 8) | $9.395/1M (concurrency 2) | 73% | $690 |
| 8B | $4.899/1M (concurrency 4) | no stage passed (peak throughput with SLO ignored: $11.20/1M) | ≥56% | ≥$630 |
| 14B | $6.968/1M (concurrency 4) | no stage passed (peak throughput with SLO ignored: $20.58/1M) | ≥66% | ≥$1,361 |

- The saving percentage does not depend on the hardware price. The dollar saving is at $1/hour, so multiply by your real hourly cost.
- At equal concurrency, caching also raised throughput: 0.6B +13–70%, 1.7B −0–+167%, 4B +42–137%.

## Interpretation

- **The saving comes from prefill.** Caching skips the computation of an input prefix that was already computed. TTFT fell 3–12×, and the freed compute let the server take more concurrency at the same SLO.
- **Output token counts did not change.** The mean output tokens per request were 57–61 with caching on or off (0.6B 59.6 vs 57.0, 4B 61.5 vs 58.9, 14B 58.1 vs 59.2).
  - Output tokens are generated one at a time, so prefix caching cannot skip them.
  - The **cost per output token** still fell. Cost = GPU time ÷ output tokens; the denominator stayed the same while the GPU time spent on prefill fell.
  - At low concurrency ITL (generation speed) was the same on and off (0.6B 7.2 vs 7.0 ms). At high concurrency caching was slightly faster (0.6B at concurrency 16: 26.1 vs 37.4 ms), because less prefill work interrupted generation.
- **Cutting output cost needs other levers.** They were not measured here.
  - Fewer output tokens: turn thinking off (in smoke.yaml, replies hitting the limit went from 48/48 to 30/55), cap `max_tokens`.
  - Cheaper output tokens: quantization (FP8 etc.), speculative decoding, a different engine or GPU. personaload can compare them by running the same scenario.
- **Where hits come from.** The first turn of a new session hit 87.7%, thanks to shared personas. Later turns hit 94.3%, adding reuse of the session's own history. With distinct personas the first-turn hit falls to 1.5% while later turns stay at 95.3% (see [500 personas](#500-personas-persona-diverse)).
- **Bigger models save more.** Prefill compute is roughly proportional to parameters × input tokens, so the share of compute that caching skips grows with model size.

## 500 personas (persona-diverse)

The persona pool was raised to 500 and selected uniformly (`personas.count: 500`, `persona_selection: uniform`). Everything else (models, concurrency, SLO, output length, seed) matched the workload above. That is also the only difference between `scenarios/persona-diverse.yaml` and `persona-shared.yaml`. The shipped scenarios target datacenter GPUs (concurrency up to 200, 3.5-minute stages), so they were not run on the laptop.

**Per stage**

| Model | Cache | Output tok/s (concurrency 2/4/8/16) | TTFT p50 ms (2/4/8/16) | SLO (2/4/8/16) |
|---|---|---|---|---|
| 0.6B | on | 40.1 / 115.6 / 164.0 / 202.6 | 81 / 80 / 71 / 137 | PASS / PASS / PASS / PASS |
| 0.6B | off | 33.6 / 94.5 / 125.4 / 132.0 | 206 / 261 / 324 / 560 | PASS / PASS / PASS / FAIL |
| 1.7B | on | 39.3 / 95.1 / 138.5 / 184.2 | 102 / 110 / 118 / 197 | PASS / PASS / PASS / FAIL |
| 1.7B | off | 41.1 / 68.0 / 86.0 / 81.3 | 414 / 667 / 713 / 1177 | PASS / PASS / FAIL / FAIL |
| 4B | on | 39.6 / 63.3 / 91.5 / 85.4 | 173 / 171 / 225 / 403 | PASS / PASS / FAIL / FAIL |
| 4B | off | 29.6 / 36.5 / 50.1 / 38.4 | 1236 / 1908 / 2548 / 3244 | PASS / FAIL / FAIL / FAIL |
| 8B | on | 30.2 / 53.2 / 54.6 / 13.2 | 180 / 249 / 348 / 481 | PASS / PASS / FAIL / FAIL |
| 8B | off | 16.4 / 17.2 / 16.5 / 4.8 | 2083 / 3517 / 3243 / 5725 | FAIL / FAIL / FAIL / FAIL |
| 14B | on | 16.0 / 30.3 / 15.0 / 0 | 414 / 501 / 839 / N/A | FAIL / PASS / FAIL / N/A |
| 14B | off | 7.3 / 10.8 / 5.4 / 0 | 4865 / 8332 / 7922 / N/A | FAIL / FAIL / FAIL / N/A |

- There were no errors (14B at concurrency 16 is N/A both on and off because no request finished inside the window). Cache hit was 88.6–95.6% with caching on and 0% off.
- At equal concurrency, caching raised throughput by 0.6B +19–54%, 1.7B −4–+127%, 4B +34–123%.

**Headline cost ($1/hour)**

| Model | 5 personas: on → off (saving) | 500 personas: on → off (saving) | Saving per 100M output tokens/month at 500 personas |
|---|---|---|---|
| 0.6B | $1.015 → $1.912 (47%) | $1.371 → $2.216 (38%) | $85 |
| 1.7B | $1.418 → $4.150 (66%) | $2.006 → $4.083 (51%) | $208 |
| 4B | $2.491 → $9.395 (73%) | $4.386 → $9.395 (53%) | $501 |
| 8B | $4.899 → no stage passed (≥56%) | $5.221 → no stage passed (≥68%) | ≥$1,093 |
| 14B | $6.968 → no stage passed (≥66%) | $9.158 → no stage passed (≥64%) | ≥$1,656 |

- For 8B and 14B the cache-off side is a lower bound computed from peak throughput with the SLO ignored. At 500 personas, 8B reached 17.2 tok/s ($16.15/1M) and 14B 10.8 tok/s ($25.72/1M). Cache-off throughput differs between the two conditions, so do not compare the two lower bounds with each other.

**Interpretation**

- **Only the first turn loses the cache.** First-turn cache hit fell from 87.7% to 1.5% (217 requests). From the second turn on it was 95.3% (790 requests). Reusing the session's own history alone cut cost by about half.
- **Shared personas pay off only with caching.**
  - Cache on: 500 personas cost 7–76% more than 5 (4B: $2.491 vs $4.386).
  - Cache off: the two conditions cost nearly the same (4B: $9.395 for both; only 0.6B differed, by 16%). Without a cache, sharing does not reduce compute.
- **Large models collapsed at concurrency 16 even with caching.** 8B reached 13.2 tok/s (94.0 with 5 personas); 14B reached 15.0 at concurrency 8 and 0 at 16.
  - It was not memory. vLLM logs showed 12–18% KV-cache usage and 0 preemptions.
  - First-turn prefill held the GPU. In the same window the vLLM logs showed 400–1,000 input tokens/s against 2–5 output tokens/s (48 in one interval), because every new session brought an uncached ~2,000-token persona. With 5 personas the same window showed 80–210 input and 85–96 output tokens/s.
  - So in a service with many distinct personas, persona prompt length directly drives cost and the throughput ceiling.

## Limits

- **Not a datacenter GPU.** These are Apple Silicon + MLX 4-bit results. Datacenter GPUs such as H100 or L40S have far more compute per unit of memory bandwidth, so prefill is relatively cheap and the saving is likely smaller.
- **Each condition was measured once.** Thermal and background-load variation was not averaged out with repeats.
- **Both sweeps share a seed.** The 5- and 500-persona conditions have the same session shape (turn counts, output caps) and differ only in persona text, so the difference between them comes from persona sharing.
- **Coarse concurrency steps.** With 2/4/8/16 the headline saving is coarse, and it can be overstated when the SLO boundary falls between steps.
- **Short replies.** At 32–96 tokens, input dominates. Longer replies shrink the saving.

## Reproduce

```bash
# engine (Apple Silicon)
brew tap vllm-project/vllm-metal https://github.com/vllm-project/vllm-metal
brew install vllm-project/vllm-metal/vllm-metal

# per model: cache on
vllm serve mlx-community/Qwen3-8B-4bit --host 127.0.0.1 --port 8000 --served-model-name local-model \
  --max-model-len 8192 --enable-prefix-caching --enable-prompt-tokens-details
personaload run sweep.yaml --out runs/8B/on

# cache off (restart the server)
vllm serve mlx-community/Qwen3-8B-4bit --host 127.0.0.1 --port 8000 --served-model-name local-model \
  --max-model-len 8192 --no-enable-prefix-caching --enable-prompt-tokens-details
personaload run sweep.yaml --out runs/8B/off

# compare
personaload report runs/8B/on/* runs/8B/off/*
```

On an NVIDIA server, compare a server started with `scripts/vllm-linux.sh` against the same command with `--no-enable-prefix-caching`.
