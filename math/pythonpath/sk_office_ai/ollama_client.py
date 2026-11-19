# Copyright (C) 2026 Enrico Miot (EndrX)
# Licensed under GNU GPL v3 or later — see the LICENSE file or <https://www.gnu.org/licenses/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Minimal stdlib (http.client) REST client for a local Ollama server.

Ported from the sksuite convention (see skmail's core/ai/ollama_client.py):
no SDK dependency, just list_models()/chat() against the REST API, both
raising OllamaError on network/JSON/empty-reply failures.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import urllib.parse

DEFAULT_HOST = "http://localhost:11434"


class OllamaError(Exception):
    pass


class Cancelled(OllamaError):
    """The request was stopped by CancelToken.cancel()."""


class CancelToken:
    """Lets another thread abort an in-flight request. cancel() shuts the
    socket down: the blocked read in the worker fails at once, and Ollama,
    seeing the client disconnect, stops generating (its log shows "cancel
    task") instead of finishing a reply nobody will read."""

    def __init__(self):
        self.cancelled = False
        self._conn = None
        self._lock = threading.Lock()

    def attach(self, conn):
        with self._lock:
            if self.cancelled:
                raise Cancelled("cancelled")
            self._conn = conn

    def detach(self):
        with self._lock:
            self._conn = None

    def cancel(self):
        with self._lock:
            self.cancelled = True
            conn = self._conn
        if conn is not None and conn.sock is not None:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def check(self):
        if self.cancelled:
            raise Cancelled("cancelled")


def _request(host: str, path: str, payload: dict | None, timeout: float,
             cancel: CancelToken | None = None):
    url = host.rstrip("/") + path
    parts = urllib.parse.urlsplit(url)
    connection_class = (http.client.HTTPSConnection if parts.scheme == "https"
                        else http.client.HTTPConnection)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    conn = connection_class(parts.hostname, parts.port, timeout=timeout)
    try:
        if cancel is not None:
            cancel.attach(conn)
        conn.connect()
        conn.request("POST" if body is not None else "GET",
                     parts.path + (f"?{parts.query}" if parts.query else ""),
                     body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status >= 400:
            raise OllamaError(f"Ollama at {url} answered HTTP {resp.status}: "
                              f"{raw[:300].decode('utf-8', 'replace')}")
    except OllamaError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        if cancel is not None and cancel.cancelled:
            raise Cancelled("cancelled") from exc
        raise OllamaError(f"could not reach Ollama at {url}: {exc}") from exc
    finally:
        if cancel is not None:
            cancel.detach()
        conn.close()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OllamaError(f"invalid JSON from Ollama at {url}: {exc}") from exc


def list_models(host: str = DEFAULT_HOST, timeout: float = 10.0) -> list[dict]:
    """Return the raw list of model dicts from GET /api/tags.

    Each dict includes at least "name" and "details" (which in turn has
    "parameter_size", e.g. "7B", "0.5B", "70B") as reported by Ollama.
    """
    body = _request(host, "/api/tags", None, timeout)
    models = body.get("models")
    if not models:
        raise OllamaError(f"no models installed on Ollama at {host}")
    return models


def chat(
    host: str,
    model: str,
    messages: list[dict],
    timeout: float = 120.0,
    format: str | None = None,
    options: dict | None = None,
    cancel: CancelToken | None = None,
    allow_empty: bool = False,
) -> str:
    """POST /api/chat with stream: False, return the assistant's reply text.

    format="json" turns on Ollama's constrained JSON output, so the reply is
    guaranteed to be syntactically valid JSON (small models otherwise
    routinely answer in bare prose)."""
    payload = {"model": model, "messages": messages, "stream": False}
    if format:
        payload["format"] = format
    if options:
        payload["options"] = options
    body = _request(host, "/api/chat", payload, timeout, cancel)
    message = body.get("message") or {}
    content = message.get("content") or ""
    if not content and not allow_empty:
        raise OllamaError(f"empty reply from Ollama model {model!r}")
    return content


def chat_stream(
    host: str,
    model: str,
    messages: list[dict],
    on_delta,
    timeout: float | None = 120.0,
    options: dict | None = None,
    cancel: CancelToken | None = None,
) -> str:
    """POST /api/chat with stream: True. on_delta(text_so_far) is called (on
    the calling thread) after every chunk; returns the whole reply text,
    possibly empty. timeout applies to each read, so a long reply that
    keeps arriving never times out, but a stalled one does."""
    url = host.rstrip("/") + "/api/chat"
    parts = urllib.parse.urlsplit(url)
    connection_class = (http.client.HTTPSConnection if parts.scheme == "https"
                        else http.client.HTTPConnection)
    payload = {"model": model, "messages": messages, "stream": True}
    if options:
        payload["options"] = options
    conn = connection_class(parts.hostname, parts.port, timeout=timeout)
    text = ""
    try:
        if cancel is not None:
            cancel.attach(conn)
        conn.connect()
        conn.request("POST", parts.path, body=json.dumps(payload).encode("utf-8"),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        if resp.status >= 400:
            raw = resp.read()
            raise OllamaError(f"Ollama at {url} answered HTTP {resp.status}: "
                              f"{raw[:300].decode('utf-8', 'replace')}")
        while True:
            line = resp.readline()
            if not line:
                break
            if not line.strip():
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError as exc:
                raise OllamaError(f"invalid JSON from Ollama at {url}: {exc}") from exc
            if chunk.get("error"):
                raise OllamaError(f"Ollama error: {chunk['error']}")
            delta = (chunk.get("message") or {}).get("content") or ""
            if delta:
                text += delta
                on_delta(text)
            if chunk.get("done"):
                break
    except OllamaError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        if cancel is not None and cancel.cancelled:
            raise Cancelled("cancelled") from exc
        raise OllamaError(f"could not reach Ollama at {url}: {exc}") from exc
    finally:
        if cancel is not None:
            cancel.detach()
        conn.close()
    if cancel is not None:
        cancel.check()
    return text
