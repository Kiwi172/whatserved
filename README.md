# whatserved

[![tests](https://github.com/Kiwi172/whatserved/actions/workflows/ci.yml/badge.svg)](https://github.com/Kiwi172/whatserved/actions/workflows/ci.yml)

**What is actually served on this machine's inference ports — which
process, which engine, which model, which context?**

You run llama-server as a systemd unit, LM Studio on the desktop, Ollama
"just to try something", and a serve wrapper that defaults to port 8080
like everything else. A month later you cannot remember which port is
which, whether the thing on 8080 is your model or your friend's Ollama,
or what context length it actually started with. `ss` tells you a PID.
`whatserved` tells you the truth:

```
$ whatserved --all
port   engine                             process                                models
--------------------------------------------------------------------------------------------------------------
8080   LM Studio-style API                /home/you/Strata/.venv/bin/python /h  qwen3.8-flash-next [loaded] ctx=131072
8000   unknown HTTP/TCP service           .venv/bin/python -u -m src.api.main
8138   web UI: Strata Fleet               python3 server.py
8791   Ollama                             node ...
```

It joins three sources that nobody joins by hand:

1. **the kernel** — `/proc/net/tcp` for what is really listening,
2. **the process table** — socket inode → PID → full command line,
3. **the API itself** — GETs `/v1/models`, `/api/tags`, `/api/ps`,
   `/version`, `/health`, `/`, and fingerprints the answer:
   llama.cpp, vLLM, Ollama, LM Studio-style, or a web UI's `<title>`.

Read-only: it sends GET requests to localhost and reads `/proc`. It
never starts, stops, or reconfigures anything. No network beyond
localhost, no dependencies, one file.

---

## Install

Linux (the `/proc` attribution; on other systems it still fingerprints
APIs, just without process names):

```bash
curl -sO https://raw.githubusercontent.com/Kiwi172/whatserved/main/whatserved.py
chmod +x whatserved.py
```

Or:

```bash
pipx install git+https://github.com/Kiwi172/whatserved
```

## Use

```bash
whatserved                 # the usual inference ports that are listening
whatserved --all           # every listening TCP port on the box
whatserved --ports 8080,11434
whatserved --json | jq '.services[] | select(.models != [])'
```

The default port list is the local-inference ecosystem's habits:
11434/11435 (Ollama), 1234 (LM Studio), 8080/8081, 8000/8001
(vLLM, llama-server), 3000/3001 (Open WebUI), 7860
(text-generation-webui), 2888 (Jan), 1025/1026 (MLC).

## What it can tell you

| signal | source |
|---|---|
| port is really listening | `/proc/net/tcp` + `tcp6` |
| which process owns it | socket inode → `/proc/PID/fd` → cmdline |
| engine family | API shape: `owned_by: llama.cpp`, `owned_by: vllm`, LM Studio's `status/meta.n_ctx`, Ollama's `/api/tags` |
| which model is loaded, and its context | `/v1/models` meta, Ollama `/api/ps` (`ctx_len`) |
| web UIs | `<title>` of the root page |
| when the API says nothing | the process cmdline (`llama-server`, `ollama serve`, …) |

When the API and the command line disagree, it shows you both — that
disagreement is usually the bug you were hunting.

## Non-goals

- **Not a process manager.** It will not start, stop, or kill anything.
- **Not remote.** It inspects localhost; point your own client at remote
  servers.
- **Not exhaustive on TLS.** HTTPS-only servers on those ports are
  reported by process, not by API.

## Tests

```bash
python3 -m pytest tests/      # or: python3 tests/test_whatserved.py
```

The tests stand up fake LM Studio / Ollama / llama.cpp / vLLM / web-UI
servers on ephemeral localhost ports with the stdlib `http.server`, so
every fingerprint is proven against a server the test fully controls —
no real LLM server needed.

## License

MIT
