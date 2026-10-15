#!/usr/bin/env python3
"""whatserved -- what is actually served on this machine's LLM ports?

You run llama-server as a systemd unit, LM Studio on the desktop, Ollama
"just to try something", and a serve wrapper that defaults to the same
port as everything else. Six weeks later: which port serves what, which
model is loaded right now, and what context did it start with?

whatserved lists the listening ports (yours or the usual inference ones),
attributes each to a process via /proc, and fingerprints the API:
OpenAI-compatible /v1/models, Ollama /api/tags, llama.cpp, vLLM, LM
Studio, web UI titles. Read-only: it sends GET requests and reads
/proc, it never starts, stops, or touches anything. Stdlib-only.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request

VERSION = "0.1.0"

# ports the local-inference ecosystem habitually uses
COMMON_PORTS = [
    11434, 11435,   # ollama
    1234,           # LM Studio
    8080, 8081,     # llama-server / strata / misc
    8000, 8001,     # vllm / text-generation-webui api
    5000, 5001,     # legacy
    3000, 3001,     # open-webui
    7860,           # text-generation-webui gradio
    2888,           # jan
    1025, 1026,     # mlc
]

PROBES = [
    ("GET", "/v1/models"),
    ("GET", "/api/tags"),
    ("GET", "/api/ps"),
    ("GET", "/api/version"),
    ("GET", "/version"),
    ("GET", "/health"),
    ("GET", "/"),
]


# ---------------------------------------------------------------------------
# listening ports + process attribution (Linux /proc)
# ---------------------------------------------------------------------------

def listening_ports():
    """{port: socket-inode} for LISTEN sockets on this host."""
    out = {}
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                next(f)
                for line in f:
                    p = line.split()
                    if p[3] != "0A":          # 0A = TCP_LISTEN
                        continue
                    port = int(p[1].rsplit(":", 1)[1], 16)
                    out[port] = p[9]
        except OSError:
            continue
    return out


def pid_for_inode(inode):
    """Reverse-map a socket inode to (pid, cmdline) via /proc/*/fd."""
    target = f"socket:[{inode}]"
    for fd_dir in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            if os.readlink(fd_dir) == target:
                pid = fd_dir.split("/")[2]
                try:
                    cmd = open(f"/proc/{pid}/cmdline", "rb").read()
                    cmd = cmd.replace(b"\0", b" ").decode("utf-8", "replace").strip()
                except OSError:
                    cmd = ""
                return int(pid), cmd
        except OSError:
            continue
    return None, None


# ---------------------------------------------------------------------------
# API fingerprinting
# ---------------------------------------------------------------------------

def http_get(port, path, timeout):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"User-Agent": "whatserved/" + VERSION,
                 "Accept": "application/json, text/html;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(256 * 1024)
            return r.status, dict(r.headers), body
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(64 * 1024) if e.fp else b""
    except Exception:
        return None, {}, b""


def fingerprint(port, timeout):
    """Probe the usual endpoints; return {engine, models, notes} or None."""
    info = {"engine": None, "models": [], "notes": [], "endpoints": []}
    bodies = {}
    for _, path in PROBES:
        status, headers, body = http_get(port, path, timeout)
        if status is None:
            return None                       # nothing listening / no answer
        if status < 400:
            info["endpoints"].append(path)
            bodies[path] = (headers, body)
    if not bodies:
        return None

    def hdr(path, name):
        headers = bodies.get(path, ({}, b""))[0]
        for k, v in headers.items():
            if k.lower() == name:
                return v
        return ""

    server = hdr("/v1/models", "server") or hdr("/", "server")

    # --- OpenAI-compatible /v1/models ---------------------------------------
    if "/v1/models" in bodies:
        hdr, raw = bodies["/v1/models"]
        try:
            data = json.loads(raw).get("data", [])
        except (json.JSONDecodeError, AttributeError):
            data = []
        if isinstance(data, list) and data:
            for m in data:
                entry = {"id": m.get("id", "?")}
                meta = m.get("meta") or {}
                if isinstance(meta, dict):
                    if meta.get("n_ctx"):
                        entry["n_ctx"] = meta["n_ctx"]
                    if meta.get("size"):
                        entry["size_vram"] = meta["size"]
                st = m.get("status")
                if isinstance(st, dict) and st.get("value"):
                    entry["state"] = st["value"]
                info["models"].append(entry)
            owned = {m.get("owned_by") for m in data if m.get("owned_by")}
            if any(isinstance(m.get("status"), dict) and "meta" in m
                   for m in data):
                info["engine"] = "LM Studio-style API"
            elif "llama.cpp" in owned:
                info["engine"] = "llama.cpp (llama-server)"
            elif "vllm" in owned:
                info["engine"] = "vLLM"
            else:
                info["engine"] = "OpenAI-compatible API"

    # --- Ollama --------------------------------------------------------------
    if "/api/tags" in bodies:
        try:
            tags = json.loads(bodies["/api/tags"][1]).get("models", [])
        except (json.JSONDecodeError, AttributeError):
            tags = []
        info["engine"] = "Ollama"
        info["models"] = [{"id": t.get("name", "?")} for t in tags][:50]
        if "/api/ps" in bodies:
            try:
                ps = json.loads(bodies["/api/ps"][1]).get("models", [])
                for m in ps:
                    for e in info["models"]:
                        if e["id"] == m.get("name"):
                            e["state"] = "loaded"
                            if m.get("ctx_len"):
                                e["n_ctx"] = m["ctx_len"]
            except (json.JSONDecodeError, AttributeError):
                pass

    # --- version / health hints ----------------------------------------------
    if "/version" in bodies:
        try:
            info["notes"].append("vllm /version: "
                                 + json.loads(bodies["/version"][1]).get("version", "?"))
            info["engine"] = info["engine"] or "vLLM"
        except (json.JSONDecodeError, AttributeError):
            pass

    # --- web UI titles (when no JSON API answered) ----------------------------
    if info["engine"] is None and "/" in bodies:
        html = bodies["/"][1].decode("utf-8", "replace")
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        if m:
            info["engine"] = "web UI: " + m.group(1).strip()[:60]
        elif "server" in {k.lower() for k in bodies["/"][0]}:
            info["engine"] = "HTTP service"
    if server:
        info["notes"].append("Server: " + server)
    return info


def guess_from_cmdline(cmd):
    c = cmd.lower()
    for pat, name in [
        ("llama-server", "llama-server"),
        ("llama.cpp", "llama.cpp"),
        ("ollama", "Ollama"),
        ("vllm", "vLLM"),
        ("lm studio", "LM Studio"),
        ("lmstudio", "LM Studio"),
        ("text-generation-webui", "text-generation-webui"),
        ("open-webui", "Open WebUI"),
        ("strata", "Strata serve"),
        ("jan", "Jan"),
        ("mlc", "MLC"),
        ("koboldcpp", "KoboldCpp"),
    ]:
        if pat in c:
            return name
    return None


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        prog="whatserved",
        description="What is actually served on this machine's inference "
                    "ports? Maps port -> process -> API -> loaded model. "
                    "Read-only, stdlib-only, no network beyond localhost.")
    ap.add_argument("--ports", metavar="P1,P2",
                    help="explicit port list (default: the usual inference "
                         "ports that are actually listening)")
    ap.add_argument("--all", action="store_true",
                    help="probe every listening TCP port, not just the "
                         "usual ones")
    ap.add_argument("--timeout", type=float, default=2.0,
                    help="per-request timeout seconds (default: 2)")
    ap.add_argument("--no-proc", action="store_true",
                    help="skip /proc process attribution")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--version", action="version",
                    version=f"whatserved {VERSION}")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def c(code, s):
        return f"\033[{code}m{s}\033[0m" if color else s

    have_proc = os.path.isdir("/proc/net")
    listen = listening_ports() if have_proc else {}

    if args.ports:
        ports = [int(p) for p in re.split(r"[,\s]+", args.ports) if p.strip()]
    elif args.all or not have_proc:
        ports = sorted(set(listen) | set(COMMON_PORTS))
    else:
        ports = sorted(p for p in COMMON_PORTS if p in listen)
        if not ports:
            print("no usual inference port is listening; "
                  "use --all to probe every open port")
            return 0

    rows = []
    for port in ports:
        entry = {"port": port, "listening": port in listen if have_proc else None,
                 "pid": None, "cmd": None, "engine": None, "models": [],
                 "notes": []}
        if have_proc and not args.no_proc and port in listen:
            pid, cmd = pid_for_inode(listen[port])
            entry["pid"], entry["cmd"] = pid, cmd
        info = fingerprint(port, args.timeout)
        if info:
            entry["engine"] = info["engine"]
            entry["models"] = info["models"]
            entry["notes"] = info["notes"]
            entry["endpoints"] = info["endpoints"]
        elif guess_from_cmdline(entry["cmd"] or ""):
            entry["engine"] = guess_from_cmdline(entry["cmd"]) + " (from cmdline; no known API answered)"
        if entry["engine"] is None and entry["cmd"]:
            entry["engine"] = guess_from_cmdline(entry["cmd"]) or "unknown HTTP/TCP service"
        rows.append(entry)

    if args.as_json:
        print(json.dumps({"tool": "whatserved", "version": VERSION,
                          "services": rows}, indent=2))
        return 0

    found = [r for r in rows if r["engine"]]
    if not found:
        print("nothing serving on the probed ports.")
        return 0
    print(f"{'port':<6} {'engine':<54} {'process':<38} models")
    print("-" * 110)
    for r in found:
        proc = (r["cmd"] or f"pid {r['pid']}")[:38] if r["pid"] else "-"
        models = ", ".join(
            m["id"] + (f" [{m['state']}]" if m.get("state") else "")
            + (f" ctx={m['n_ctx']}" if m.get("n_ctx") else "")
            for m in r["models"][:3])
        if len(r["models"]) > 3:
            models += f" (+{len(r['models']) - 3} more)"
        eng = (r["engine"] or "?")[:34]
        print(f"{r['port']:<6} {c('1', eng):<54} {proc:<38} {models[:80]}")
        for n in r["notes"]:
            print(c("90", f"       {n}"))
    silent = len(rows) - len(found)
    if silent:
        print(f"\n({silent} probed port(s) answered nothing recognizable)")
    print("read-only: whatserved only GETs and reads /proc; it changes nothing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
