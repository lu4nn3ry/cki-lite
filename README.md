# cki-lite

Minimal open-source terminal agent for NVIDIA NIM. It uses only Python's standard library, works on Alpine Linux, exposes a CLI chat, supports OpenAI-compatible tool calling, and can execute shell commands requested by the agent.

## Requirements

- Python 3.8+
- An NVIDIA NIM API key

## Setup

```sh
export NVIDIA_API_KEY='nvapi-...'
chmod +x cki-lite.py
./cki-lite.py --list-models
./cki-lite.py
./cki-lite.py --verbose
```

The default endpoint is `https://integrate.api.nvidia.com/v1`. Override it with `NIM_BASE_URL` or `--base-url` for a self-hosted NIM deployment.

Only Gemma 3/4, OpenAI GPT-OSS and text-oriented Nemotron instruct/super/ultra/lightning/nano models are shown. Embedding, vision, safety, parser, reward and other specialized models are filtered out.

## CLI commands

- `/terminal` — execute a shell command directly.
- `/clear` — clear conversation history.
- `/quit` — exit.

If a selected model fails, cki-lite automatically tries another visible Gemma/Nemotron model while preserving the task history.

Use `--verbose` to see the observable agent trace: loop number, active model, message count, request latency, tool calls, command execution, exit codes, timeouts and model fallback. Provider hidden chain-of-thought is not exposed; the trace shows actions and results instead.

## Saved conversations and debug export

Sessions are saved automatically as JSON under `~/.cki-lite/` (override with `CKI_LITE_HOME`). The session ID is printed when chat starts.

```sh
./cki-lite.py --session 20260908-120000-a1b2c3  # resume
./cki-lite.py --export debug.json                # export all sessions
./cki-lite.py --session 20260908-120000-a1b2c3 --export debug.json
```

Inside a chat, `/save` saves immediately and `/export` exports the current session. Export files contain messages, tool calls, command output, model changes and loop context, but never the API key.

## Security

The terminal tool runs commands with the same permissions as the `cki-lite` process. Run it only on a machine you control. Never commit API keys; use `NVIDIA_API_KEY` or an interactive prompt.

## License

MIT. See [LICENSE](LICENSE).
