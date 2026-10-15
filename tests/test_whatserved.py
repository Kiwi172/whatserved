"""Tests for whatserved. Run with: python3 -m pytest tests/ (or python3 tests/test_whatserved.py)

Fake inference servers (LM Studio / Ollama / llama.cpp / vLLM / web UI) are
started on ephemeral localhost ports with the stdlib http.server, so engine
fingerprinting is proven without any real LLM server installed.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TOOL = str(Path(__file__).resolve().parent.parent / "whatserved.py")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import whatserved as ws  # noqa: E402

servers = []


def serve(routes):
    """routes: {path: (status, content-type, body bytes)}; others -> 404."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            r = routes.get(self.path)
            if r is None:
                self.send_response(404)
                self.end_headers()
                return
            status, ctype, body = r
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    servers.append(srv)
    return srv.server_address[1]


def teardown():
    for s in servers:
        s.shutdown()
    servers.clear()


def run(*argv):
    return subprocess.run([sys.executable, TOOL, *argv],
                          capture_output=True, text=True)


LMSTUDIO = json.dumps({"object": "list", "data": [{
    "id": "qwen3.8-flash-next-iq3_s", "object": "model",
    "status": {"value": "loaded"},
    "meta": {"n_ctx": 131072, "size": 16000000000}}]}).encode()

LLAMACPP = json.dumps({"object": "list", "data": [
    {"id": "models", "object": "model", "owned_by": "llama.cpp"}]}).encode()

OLLAMA_TAGS = json.dumps({"models": [{"name": "llama3.2:latest"},
                                     {"name": "qwen3:4b"}]}).encode()
OLLAMA_PS = json.dumps({"models": [{"name": "qwen3:4b",
                                    "ctx_len": 8192}]}).encode()

VLLM_MODELS = json.dumps({"object": "list", "data": [
    {"id": "hf-model", "object": "model", "owned_by": "vllm"}]}).encode()
VLLM_VER = json.dumps({"version": "0.6.3"}).encode()


def test_fingerprint_lmstudio_style():
    port = serve({"/v1/models": (200, "application/json", LMSTUDIO)})
    info = ws.fingerprint(port, 2)
    assert info["engine"] == "LM Studio-style API"
    m = info["models"][0]
    assert m["id"] == "qwen3.8-flash-next-iq3_s"
    assert m["state"] == "loaded" and m["n_ctx"] == 131072
    teardown()


def test_fingerprint_llamacpp():
    port = serve({"/v1/models": (200, "application/json", LLAMACPP)})
    info = ws.fingerprint(port, 2)
    assert info["engine"] == "llama.cpp (llama-server)"
    teardown()


def test_fingerprint_ollama_with_loaded():
    port = serve({"/api/tags": (200, "application/json", OLLAMA_TAGS),
                  "/api/ps": (200, "application/json", OLLAMA_PS)})
    info = ws.fingerprint(port, 2)
    assert info["engine"] == "Ollama"
    q = [m for m in info["models"] if m["id"] == "qwen3:4b"][0]
    assert q["state"] == "loaded" and q["n_ctx"] == 8192
    teardown()


def test_fingerprint_vllm():
    port = serve({"/v1/models": (200, "application/json", VLLM_MODELS),
                  "/version": (200, "application/json", VLLM_VER)})
    info = ws.fingerprint(port, 2)
    assert info["engine"] == "vLLM"
    assert any("0.6.3" in n for n in info["notes"])
    teardown()


def test_fingerprint_webui_title():
    port = serve({"/": (200, "text/html", b"<html><title>Open WebUI</title>")})
    info = ws.fingerprint(port, 2)
    assert info["engine"] == "web UI: Open WebUI"
    teardown()


def test_fingerprint_closed_port():
    serve({"/": (200, "text/html", b"hi")})  # occupy + get a free-ish port
    dead = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = dead.server_address[1]
    dead.server_close()
    assert ws.fingerprint(port, 1) is None


def test_guess_from_cmdline():
    assert ws.guess_from_cmdline("/usr/local/bin/llama-server -m x.gguf") \
        == "llama-server"
    assert ws.guess_from_cmdline("ollama serve") == "Ollama"
    assert ws.guess_from_cmdline("/usr/bin/python3 -m http.server") is None


def test_listening_ports_finds_own_server():
    port = serve({"/v1/models": (200, "application/json", LMSTUDIO)})
    lp = ws.listening_ports()
    assert port in lp and lp[port]          # socket inode present
    pid, cmd = ws.pid_for_inode(lp[port])
    assert pid == str(__import__("os").getpid()) or str(pid) == str(
        __import__("os").getpid())
    teardown()


def test_cli_ports_and_json():
    port = serve({"/v1/models": (200, "application/json", LMSTUDIO)})
    r = run("--ports", str(port), "--json")
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    svc = [s for s in d["services"] if s["port"] == port][0]
    assert svc["engine"] == "LM Studio-style API"
    assert svc["models"][0]["id"] == "qwen3.8-flash-next-iq3_s"
    teardown()


if __name__ == "__main__":
    import inspect
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception as e:
                failed += 1
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
            finally:
                teardown()
    sys.exit(1 if failed else 0)
