#!/usr/bin/env bash
# vLLM on Linux with an NVIDIA GPU, for scenarios/persona-*.yaml and long-context.yaml.
# Also runs unchanged on Apple Silicon with vllm-metal and an MLX model (see README).
# Serves an OpenAI-compatible API at http://$HOST:8000/v1 under the model name "local-model".
#
#   python -m venv ~/vllm && ~/vllm/bin/pip install vllm   # keep vLLM out of the personaload venv
#   PATH=~/vllm/bin:$PATH scripts/vllm-linux.sh
#
# Override with environment variables, e.g. MODEL=Qwen/Qwen2.5-7B-Instruct MAX_LEN=16384
# (gated models such as meta-llama/* need the license accepted on Hugging Face and HF_TOKEN set)
set -euo pipefail

MODEL="${MODEL:-Qwen/Qwen3-8B}"
HOST="${HOST:-127.0.0.1}"     # 0.0.0.0 to benchmark from another machine (no auth: trusted networks only)
PORT="${PORT:-8000}"
MAX_LEN="${MAX_LEN:-32768}"   # long-context.yaml reaches ~25k tokens of history

if ! command -v vllm >/dev/null; then
  echo "vllm not found: pip install vllm (in its own venv)" >&2
  exit 1
fi

# --enable-prompt-tokens-details returns usage.prompt_tokens_details.cached_tokens (report cache hit%)
exec vllm serve "$MODEL" --host "$HOST" --port "$PORT" --served-model-name local-model \
  --max-model-len "$MAX_LEN" --enable-prefix-caching --enable-prompt-tokens-details
