#!/usr/bin/env bash
# llama.cpp server on an Apple Silicon Mac (Metal), for scenarios/smoke.yaml.
# Serves an OpenAI-compatible API at http://127.0.0.1:8000/v1 under the model name "local-model".
#
#   brew install llama.cpp
#   scripts/llama-cpp-macos.sh          # the first run downloads the model (~0.8 GB) from Hugging Face
#
# Override with environment variables, e.g. MODEL=ggml-org/gemma-3-4b-it-GGUF (~2.5 GB) scripts/llama-cpp-macos.sh
set -euo pipefail

MODEL="${MODEL:-ggml-org/gemma-3-1b-it-GGUF}"  # Hugging Face repo[:quant] for llama-server -hf
PORT="${PORT:-8000}"
PARALLEL="${PARALLEL:-4}"                       # request slots; smoke.yaml goes up to concurrency 4
CTX_PER_SLOT="${CTX_PER_SLOT:-4096}"            # llama-server splits --ctx-size across the slots

if ! command -v llama-server >/dev/null; then
  echo "llama-server not found: brew install llama.cpp" >&2
  exit 1
fi

# llama-server allows every CORS origin with no API key; keep browser pages off the local server.
# The env var (not --cors-origins) so builds that predate the option still start.
export LLAMA_ARG_CORS_ORIGINS="${LLAMA_ARG_CORS_ORIGINS:-localhost}"

exec llama-server -hf "$MODEL" --host 127.0.0.1 --port "$PORT" --alias local-model \
  --parallel "$PARALLEL" --ctx-size "$((PARALLEL * CTX_PER_SLOT))" --jinja
