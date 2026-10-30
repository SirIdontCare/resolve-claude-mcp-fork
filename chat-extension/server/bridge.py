#!/usr/bin/env python
"""
stdio -> HTTP bridge for the Resolve MCP server.

The Claude desktop app launches extensions over stdio, but on the free edition
of DaVinci Resolve the server has to live inside Resolve and speak HTTP (see
the repository's README). This script sits between the two: it reads JSON-RPC
messages from stdin, forwards them to the HTTP endpoint, and writes whatever
comes back to stdout.

Standard library only, so the extension needs no bundled dependencies.
"""

import json
import os
import sys
import threading
import urllib.error
import urllib.request

URL = os.environ.get("RESOLVE_MCP_URL", "http://127.0.0.1:8765/mcp")
TIMEOUT = float(os.environ.get("RESOLVE_MCP_TIMEOUT", "300"))

# MCP stdio is UTF-8 and newline-framed. Windows would otherwise give us the
# console codepage, which cannot encode the em dashes in the tool descriptions,
# and CRLF line endings.
for _stream, _kw in ((sys.stdin, {}), (sys.stdout, {"newline": "\n"})):
    try:
        _stream.reconfigure(encoding="utf-8", **_kw)
    except Exception:
        pass

_out_lock = threading.Lock()
_protocol_version = None

HINT = ("Cannot reach the Resolve MCP server at %s. In DaVinci Resolve, open a "
        "project and run Workspace > Scripts > MCP Server, then reload this "
        "extension." % URL)


def emit(obj):
    """Write one JSON-RPC message to stdout, newline-delimited."""
    data = json.dumps(obj, ensure_ascii=False)
    with _out_lock:
        sys.stdout.write(data + "\n")
        sys.stdout.flush()


def log(msg):
    sys.stderr.write("[resolve-bridge] %s\n" % msg)
    sys.stderr.flush()


def parse_sse(body):
    """Pull the JSON payloads out of an event-stream response."""
    out = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        payload = "".join(
            line[5:].lstrip() if line.startswith("data:") else ""
            for line in block.split("\n")
            if line.startswith("data:")
        )
        if payload.strip():
            out.append(payload)
    return out


def forward(message):
    """POST one message upstream and emit any responses."""
    global _protocol_version

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if _protocol_version:
        headers["MCP-Protocol-Version"] = _protocol_version

    req = urllib.request.Request(
        URL, data=json.dumps(message).encode("utf-8"),
        headers=headers, method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            ctype = (resp.headers.get("Content-Type") or "").lower()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        fail(message, -32000, "HTTP %s from the Resolve MCP server: %s"
                              % (e.code, body or HINT))
        return
    except Exception as e:
        fail(message, -32000, "%s (%s: %s)" % (HINT, type(e).__name__, e))
        return

    if not raw.strip():
        return  # notification accepted, nothing to relay

    payloads = parse_sse(raw) if "text/event-stream" in ctype else [raw]
    for payload in payloads:
        try:
            parsed = json.loads(payload)
        except ValueError:
            log("could not parse upstream payload: %r" % payload[:200])
            continue
        # Remember the negotiated version so later requests carry the header.
        if isinstance(parsed, dict):
            result = parsed.get("result")
            if isinstance(result, dict) and result.get("protocolVersion"):
                _protocol_version = result["protocolVersion"]
        emit(parsed)


def fail(message, code, text):
    """Answer a request with a JSON-RPC error; stay quiet for notifications."""
    log(text)
    if isinstance(message, dict) and message.get("id") is not None:
        emit({"jsonrpc": "2.0", "id": message["id"],
              "error": {"code": code, "message": text}})


def main():
    log("bridging stdio to %s" % URL)
    threads = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            log("ignoring non-JSON line: %r" % line[:200])
            continue
        # One thread per message: a slow render tool must not block the
        # next request, and the client is free to pipeline.
        t = threading.Thread(target=forward, args=(message,), daemon=True)
        t.start()
        threads.append(t)
        threads = [t for t in threads if t.is_alive()]
    log("stdin closed, exiting")


if __name__ == "__main__":
    main()
