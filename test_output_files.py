"""
End-to-end test: starts the server, submits a job, polls to completion,
checks that result.json and result.md are written to outputs/{job_id}/.
Run from the integrated apps/V2/ directory:
    python test_output_files.py
"""
import os
import sys
import time
import subprocess
import json
import urllib.request
import urllib.error
from pathlib import Path

PORT = 8099
BASE_URL = f"http://localhost:{PORT}"
PDF = Path("uploads/34efc775-bf9c-4145-baad-ed3ce4ed18b2/RFP-2026-IT-001-Overview.pdf")
LOG = Path("test_output_files.log")

def log(msg):
    print(msg, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

def http_get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())

def http_post_file(url, filepath):
    """POST multipart/form-data with a single file field 'files'."""
    boundary = "BoundaryXYZ1234567890"
    filename = filepath.name
    file_bytes = filepath.read_bytes()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
        f"Content-Type: application/pdf\r\n"
        f"\r\n"
    ).encode() + file_bytes + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def wait_for_server(max_wait=20):
    for _ in range(max_wait):
        try:
            http_get(f"{BASE_URL}/health", timeout=2)
            return True
        except Exception:
            time.sleep(1)
    return False

def main():
    LOG.write_text("", encoding="utf-8")  # reset log
    if not PDF.exists():
        log(f"ERROR: test PDF not found: {PDF}")
        sys.exit(1)

    log(f"[1] Starting server on port {PORT}...")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    if not wait_for_server():
        log("ERROR: server did not start in time")
        server.terminate()
        sys.exit(1)
    log("    Server is up.")

    try:
        log(f"[2] Submitting job with: {PDF.name}")
        resp = http_post_file(f"{BASE_URL}/analyze", PDF)
        job_id = resp["job_id"]
        log(f"    job_id = {job_id}")

        log("[3] Polling for completion (max 10 min)...")
        deadline = time.time() + 600
        status = "pending"
        while time.time() < deadline and status not in ("complete", "failed"):
            time.sleep(20)
            try:
                j = http_get(f"{BASE_URL}/jobs/{job_id}")
                status = j["status"]
                log(f"    status = {status}")
            except Exception as e:
                log(f"    poll error: {e}")

        log(f"[4] Final status: {status}")

        if status == "complete":
            out_dir = Path("outputs") / job_id
            log(f"[5] Checking output dir: {out_dir.resolve()}")
            if out_dir.exists():
                for f in out_dir.iterdir():
                    log(f"    FOUND: {f.name}  ({f.stat().st_size} bytes)")
                    if f.name == "result.md":
                        log("\n--- First 40 lines of result.md ---")
                        lines = f.read_text(encoding="utf-8").splitlines()
                        for line in lines[:40]:
                            log(line)
                        log("---")
            else:
                log("    ERROR: output directory was NOT created!")
        elif status == "failed":
            log("    Pipeline failed — check API credentials in .env")
        else:
            log("    Timed out waiting for completion")

    finally:
        server.terminate()
        out, _ = server.communicate(timeout=5)
        last_logs = (out or b"").decode(errors="replace").strip().splitlines()[-30:]
        log("\n--- Last server logs ---")
        for line in last_logs:
            log(line)

if __name__ == "__main__":
    main()
