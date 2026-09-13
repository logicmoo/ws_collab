"""Isolated CDP target; no existing tabs, browser settings, or live APIs are changed."""
from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from urllib.request import urlopen

from captioner_counting_harness import ARTIFACTS, ROOT, TESTS, save_json, sha256


def analyze_native(manifest: dict, report: dict) -> list:
    script = """
const fs = require("node:fs");
const runner = require(process.argv[1]);
const input = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(runner.compareNative(input.manifest, input.report)));
"""
    result = subprocess.run(["node", "-e", script, str(TESTS / "captioner_counting_vad.js")],
                            input=json.dumps({"manifest": manifest, "report": report}),
                            text=True, capture_output=True, check=True, timeout=30)
    return json.loads(result.stdout)


class Cdp:
    def __init__(self, url: str):
        import websocket
        self.socket = websocket.create_connection(url, timeout=90, suppress_origin=True)
        self.counter = 0

    def call(self, method: str, params=None, session=None):
        self.counter += 1
        request = {"id": self.counter, "method": method, "params": params or {}}
        if session:
            request["sessionId"] = session
        self.socket.send(json.dumps(request))
        while True:
            message = json.loads(self.socket.recv())
            if message.get("id") == request["id"]:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    def evaluate(self, expression: str, session: str):
        result = self.call("Runtime.evaluate", {
            "expression": expression, "awaitPromise": True,
            "returnByValue": True, "userGesture": True,
        }, session)
        if "exceptionDetails" in result:
            raise RuntimeError(json.dumps(result["exceptionDetails"]))
        return result.get("result", {}).get("value")


def run_native(runtime: Path, transcript: Path, port: int, manifest_file: Path | None = None,
               artifact_dir: Path | None = None) -> dict:
    manifest_file = manifest_file or ARTIFACTS / "manifest.json"
    output_root = (artifact_dir or ARTIFACTS).resolve()
    if not output_root.is_relative_to(ROOT):
        raise ValueError("Native artifacts must stay inside the ws_collab repository")
    output_root.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    run_dir = output_root / f"native_{stamp}"
    run_dir.mkdir()
    shutil.copyfile(runtime, run_dir / "captioner_runtime.js")
    shutil.copyfile(transcript, run_dir / "transcript_runtime.js")
    shutil.copyfile(TESTS / "captioner_counting_native.js", run_dir / "harness.js")
    shutil.copyfile(manifest_file, run_dir / "source_manifest.json")
    html = b"""<!doctype html><meta charset="utf-8"><title>Isolated prerecorded audio test</title>
<script src="/captioner_runtime.js"></script><script src="/transcript_runtime.js"></script>
<script src="/native.js"></script><p>Prerecorded audio only. No microphone or conversation writes.</p>"""
    assets = {
        "/captioner_runtime.js": (run_dir / "captioner_runtime.js", "application/javascript"),
        "/transcript_runtime.js": (run_dir / "transcript_runtime.js", "application/javascript"),
        "/native.js": (run_dir / "harness.js", "application/javascript"),
    }
    prepared_cases = []
    for index, case in enumerate(manifest["cases"]):
        file = (manifest_file.parent / case["wav"]).resolve()
        if not file.is_relative_to(ROOT):
            raise ValueError("Native recordings must stay inside the ws_collab repository")
        route = f"/audio/{index}.wav"
        assets[route] = (file, "audio/wav")
        prepared_cases.append({**case, "audio_url": route, "source_kind": case.get("source_kind")
                               or manifest.get("source_kind") or (
                                   "synthetic_tts" if manifest_file.resolve() == (ARTIFACTS / "manifest.json").resolve()
                                   else "recorded_audio_unspecified")})
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append({"method": "GET", "path": self.path})
            if self.path == "/":
                payload, mime = html, "text/html"
            elif self.path in assets:
                file, mime = assets[self.path]
                payload = file.read_bytes()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            requests.append({"method": "POST", "path": self.path})
            self.send_error(405)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    test_origin = f"http://127.0.0.1:{server.server_port}"
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(), "test_origin": test_origin,
        "runtime_sha256": sha256(run_dir / "captioner_runtime.js"),
        "transcript_sha256": sha256(run_dir / "transcript_runtime.js"),
        "source_manifest": str(manifest_file),
        "source_manifest_sha256": sha256(manifest_file),
        "cases": [], "requests": requests, "cleanup": {},
        "note": "Only a newly created CDP browser context is used. Every ASR event and AudioWorklet RMS frame is retained. No invented word timestamps.",
    }
    cdp = None
    context = None
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=10) as response:
            version = json.load(response)
        report["browser"] = version["Browser"]
        cdp = Cdp(version["webSocketDebuggerUrl"])
        context = cdp.call("Target.createBrowserContext", {"disposeOnDetach": True})["browserContextId"]
        report["created_browser_context_id"] = context
        target = cdp.call("Target.createTarget", {"url": test_origin + "/", "browserContextId": context,
                                                 "background": True})["targetId"]
        report["created_target_id"] = target
        session = cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        cdp.call("Runtime.enable", session=session)
        deadline = time.monotonic() + 15
        while not cdp.evaluate("typeof window.runCountingCase === 'function'", session):
            if time.monotonic() > deadline:
                raise RuntimeError("Isolated page failed to load")
            time.sleep(0.2)
        for case in prepared_cases:
            print("Native prerecorded case:", case["id"], case["source_kind"], flush=True)
            result = cdp.evaluate("window.runCountingCase(" + json.dumps(case) + ")", session)
            report["cases"].append(result)
            save_json(run_dir / f"{case['id']}.json", result)
            print(result["outcome"], result.get("summary"), flush=True)
    except Exception as error:
        report["harness_error"] = str(error)
        print("Native harness unavailable:", error, flush=True)
    finally:
        if cdp and context:
            try:
                cdp.call("Target.disposeBrowserContext", {"browserContextId": context})
                report["cleanup"]["created_context_disposed"] = True
            except Exception as error:
                report["cleanup"]["error"] = str(error)
        if cdp:
            cdp.socket.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        report["cleanup"]["test_http_server_closed"] = True
        try:
            report["comparisons"] = analyze_native(manifest, report)
        except Exception as error:
            report["analysis_error"] = str(error)
        save_json(run_dir / "native_results.json", report)
        save_json(output_root / "native_latest.json", {"report": str(run_dir / "native_results.json")})
    return report
