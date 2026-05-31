#!/usr/bin/env python3
"""Lightweight HTTP proxy that wraps `claude -p` for Docker containers.

Run on the host so containerized services can reach Claude Code CLI:

    python scripts/claude_proxy.py                  # 0.0.0.0:9100
    python scripts/claude_proxy.py --port 9200      # custom port
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class ProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            body = json.dumps({
                'status': 'ok',
                'claude_available': shutil.which('claude') is not None,
            })
            self._respond(200, body)
        elif self.path == '/ready':
            self._handle_ready()
        else:
            self._respond(404, json.dumps({'error': 'not found'}))

    def _handle_ready(self):
        # Exercises the same subprocess spawn mechanism /generate uses, so a
        # wedged subprocess layer (FD exhaustion, zombied node, broken CLI)
        # surfaces as an unhealthy container instead of silent request hangs.
        if not shutil.which('claude'):
            self._respond(503, json.dumps({'status': 'unready', 'error': 'claude CLI not found'}))
            return
        try:
            result = subprocess.run(
                ['claude', '--version'],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            self._respond(503, json.dumps({'status': 'unready', 'error': 'claude --version timed out'}))
            return
        except OSError as exc:
            self._respond(503, json.dumps({'status': 'unready', 'error': f'spawn failed: {exc}'}))
            return

        if result.returncode != 0:
            self._respond(503, json.dumps({
                'status': 'unready',
                'error': 'claude --version failed',
                'detail': result.stderr.strip()[:200],
                'returncode': result.returncode,
            }))
            return

        self._respond(200, json.dumps({'status': 'ready', 'version': result.stdout.strip()}))

    def do_POST(self):
        if self.path != '/generate':
            self._respond(404, json.dumps({'error': 'not found'}))
            return

        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self._respond(400, json.dumps({'error': 'invalid JSON'}))
            return

        prompt = data.get('prompt', '')
        system_prompt = data.get('system_prompt', '')
        model = data.get('model', 'sonnet')
        json_schema = data.get('json_schema')
        timeout = data.get('timeout', 300)

        if not prompt:
            self._respond(400, json.dumps({'error': 'missing "prompt" field'}))
            return

        if not shutil.which('claude'):
            self._respond(503, json.dumps({'error': 'claude CLI not found on host'}))
            return

        effort = data.get('effort', '')

        cmd = [
            'claude',
            '-p',
            '--no-session-persistence',
            '--model', model,
            '--output-format', 'json',
        ]

        if effort:
            cmd.extend(['--effort', effort])

        if system_prompt:
            cmd.extend(['--system-prompt', system_prompt])

        if json_schema:
            cmd.extend(['--json-schema', json.dumps(json_schema) if isinstance(json_schema, dict) else json_schema])

        start = time.time()
        try:
            env = {k: v for k, v in os.environ.items() if k != 'CLAUDECODE'}
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            self._respond(504, json.dumps({'error': 'claude CLI timed out', 'timeout': timeout}))
            return

        duration_ms = int((time.time() - start) * 1000)

        if result.returncode != 0:
            self._respond(502, json.dumps({
                'error': 'claude CLI failed',
                'detail': result.stderr.strip(),
                'returncode': result.returncode,
            }))
            return

        # Parse the JSON output envelope from --output-format json
        stdout = result.stdout.strip()
        envelope = {}
        try:
            envelope = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            pass

        if isinstance(envelope, dict) and envelope.get('is_error'):
            self._respond(502, json.dumps({
                'error': 'claude CLI returned error',
                'detail': envelope.get('result', ''),
            }))
            return

        # Prefer structured_output (pre-parsed from --json-schema), fall back to result string
        if isinstance(envelope, dict):
            ai_result = envelope.get('structured_output') or envelope.get('result', stdout)
        else:
            ai_result = stdout

        # Treat empty/blank responses as errors — callers must know something went wrong
        if not ai_result or (isinstance(ai_result, str) and not ai_result.strip()):
            self._respond(502, json.dumps({
                'error': 'claude CLI returned empty response',
                'detail': f'stdout={repr(result.stdout[:200])} stderr={repr(result.stderr[:200])}',
            }))
            return

        body = json.dumps({
            'result': ai_result,
            'model': model,
            'duration_ms': duration_ms,
            'cost_usd': envelope.get('total_cost_usd') if isinstance(envelope, dict) else None,
        })
        self._respond(200, body)

    def _respond(self, status: int, body: str):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, fmt, *args):
        print(f"[claude-proxy] {args[0]}" if args else fmt)


def _watchdog(port, interval=15, timeout=5, failure_threshold=3):
    # Converts "hang" (which restart: unless-stopped ignores) into "exit"
    # (which it restarts). Probes /ready via localhost — catches HTTP accept
    # freezes, thread-pool starvation, and wedged subprocess spawn all at once.
    url = f'http://127.0.0.1:{port}/ready'
    failures = 0
    # Let the server bind before first probe.
    time.sleep(interval)
    while True:
        ok = False
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                ok = resp.status == 200
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
            ok = False

        if ok:
            failures = 0
        else:
            failures += 1
            print(f"[claude-proxy] watchdog: /ready probe failed ({failures}/{failure_threshold})", flush=True)
            if failures >= failure_threshold:
                print("[claude-proxy] watchdog: persistent failure, exiting for container restart", flush=True)
                os._exit(1)
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description='Claude Code HTTP proxy for Docker containers')
    parser.add_argument('--host', default='0.0.0.0', help='Bind address (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=9100, help='Port (default: 9100)')
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    print(f"Claude proxy listening on {args.host}:{args.port}")
    print(f"Claude CLI available: {shutil.which('claude') is not None}")
    print("Endpoints: GET /health, GET /ready, POST /generate")

    threading.Thread(target=_watchdog, args=(args.port,), daemon=True, name='watchdog').start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == '__main__':
    main()
