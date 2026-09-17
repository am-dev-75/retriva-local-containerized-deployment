#!/usr/bin/env python3
"""Scriptable end-to-end CRM qualification test cycle.

Reproduces the manual WebUI flow:
  1. (implicit) create a NEW chat session  -> client-generated UUID4
  2. upload an Excel workbook attachment   -> POST /api/v2/sessions/{sid}/attachments
  3. trigger qualification:
       --chat-prompt : send the manual chat prompt (agent mode,
                       POST /gateway/chat) which makes the LLM invoke the
                       qualify_candidates tool; the script also calls
                       POST /api/v2/crm/qualify directly as a fallback if
                       the chat turn did not start a job.
       (default)     -> POST /api/v2/crm/qualify directly (identical to
                       the WebUI "Qualify candidates" button)
  4. poll GET /api/v2/crm/jobs/{job_id} until a terminal state
  5. download the qualification-report XLSX artifact to --out-dir

Requirements: Python 3.10+, only stdlib.  No auth headers needed while
RETRIVA_AUTH_PROVIDER=none.

Usage:
  python3 scripts/e2e_qualify_test.py \
      --workbook /tmp/user_leads_urls.xlsx \
      --kb cust_0007 \
      --chat-prompt "Qualify the companies in the attached spreadsheet." \
      --out-dir /tmp/e2e_reports [--mode chat] [--timeout 1800] [--keep-open]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from urllib import error, request as urlrequest

GATEWAY = "http://localhost:8202"
TERMINAL_PREFIXES = ("COMPLETED", "FAILED", "CANCELLED", "EXPIRED")
XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _req(method: str, url: str, *, data=None, headers=None, raw_body=None,
         timeout: float = 120.0):
    """Minimal JSON/HTTP helper returning (status, parsed_or_bytes)."""
    req = urlrequest.Request(url, method=method)
    body = raw_body
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urlrequest.urlopen(req, data=body, timeout=timeout) as resp:
            payload = resp.read()
            ctype = resp.headers.get_content_type()
            if ctype == "application/json":
                try:
                    return resp.status, json.loads(payload)
                except json.JSONDecodeError:
                    return resp.status, payload
            return resp.status, payload
    except error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload)
        except Exception:
            return exc.code, payload


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def upload_workbook(session_id: str, workbook: str) -> str:
    """Multipart upload; returns attachment_id."""
    boundary = f"----retre2e{uuid.uuid4().hex}"
    path = Path(workbook)
    raw = path.read_bytes()
    fname = path.name
    pre = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; '
        f'filename="{fname}"\r\n'
        f"Content-Type: application/vnd.openxmlformats-officedocument."
        f"spreadsheetml.sheet\r\n\r\n"
    ).encode("utf-8")
    post = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = pre + raw + post
    req = urlrequest.Request(
        f"{GATEWAY}/api/v2/sessions/{session_id}/attachments",
        method="POST",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urlrequest.urlopen(req, timeout=300) as resp:
        payload = json.loads(resp.read())
    att = payload.get("attachment_id")
    if not att:
        raise RuntimeError(f"upload response missing attachment_id: {payload}")
    log(f"uploaded: attachment_id={att} size={payload.get('size')} "
        f"file={payload.get('original_filename')}")
    return att


def trigger_via_chat(session_id: str, attachment_id: str, kb_id: str,
                     prompt: str) -> str | None:
    """Send the manual chat prompt in agent mode; returns job_id or None.

    The gateway agent loop invokes the qualify_candidates tool with the
    trusted session context; its reply exposes tool name/args/ok but not
    the tool result payload, so job discovery is done via the
    session-jobs listing endpoint below.
    """
    body = json.dumps({
        "message": prompt,
        "kb_ids": [kb_id],
        "metadata_filter_mode": "soft",
        "stream": False,
        "session_id": session_id,
        "tools_enabled": True,
        "attachment_ids": [attachment_id],
    }).encode("utf-8")
    req = urlrequest.Request(
        f"{GATEWAY}/gateway/chat", method="POST",
        data=body, headers={"Content-Type": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=300) as resp:
            reply = json.loads(resp.read())
    except error.HTTPError as exc:
        log(f"WARN chat turn failed (HTTP {exc.code}): {exc.read()[:200]}")
        return None
    tool_calls = (reply.get("agent") or {}).get("tool_calls") or []
    tools = [tc.get("tool") for tc in tool_calls]
    log(f"chat agent reply: stopped_reason="
        f"{(reply.get('agent') or {}).get('stopped_reason')!r}, "
        f"tools={tools}")
    if "qualify_candidates" not in tools:
        return None
    # The LLM started (or attempted) the job: find it via the
    # session jobs listing (trusted association).
    return find_session_job(session_id, attachment_id)


def find_session_job(session_id: str, attachment_id: str) -> str | None:
    """Find the (newest) qualification job for session+attachment."""
    req = urlrequest.Request(
        f"{GATEWAY}/api/v2/crm/sessions/{session_id}/jobs")
    try:
        with urlrequest.urlopen(req, timeout=30) as resp:
            listing = resp.read()
        jobs = json.loads(listing)
    except (error.HTTPError, json.JSONDecodeError):
        return None
    if not isinstance(jobs, list):
        jobs = jobs.get("jobs", [])
    cands = [j for j in jobs
             if j.get("attachment_id") == attachment_id]
    if not cands:
        return None
    # Prefer a non-terminal job; else the newest.
    active = [j for j in cands
              if not str(j.get("state", "")).startswith(
                  TERMINAL_PREFIXES)]
    pick = (active or cands)[-1]
    return pick.get("job_id")


def start_direct(session_id: str, attachment_id: str, kb_id: str,
                 retry_of: str | None) -> str:
    body: dict = {
        "session_id": session_id,
        "attachment_id": attachment_id,
        "kb_id": kb_id,
        "async_job": True,
    }
    if retry_of:
        body["retry_of_job_id"] = retry_of
    req = urlrequest.Request(
        f"{GATEWAY}/api/v2/crm/qualify", method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urlrequest.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read())
    log(f"qualify: status={payload.get('status')} job_id="
        f"{payload.get('job_id')}")
    return payload["job_id"]


def poll_job(job_id: str, timeout_s: float,
             poll_interval: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout_s
    last_state = None
    while time.monotonic() < deadline:
        req = urlrequest.Request(f"{GATEWAY}/api/v2/crm/jobs/{job_id}")
        try:
            with urlrequest.urlopen(req, timeout=30) as resp:
                job = json.loads(resp.read())
        except error.HTTPError as exc:
            if exc.code == 404:
                raise RuntimeError(
                    "job not found (process-local registry lost?) — "
                    "fail fast instead of hanging") from exc
            raise
        state = job.get("state", "")
        if state != last_state:
            log(f"job {job_id}: state={state} progress={job.get('progress')} "
                f"stage={job.get('stage_detail')}")
            last_state = state
        if state.startswith(TERMINAL_PREFIXES):
            return job
        time.sleep(poll_interval)
    raise TimeoutError(f"job {job_id} did not finish within {timeout_s}s")


def download_artifacts(session_id: str, job: dict, out_dir: str) -> list:
    """Download the report artifacts; returns local paths."""
    downloaded: list = []
    artifact_ids = job.get("artifact_ids") or []
    if not artifact_ids:
        # Fall back to the results endpoint (may 409/410).
        req = urlrequest.Request(
            f"{GATEWAY}/api/v2/crm/jobs/{job['job_id']}/results")
        try:
            with urlrequest.urlopen(req, timeout=60) as resp:
                results = json.loads(resp.read())
            artifact_ids = [
                a["artifact_id"] for a in results.get("artifacts", [])]
        except error.HTTPError as exc:
            log(f"WARN: results endpoint returned {exc.code}; trying "
                f"session artifacts listing")
            with urlrequest.urlopen(urlrequest.Request(
                    f"{GATEWAY}/api/v2/sessions/{session_id}/artifacts"),
                    timeout=60) as resp:
                listing = json.loads(resp.read())
            items = listing if isinstance(listing, list) \
                else listing.get("artifacts", [])
            artifact_ids = [a.get("artifact_id") for a in items
                            if a.get("artifact_kind") ==
                            "qualification_report"]
    for aid in artifact_ids:
        fname = aid
        media_type = ""
        try:
            with urlrequest.urlopen(urlrequest.Request(
                    f"{GATEWAY}/api/v2/sessions/{session_id}/artifacts/"
                    f"{aid}"), timeout=30) as resp:
                meta = json.loads(resp.read())
            fname = meta.get("filename") or aid
            media_type = meta.get("media_type") or ""
        except error.HTTPError:
            pass
        if not str(fname).endswith((".xlsx", ".md")):
            fname = f"{aid}.bin"
        with urlrequest.urlopen(urlrequest.Request(
                f"{GATEWAY}/api/v2/sessions/{session_id}/artifacts/"
                f"{aid}/content"), timeout=300) as resp:
            data = resp.read()
        local = Path(out_dir) / fname
        local.write_bytes(data)
        log(f"downloaded artifact {aid} -> {local} ({len(data)} bytes, "
            f"type={media_type or 'unknown'})")
        downloaded.append(str(local))
    return downloaded


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workbook", required=True,
                    help="path to the Excel candidate workbook")
    ap.add_argument("--kb", default="cust_0007", help="knowledge base id")
    ap.add_argument("--gateway", default=GATEWAY)
    ap.add_argument("--chat-prompt",
                    default="Qualify the companies in the attached "
                            "spreadsheet.",
                    help="chat prompt for --mode chat")
    ap.add_argument("--mode", choices=("direct", "chat"), default="direct",
                    help="direct: POST /api/v2/crm/qualify; chat: send the "
                         "prompt through POST /gateway/chat (agent mode), "
                         "falling back to direct")
    ap.add_argument("--retry-of", default=None,
                    help="optional retry_of_job_id audit lineage")
    ap.add_argument("--timeout", type=float, default=1500.0,
                    help="job completion timeout in seconds")
    ap.add_argument("--out", default="/tmp/retriva_e2e",
                    help="output directory for downloaded artifacts")
    ap.add_argument("--keep-open", action="store_true",
                    help="do not reuse/affect other sessions (informational)")
    args = ap.parse_args()

    session_id = str(uuid.uuid4())   # NEW chat session, client-generated
    # The WebUI generates the session id client-side; the first upload
    # creates it server-side.  Same behavior here.
    log(f"new session: {session_id}")

    att = upload_workbook(session_id, args.workbook)

    job_id = None
    if args.mode == "chat":
        job_id = trigger_via_chat(session_id, att, args.kb,
                                  args.chat_prompt)
        if not job_id:
            log("chat turn did not start a job; falling back to direct "
                "qualify")
    if not job_id:
        job_id = start_direct(session_id, att, args.kb, args.retry_of)
    else:
        log(f"job from chat trigger: {job_id}")

    job = poll_job(job_id, args.timeout)
    verdict = job.get("state")
    log(f"final state: {verdict} results={job.get('result_count')} "
        f"warnings={len(job.get('warnings') or [])}")
    if job.get("error"):
        log(f"job error: {job['error'][:200]}")

    out_dir = Path(args.out) / time.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "job_status.json").write_text(json.dumps(job, indent=2))
    files = download_artifacts(session_id, job, str(out_dir))
    if verdict.startswith("COMPLETED") and files:
        log(f"SUCCESS — {verdict}; artifacts: {', '.join(files)}")
        return 0
    log(f"FAILED — final state {verdict}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
