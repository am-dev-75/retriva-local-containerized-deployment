#!/usr/bin/env python3
"""E2E qualification harness — audited & hardened version.

Deterministic, evidence-preserving end-to-end cycle:
  new chat session -> workbook upload (session-scoped, never ingested)
  -> trigger qualification (direct API or strict chat agent tool)
  -> safe polling -> artifact download -> REAL validation -> acceptance.

Design invariants (proven by the deterministic test suite):
  * The workbook is a SESSION-SCOPED attachment. It is never ingested into
    any knowledge base (Qdrant/GraphRAG) and never becomes a candidate
    source outside this run's job.
  * The global ACP and authoritative global CCO are resolved globally
    (unique ACTIVE records) and are NEVER selected by any KB identifier.
    An optional context KB provides conversational/retrieval context ONLY.
  * Chat mode is STRICT by default: if the agent does not invoke the
    official `qualify_candidates` tool, the run fails with
    CHAT_TOOL_NOT_TRIGGERED. Fallback to direct mode is opt-in and always
    reported distinctly (CHAT_FAILED_DIRECT_FALLBACK_SUCCEEDED).

Exit-code contract (stable, documented, tested):
  0 PASS / PASS_WITH_WARNINGS (acceptance criteria met)
  1 QUALIFICATION_INCOMPLETE (job completed but acceptance failed)
  2 CHAT_TOOL_NOT_TRIGGERED (strict chat, agent never called the tool)
  3 JOB_FAILED / JOB_CANCELLED / JOB_EXPIRED / JOB_LOST
  4 TIMEOUT
  5 ARTIFACT_INVALID
  6 CONFIGURATION_ERROR (bad args / config)
  7 BINDING_MISMATCH (session/attachment/ACP/CCO binding violation)
  8 INFRASTRUCTURE_ERROR (transport-level failure)
  9 CHAT_FAILED_DIRECT_FALLBACK_SUCCEEDED
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import time
import traceback
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Outcome vocabulary & exit-code contract
# ---------------------------------------------------------------------------

OUTCOME_PASS = "PASS"
OUTCOME_PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
OUTCOME_QUALIFICATION_INCOMPLETE = "QUALIFICATION_INCOMPLETE"
OUTCOME_REGRESSION_FAILED = "REGRESSION_FAILED"
OUTCOME_CHAT_TOOL_NOT_TRIGGERED = "CHAT_TOOL_NOT_TRIGGERED"
OUTCOME_CHAT_FAILED_DIRECT_FALLBACK_SUCCEEDED = "CHAT_FAILED_DIRECT_FALLBACK_SUCCEEDED"
OUTCOME_JOB_FAILED = "JOB_FAILED"
OUTCOME_JOB_CANCELLED = "JOB_CANCELLED"
OUTCOME_JOB_EXPIRED = "JOB_EXPIRED"
OUTCOME_JOB_LOST = "JOB_LOST"
OUTCOME_TIMEOUT = "TIMEOUT"
OUTCOME_ARTIFACT_INVALID = "ARTIFACT_INVALID"
OUTCOME_BINDING_MISMATCH = "BINDING_MISMATCH"
OUTCOME_CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
OUTCOME_INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"

EXIT_CODES = {
    OUTCOME_PASS: 0,
    OUTCOME_PASS_WITH_WARNINGS: 0,
    OUTCOME_QUALIFICATION_INCOMPLETE: 1,
    OUTCOME_REGRESSION_FAILED: 1,
    OUTCOME_CHAT_TOOL_NOT_TRIGGERED: 2,
    OUTCOME_JOB_FAILED: 3,
    OUTCOME_JOB_CANCELLED: 3,
    OUTCOME_JOB_EXPIRED: 3,
    OUTCOME_JOB_LOST: 3,
    OUTCOME_TIMEOUT: 4,
    OUTCOME_ARTIFACT_INVALID: 5,
    OUTCOME_CONFIGURATION_ERROR: 6,
    OUTCOME_BINDING_MISMATCH: 7,
    OUTCOME_INFRASTRUCTURE_ERROR: 8,
    OUTCOME_CHAT_FAILED_DIRECT_FALLBACK_SUCCEEDED: 9,
}

TERMINAL_STATES = {
    "COMPLETED",
    "COMPLETED_WITH_WARNINGS",
    "FAILED",
    "CANCELLED",
    "EXPIRED",
}
COMPLETED_STATES = {"COMPLETED", "COMPLETED_WITH_WARNINGS"}

# Worksheets that MUST exist in a valid qualification XLSX (report.py).
REQUIRED_XLSX_SHEETS = ("Qualification", "Average Customer Profile")
RECONCILIATION_SHEET = "Source Record Reconciliation"

DEFAULT_OUT = "./artifacts/e2e-runs"
DEFAULT_TIMEOUT_S = 1500
POLL_INTERVAL_S = 5.0
MAX_CONSECUTIVE_POLL_ERRORS = 3

QUALIFY_TOOL_NAME = "qualify_candidates"


# ---------------------------------------------------------------------------
# Logging (console + durable execution.log)
# ---------------------------------------------------------------------------

class RunLogger:
    def __init__(self, log_path: Optional[Path] = None) -> None:
        self._fh = open(log_path, "a", encoding="utf-8") if log_path else None

    def log(self, msg: str) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"[{stamp}] {msg}"
        print(line, flush=True)
        if self._fh:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()


# ---------------------------------------------------------------------------
# HTTP helper (stdlib only; injectable for tests)
# ---------------------------------------------------------------------------

class HttpError(Exception):
    def __init__(self, status: int, body: bytes, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:200]!r}")
        self.status = status
        self.body = body
        self.url = url


class Transport:
    """Thin HTTP client. Tests inject a fake via http_fn."""

    def __init__(self, http_fn: Optional[Callable[..., Any]] = None) -> None:
        # http_fn(method, url, body_bytes|None, headers) -> (status, bytes)
        self._fn = http_fn or self._stdlib_http

    @staticmethod
    def _stdlib_http(method: str, url: str, body: Optional[bytes], headers: dict) -> tuple:
        req = Request(url, data=body, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urlopen(req, timeout=60) as resp:
                return resp.status, resp.read()
        except HTTPError as e:
            return e.code, e.read()

    def request(self, method: str, url: str, body: Optional[bytes] = None,
                headers: Optional[dict] = None) -> tuple:
        return self._fn(method, url, body, headers or {})

    def get_json(self, url: str) -> tuple:
        status, body = self.request("GET", url)
        if status != 200:
            raise HttpError(status, body, url)
        return status, json.loads(body.decode("utf-8"))

    def post_json(self, url: str, payload: dict, headers: Optional[dict] = None) -> tuple:
        body = json.dumps(payload).encode("utf-8")
        return self.request("POST", url, body,
                            headers or {"Content-Type": "application/json"})


# ---------------------------------------------------------------------------
# Evidence directory
# ---------------------------------------------------------------------------

class EvidenceDir:
    """Immutable durable run directory with the canonical evidence files."""

    FILES = [
        "run_manifest.json", "input_workbook.xlsx", "input_workbook.sha256",
        "prompt.txt", "prompt.sha256", "chat_request.json", "chat_response.json",
        "tool_call.json", "upload_response.json", "job_status.json",
        "results.json", "qualification_report.md", "qualification_report.xlsx",
        "artifact_hashes.json", "acceptance_result.json", "execution.log",
    ]

    def __init__(self, root: Path, run_id: str) -> None:
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = root / f"{ts}-{run_id}"
        self.path.mkdir(parents=True, exist_ok=False)
        (self.path / "execution.log").touch()

    def write(self, name: str, data: bytes) -> Path:
        p = self.path / name
        if p.exists():
            raise RuntimeError(f"evidence file already exists: {name}")
        p.write_bytes(data)
        os.chmod(p, 0o444)  # immutable evidence
        return p

    def write_json(self, name: str, obj: Any) -> Path:
        return self.write(name, json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Upload (session-scoped attachment; NEVER ingested)
# ---------------------------------------------------------------------------

XLSX_MIME = ("application/vnd.openxmlformats-officedocument."
             "spreadsheetml.sheet")


def guess_mime(filename: str) -> str:
    """MIME for the multipart file part. The server uses this value for
    parser selection (file.content_type), so it MUST be the real type —
    application/octet-stream would route .xlsx to the plain-text parser
    and yield zero candidates."""
    ext = Path(filename).suffix.lower()
    return {
        ".xlsx": XLSX_MIME,
        ".csv": "text/csv",
        ".pdf": "application/pdf",
        ".md": "text/markdown",
        ".txt": "text/plain",
    }.get(ext, "application/octet-stream")


def upload_workbook(t: Transport, gateway: str, session_id: str,
                    workbook: Path, logger: RunLogger) -> dict:
    boundary = f"----e2eBoundary{uuid.uuid4().hex}"
    wb_bytes = workbook.read_bytes()
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{workbook.name}"\r\n'.encode(),
        f"Content-Type: {guess_mime(workbook.name)}\r\n\r\n".encode(),
        wb_bytes, b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    url = f"{gateway}/api/v2/sessions/{session_id}/attachments"
    status, resp = t.request("POST", url, body, {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    if status != 201:
        raise HttpError(status, resp, url)
    return json.loads(resp.decode("utf-8"))


# ---------------------------------------------------------------------------
# Trigger: direct
# ---------------------------------------------------------------------------

def start_direct(t: Transport, gateway: str, session_id: str, attachment_id: str,
                 context_kb: Optional[str], retry_of: Optional[str],
                 logger: RunLogger) -> dict:
    payload: dict = {
        "session_id": session_id,
        "attachment_id": attachment_id,
        "async_job": True,
    }
    if context_kb:
        # Conversational/retrieval context ONLY. Never selects ACP/CCO,
        # never ingests the workbook, never scopes qualification.
        payload["kb_id"] = context_kb
    if retry_of:
        payload["retry_of_job_id"] = retry_of
    url = f"{gateway}/api/v2/crm/qualify"
    status, resp = t.post_json(url, payload)
    if status not in (200, 202):
        raise HttpError(status, resp, url)
    return json.loads(resp.decode("utf-8"))


# ---------------------------------------------------------------------------
# Trigger: strict chat
# ---------------------------------------------------------------------------

def build_chat_request(session_id: str, prompt: str, attachment_ids: list,
                       context_kb: Optional[str]) -> dict:
    req: dict = {
        "message": prompt,
        "metadata_filter_mode": "soft",
        "stream": False,
        "session_id": session_id,
        "tools_enabled": True,
        "attachment_ids": attachment_ids,
    }
    # kb_ids omitted entirely when no context KB: the KB is NOT a
    # qualification parameter. ACP/CCO are global and KB-independent.
    if context_kb:
        req["kb_ids"] = [context_kb]
    return req


def trigger_via_chat(t: Transport, gateway: str, chat_request: dict,
                     logger: RunLogger) -> tuple:
    """Returns (reply_dict, tool_call_dict_or_None)."""
    url = f"{gateway}/gateway/chat"
    status, resp = t.post_json(url, chat_request)
    if status != 200:
        raise HttpError(status, resp, url)
    reply = json.loads(resp.decode("utf-8"))
    agent = reply.get("agent") or {}
    for tc in agent.get("tool_calls") or []:
        if tc.get("tool") == QUALIFY_TOOL_NAME:
            return reply, tc
    return reply, None


def find_session_job(t: Transport, gateway: str, session_id: str,
                     attachment_id: str, logger: RunLogger) -> Optional[dict]:
    """Discover the job the agent created (tool_calls carry no result payload)."""
    url = f"{gateway}/api/v2/crm/sessions/{session_id}/jobs"
    _, data = t.get_json(url)
    jobs = data.get("jobs") or []
    matching = [j for j in jobs if j.get("attachment_id") == attachment_id]
    if not matching:
        return None
    non_terminal = [j for j in matching if j.get("state") not in TERMINAL_STATES]
    pool = non_terminal or matching
    return max(pool, key=lambda j: j.get("created_at") or "")


# ---------------------------------------------------------------------------
# Polling (safe: bounded transient errors, JOB_LOST, monotonic elapsed)
# ---------------------------------------------------------------------------

def poll_job(t: Transport, gateway: str, job_id: str, timeout_s: float,
             logger: RunLogger,
             interval_s: Optional[float] = None,
             max_consecutive_errors: int = MAX_CONSECUTIVE_POLL_ERRORS) -> dict:
    """Poll until terminal state. Returns the last job dict.

    Raises TimeoutError on timeout; returns a {"state": "JOB_LOST"} dict
    on 404-after-known or too many consecutive transient errors.
    """
    if interval_s is None:
        interval_s = POLL_INTERVAL_S
    deadline = time.monotonic() + timeout_s
    consecutive_errors = 0
    last: Optional[dict] = None
    url = f"{gateway}/api/v2/crm/jobs/{job_id}"
    while time.monotonic() < deadline:
        try:
            _, job = t.get_json(url)
            consecutive_errors = 0
            last = job
            state = job.get("state")
            if state in TERMINAL_STATES:
                return job
            logger.log(f"poll: state={state} progress={job.get('progress')}")
        except HttpError as e:
            if e.status == 404:
                # Registry is process-local: 404 after a known job == lost.
                logger.log(f"poll: 404 — job lost (last state: "
                           f"{(last or {}).get('state', 'never-seen')})")
                lost = dict(last or {})
                lost["state"] = "JOB_LOST"
                lost["job_id"] = job_id
                return lost
            consecutive_errors += 1
            logger.log(f"poll: transient HTTP {e.status} "
                       f"({consecutive_errors}/{max_consecutive_errors})")
            if consecutive_errors >= max_consecutive_errors:
                lost = dict(last or {})
                lost["state"] = "JOB_LOST"
                lost["job_id"] = job_id
                lost["poll_error"] = f"HTTP {e.status}"
                return lost
        except (URLError, ConnectionError, json.JSONDecodeError) as e:
            consecutive_errors += 1
            logger.log(f"poll: transient error {e!r} "
                       f"({consecutive_errors}/{max_consecutive_errors})")
            if consecutive_errors >= max_consecutive_errors:
                lost = dict(last or {})
                lost["state"] = "JOB_LOST"
                lost["job_id"] = job_id
                lost["poll_error"] = repr(e)
                return lost
        time.sleep(interval_s)
    raise TimeoutError(f"polling exceeded {timeout_s}s; last state: "
                       f"{(last or {}).get('state', 'never-seen')}")


# ---------------------------------------------------------------------------
# Results & artifacts
# ---------------------------------------------------------------------------

def fetch_results(t: Transport, gateway: str, job_id: str) -> dict:
    url = f"{gateway}/api/v2/crm/jobs/{job_id}/results"
    status, body = t.request("GET", url)
    if status != 200:
        raise HttpError(status, body, url)
    return json.loads(body.decode("utf-8"))


def download_artifacts(t: Transport, gateway: str, session_id: str,
                       job: dict, logger: RunLogger) -> list:
    """Download raw bytes for each artifact. Returns [{name, media_type, bytes}]."""
    out = []
    for aid in job.get("artifact_ids") or []:
        meta_url = f"{gateway}/api/v2/sessions/{session_id}/artifacts/{aid}"
        _, meta = t.get_json(meta_url)
        content_url = f"{meta_url}/content"
        status, content = t.request("GET", content_url)
        if status != 200:
            raise HttpError(status, content, content_url)
        out.append({
            "artifact_id": aid,
            "filename": meta.get("filename") or f"artifact_{aid}",
            "media_type": meta.get("media_type") or "application/octet-stream",
            "bytes": content,
        })
    return out


# ---------------------------------------------------------------------------
# REAL artifact validation (not HTTP 200)
# ---------------------------------------------------------------------------

def validate_xlsx(data: bytes, results: dict) -> list:
    """Structural XLSX validation. Returns list of problems (empty == valid)."""
    problems = []
    if len(data) < 4 or data[:4] != b"PK\x03\x04":
        problems.append("XLSX missing ZIP signature (PK\\x03\\x04) — "
                        "content is not a valid workbook (HTML error page?)")
        return problems
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            if "xl/workbook.xml" not in names:
                problems.append("XLSX missing xl/workbook.xml")
                return problems
            wb_xml = zf.read("xl/workbook.xml").decode("utf-8", errors="replace")
            sheets = set(re.findall(r'<sheet[^>]*name="([^"]+)"', wb_xml))
            for required in REQUIRED_XLSX_SHEETS:
                if required not in sheets:
                    problems.append(f"XLSX missing required worksheet: {required}")
            if RECONCILIATION_SHEET not in sheets:
                problems.append(f"XLSX missing worksheet: {RECONCILIATION_SHEET}")
            else:
                rec = (results.get("source_reconciliation") or {})
                reported = rec.get("reported_records")
                if not isinstance(reported, int) or reported < 0:
                    problems.append("results payload missing valid "
                                    "source_reconciliation.reported_records")
    except zipfile.BadZipFile:
        problems.append("XLSX bytes are not a valid ZIP archive")
    return problems


def validate_markdown(data: bytes, results: dict) -> list:
    problems = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        problems.append("Markdown report is not valid UTF-8")
        return problems
    stripped = text.strip()
    if not stripped:
        problems.append("Markdown report is empty")
        return problems
    if stripped[:1] == "<" and "<html" in stripped[:2000].lower():
        problems.append("Markdown report looks like an HTML error page")
        return problems
    if not re.search(r"^# .+", text, re.MULTILINE):
        problems.append("Markdown report has no H1 heading")
    n_results = len(results.get("results") or [])
    if n_results and not re.search(r"^## .+", text, re.MULTILINE):
        problems.append("Markdown report has no H2 sections despite "
                        f"{n_results} results")
    return problems


# ---------------------------------------------------------------------------
# Binding verification
# ---------------------------------------------------------------------------

def verify_bindings(run: dict, job: dict, results: dict) -> list:
    """Session/attachment/ACP/CCO binding checks. Returns problems list."""
    problems = []
    if job.get("session_id") not in (None, run["session_id"]):
        problems.append(f"job.session_id={job.get('session_id')!r} != "
                        f"run session {run['session_id']!r}")
    if job.get("attachment_id") not in (None, run["attachment_id"]):
        problems.append(f"job.attachment_id={job.get('attachment_id')!r} != "
                        f"run attachment {run['attachment_id']!r}")
    if not job.get("acp_id") or job.get("acp_version") is None:
        problems.append("job missing global ACP binding (acp_id/acp_version)")
    # CCO binding: job.portfolio_id carries global_cco_id (repurposed field).
    job_cco = job.get("portfolio_id")
    res_cco = (results.get("global_cco") or {}).get("global_cco_id")
    if not job_cco:
        problems.append("job missing global CCO binding (portfolio_id)")
    elif res_cco and res_cco != job_cco:
        problems.append(f"CCO binding mismatch: job={job_cco!r} "
                        f"results={res_cco!r}")
    res = results.get("results") or []
    if res:
        bad = [r for r in res if not r.get("global_cco_id")]
        if bad:
            problems.append(f"{len(bad)} results missing global_cco_id")
    return problems


# ---------------------------------------------------------------------------
# Acceptance criteria
# ---------------------------------------------------------------------------

DEFAULT_ACCEPTANCE = {
    "min_results": 1,
    "require_xlsx": True,
    "require_markdown": True,
    "require_reconciliation_invariant": True,
}


def evaluate_acceptance(job: dict, results: dict, artifacts: list,
                        xlsx_problems: list, md_problems: list,
                        binding_problems: list,
                        criteria: dict) -> dict:
    """Acceptance is separate from transport completion."""
    checks = []
    state = job.get("state")
    checks.append({
        "check": "terminal_state_completed",
        "passed": state in COMPLETED_STATES,
        "detail": f"state={state}",
    })
    n = len(results.get("results") or [])
    min_results = criteria.get("min_results", 1)
    checks.append({
        "check": "min_results",
        "passed": n >= min_results,
        "detail": f"{n} results (min {min_results})",
    })
    kinds = {a["filename"]: a["media_type"] for a in artifacts}
    has_xlsx = any("spreadsheetml" in m for m in kinds.values())
    has_md = any("markdown" in m or k.endswith(".md") for k, m in kinds.items())
    if criteria.get("require_xlsx", True):
        checks.append({"check": "xlsx_artifact_present", "passed": has_xlsx,
                       "detail": f"kinds={sorted(kinds)}"})
    if criteria.get("require_markdown", True):
        checks.append({"check": "markdown_artifact_present", "passed": has_md,
                       "detail": f"kinds={sorted(kinds)}"})
    if xlsx_problems:
        checks.append({"check": "xlsx_structure", "passed": False,
                       "detail": "; ".join(xlsx_problems)})
    if md_problems:
        checks.append({"check": "markdown_structure", "passed": False,
                       "detail": "; ".join(md_problems)})
    if criteria.get("require_reconciliation_invariant", True):
        rec = results.get("source_reconciliation") or {}
        inv = rec.get("invariant_ok")
        checks.append({"check": "reconciliation_invariant",
                       "passed": inv is True,
                       "detail": f"invariant_ok={inv}"})
    if binding_problems:
        checks.append({"check": "bindings", "passed": False,
                       "detail": "; ".join(binding_problems)})
    passed = all(c["passed"] for c in checks)
    return {"passed": passed, "checks": checks}


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run_qualification(args, t: Transport, logger: RunLogger,
                      ev: EvidenceDir) -> tuple:
    """Execute one full cycle. Returns (outcome, exit_code, detail)."""
    gateway = args.gateway.rstrip("/")
    run = {
        "run_id": ev.path.name.split("-", 1)[1],
        "session_id": str(uuid.uuid4()),
        "attachment_id": None,
        "mode": args.mode,
    }

    # -- prompt fidelity ----------------------------------------------------
    if args.chat_prompt_file:
        prompt_bytes = Path(args.chat_prompt_file).read_bytes()
    else:
        prompt_bytes = (args.chat_prompt or "").encode("utf-8")
    ev.write("prompt.txt", prompt_bytes)
    ev.write("prompt.sha256", sha256_bytes(prompt_bytes).encode())

    # -- workbook evidence --------------------------------------------------
    wb_path = Path(args.workbook)
    if not wb_path.is_file():
        return OUTCOME_CONFIGURATION_ERROR, 6, f"workbook not found: {wb_path}"
    wb_bytes = wb_path.read_bytes()
    ev.write("input_workbook.xlsx", wb_bytes)
    ev.write("input_workbook.sha256", sha256_bytes(wb_bytes).encode())

    # -- upload (session-scoped; NEVER ingested) ----------------------------
    logger.log(f"session {run['session_id']}: uploading {wb_path.name} "
               f"({len(wb_bytes)} bytes)")
    try:
        upload = upload_workbook(t, gateway, run["session_id"], wb_path, logger)
    except (HttpError, URLError) as e:
        return OUTCOME_INFRASTRUCTURE_ERROR, 8, f"upload failed: {e}"
    ev.write_json("upload_response.json", upload)
    run["attachment_id"] = upload.get("attachment_id")
    if not run["attachment_id"]:
        return OUTCOME_INFRASTRUCTURE_ERROR, 8, "upload response missing attachment_id"
    logger.log(f"uploaded attachment {run['attachment_id']} "
               f"(session-scoped, not ingested)")

    # -- trigger -------------------------------------------------------------
    job_id = None
    fallback = False
    if args.mode == "chat":
        chat_req = build_chat_request(run["session_id"],
                                      prompt_bytes.decode("utf-8"),
                                      [run["attachment_id"]],
                                      args.chat_context_kb)
        ev.write_json("chat_request.json", chat_req)
        logger.log("chat: sending exact prompt bytes to /gateway/chat "
                   "(agent mode, strict)")
        try:
            reply, tool_call = trigger_via_chat(t, gateway, chat_req, logger)
        except (HttpError, URLError) as e:
            return OUTCOME_INFRASTRUCTURE_ERROR, 8, f"chat failed: {e}"
        ev.write_json("chat_response.json", reply)
        if tool_call:
            ev.write_json("tool_call.json", tool_call)
            logger.log(f"chat: agent invoked {QUALIFY_TOOL_NAME}")
            job = find_session_job(t, gateway, run["session_id"],
                                   run["attachment_id"], logger)
            if job:
                job_id = job.get("job_id")
        if not job_id:
            if args.allow_direct_fallback:
                logger.log("chat: tool not triggered — falling back to "
                           "direct mode (--allow-direct-fallback)")
                fallback = True
            else:
                return (OUTCOME_CHAT_TOOL_NOT_TRIGGERED, 2,
                        "strict chat: agent never invoked "
                        f"{QUALIFY_TOOL_NAME}")

    if job_id is None:
        logger.log("direct: POST /api/v2/crm/qualify")
        try:
            resp = start_direct(t, gateway, run["session_id"],
                                run["attachment_id"], args.context_kb,
                                args.retry_of, logger)
        except (HttpError, URLError) as e:
            return OUTCOME_INFRASTRUCTURE_ERROR, 8, f"qualify failed: {e}"
        job_id = resp.get("job_id")
        if not job_id:
            return OUTCOME_INFRASTRUCTURE_ERROR, 8, "qualify response missing job_id"
        logger.log(f"direct: job {job_id} ({resp.get('status')})")

    # -- poll -----------------------------------------------------------------
    logger.log(f"polling job {job_id} (timeout {args.timeout}s)")
    try:
        job = poll_job(t, gateway, job_id, args.timeout, logger)
    except TimeoutError as e:
        return OUTCOME_TIMEOUT, 4, str(e)
    ev.write_json("job_status.json", job)
    state = job.get("state")
    logger.log(f"job terminal state: {state}")

    if state == "JOB_LOST":
        return OUTCOME_JOB_LOST, 3, "job registry lost the job (404/repeated errors)"
    if state == "FAILED":
        return OUTCOME_JOB_FAILED, 3, f"job failed: {job.get('error')}"
    if state == "CANCELLED":
        return OUTCOME_JOB_CANCELLED, 3, "job was cancelled"
    if state == "EXPIRED":
        return OUTCOME_JOB_EXPIRED, 3, "job results expired"
    if state not in COMPLETED_STATES:
        return OUTCOME_INFRASTRUCTURE_ERROR, 8, f"unexpected state {state!r}"

    # -- results ---------------------------------------------------------------
    try:
        results = fetch_results(t, gateway, job_id)
    except HttpError as e:
        if e.status in (409, 410):
            return OUTCOME_JOB_EXPIRED, 3, f"results unavailable: HTTP {e.status}"
        return OUTCOME_INFRASTRUCTURE_ERROR, 8, f"results fetch failed: {e}"
    ev.write_json("results.json", results)

    # -- binding verification ----------------------------------------------------
    binding_problems = verify_bindings(run, job, results)
    if binding_problems:
        ev.write_json("acceptance_result.json", {
            "passed": False, "binding_problems": binding_problems,
        })
        return (OUTCOME_BINDING_MISMATCH, 7, "; ".join(binding_problems))

    # -- artifacts ---------------------------------------------------------------
    try:
        artifacts = download_artifacts(t, gateway, run["session_id"], job, logger)
    except (HttpError, URLError) as e:
        return OUTCOME_INFRASTRUCTURE_ERROR, 8, f"artifact download failed: {e}"
    if not artifacts:
        return OUTCOME_ARTIFACT_INVALID, 5, "job completed with zero artifacts"

    xlsx_problems, md_problems = [], []
    hashes = {}
    for a in artifacts:
        fname = a["filename"]
        data = a["bytes"]
        hashes[fname] = {"sha256": sha256_bytes(data),
                         "size": len(data), "media_type": a["media_type"]}
        if "spreadsheetml" in a["media_type"] or fname.endswith(".xlsx"):
            ev.write("qualification_report.xlsx", data)
            xlsx_problems = validate_xlsx(data, results)
        elif fname.endswith(".md") or "markdown" in a["media_type"]:
            ev.write("qualification_report.md", data)
            md_problems = validate_markdown(data, results)
    ev.write_json("artifact_hashes.json", hashes)

    if xlsx_problems or md_problems:
        detail = "; ".join(xlsx_problems + md_problems)
        ev.write_json("acceptance_result.json", {
            "passed": False, "xlsx_problems": xlsx_problems,
            "md_problems": md_problems,
        })
        return OUTCOME_ARTIFACT_INVALID, 5, detail

    # -- acceptance ---------------------------------------------------------------
    criteria = dict(DEFAULT_ACCEPTANCE)
    if args.acceptance_file:
        criteria.update(json.loads(Path(args.acceptance_file).read_text()))
    acceptance = evaluate_acceptance(job, results, artifacts,
                                     xlsx_problems, md_problems,
                                     binding_problems, criteria)
    ev.write_json("acceptance_result.json", acceptance)

    if not acceptance["passed"]:
        failed = [c["check"] for c in acceptance["checks"] if not c["passed"]]
        return (OUTCOME_QUALIFICATION_INCOMPLETE, 1,
                f"acceptance failed: {failed}")

    if state == "COMPLETED_WITH_WARNINGS":
        return (OUTCOME_PASS_WITH_WARNINGS, 0,
                f"pass with warnings: {job.get('warnings')}")
    if fallback:
        return (OUTCOME_CHAT_FAILED_DIRECT_FALLBACK_SUCCEEDED, 9,
                "chat did not trigger the tool; direct fallback succeeded")
    return OUTCOME_PASS, 0, "acceptance passed"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="e2e_qualify",
        description=("Deterministic E2E CRM qualification harness. "
                     "The workbook is a session-scoped attachment and is "
                     "never ingested. ACP/CCO are global and never selected "
                     "by a KB."),
    )
    p.add_argument("--workbook", required=True,
                   help="Path to the candidate workbook (.xlsx).")
    p.add_argument("--gateway", default="http://localhost:8202",
                   help="Gateway base URL (default: %(default)s).")
    p.add_argument("--mode", choices=["direct", "chat"], default="direct",
                   help="Trigger mode (default: direct).")
    p.add_argument("--chat-prompt",
                   help="Inline chat prompt (chat mode only).")
    p.add_argument("--chat-prompt-file",
                   help="File whose EXACT bytes are transmitted as the chat "
                        "prompt. Mutually exclusive with --chat-prompt.")
    p.add_argument("--strict-chat", action="store_true", default=False,
                   help="Chat mode: fail with CHAT_TOOL_NOT_TRIGGERED if the "
                        "agent never invokes qualify_candidates. This is the "
                        "DEFAULT behavior when neither flag is given.")
    p.add_argument("--allow-direct-fallback", action="store_true",
                   help="Chat mode: if the agent does not trigger the tool, "
                        "fall back to direct mode and report "
                        "CHAT_FAILED_DIRECT_FALLBACK_SUCCEEDED on success. "
                        "Mutually exclusive with --strict-chat.")
    p.add_argument("--context-kb",
                   help="Direct mode: optional conversational/retrieval "
                        "context ONLY. Does NOT select the ACP or CCO (both "
                        "are global), does NOT ingest the workbook, and does "
                        "NOT scope qualification. Omit unless you want "
                        "GraphRAG retrieval context.")
    p.add_argument("--chat-context-kb",
                   help="Chat mode: same semantics as --context-kb — "
                        "conversational/retrieval context only, sent as "
                        "kb_ids to the chat turn. Never determines ACP/CCO.")
    p.add_argument("--retry-of",
                   help="Existing job_id to retry (direct mode).")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                   help="Polling timeout seconds (default: %(default)s).")
    p.add_argument("--out", default=DEFAULT_OUT,
                   help="Evidence root directory (default: %(default)s). "
                        "WARNING: /tmp is not durable across reboots.")
    p.add_argument("--acceptance-file",
                   help="JSON file overriding acceptance criteria "
                        f"(defaults: {DEFAULT_ACCEPTANCE}).")
    p.add_argument("--version", action="version", version="e2e_qualify 2.0")
    return p


def validate_args(args, logger) -> Optional[str]:
    if args.mode == "chat":
        if bool(args.chat_prompt) == bool(args.chat_prompt_file):
            return ("chat mode requires exactly one of --chat-prompt or "
                    "--chat-prompt-file")
        if args.allow_direct_fallback and args.strict_chat:
            return ("--allow-direct-fallback and --strict-chat are mutually "
                    "exclusive")
    if args.mode == "direct" and (args.chat_prompt or args.chat_prompt_file):
        return "--chat-prompt/--chat-prompt-file are chat-mode only"
    if args.mode == "direct" and args.chat_context_kb:
        return "--chat-context-kb is chat-mode only (use --context-kb)"
    if args.mode == "chat" and args.context_kb:
        return "--context-kb is direct-mode only (use --chat-context-kb)"
    if str(args.out).startswith("/tmp"):
        logger.log("WARNING: --out under /tmp is not durable across reboots; "
                   "evidence may be lost.")
    return None


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    run_id = uuid.uuid4().hex[:12]
    root = Path(args.out)
    try:
        ev = EvidenceDir(root, run_id)
    except OSError as e:
        print(f"cannot create evidence dir: {e}", file=sys.stderr)
        return EXIT_CODES[OUTCOME_INFRASTRUCTURE_ERROR]
    logger = RunLogger(ev.path / "execution.log")
    logger.log(f"e2e_qualify run {run_id} mode={args.mode}")

    err = validate_args(args, logger)
    if err:
        logger.log(f"CONFIGURATION_ERROR: {err}")
        logger.log(f"outcome={OUTCOME_CONFIGURATION_ERROR}")
        logger.close()
        return EXIT_CODES[OUTCOME_CONFIGURATION_ERROR]

    t = Transport()
    try:
        outcome, code, detail = run_qualification(args, t, logger, ev)
    except Exception as e:  # noqa: BLE001
        logger.log(f"UNEXPECTED ERROR: {e}")
        traceback.print_exc(file=sys.stderr)
        outcome, code = OUTCOME_INFRASTRUCTURE_ERROR, 8
        detail = f"unexpected error: {e}"
    logger.log(f"outcome={outcome} exit={code} detail={detail}")
    logger.log(f"evidence: {ev.path}")

    # -- run manifest (always written last, summarises the run) --------------
    try:
        manifest = {
            "run_id": run_id,
            "evidence_dir": str(ev.path),
            "mode": args.mode,
            "gateway": args.gateway,
            "workbook": str(args.workbook),
            "workbook_sha256": None,
            "prompt_sha256": None,
            "session_id": None,
            "attachment_id": None,
            "job_id": None,
            "context_kb": args.context_kb or args.chat_context_kb,
            "outcome": outcome,
            "exit_code": code,
            "detail": detail,
        }
        wb_hash_file = ev.path / "input_workbook.sha256"
        prompt_hash_file = ev.path / "prompt.sha256"
        if wb_hash_file.exists():
            manifest["workbook_sha256"] = wb_hash_file.read_text().strip()
        if prompt_hash_file.exists():
            manifest["prompt_sha256"] = prompt_hash_file.read_text().strip()
        for name, key in (("upload_response.json", "attachment_id"),):
            f = ev.path / name
            if f.exists():
                manifest["attachment_id"] = json.loads(
                    f.read_text()).get("attachment_id")
        js = ev.path / "job_status.json"
        if js.exists():
            j = json.loads(js.read_text())
            manifest["job_id"] = j.get("job_id")
            manifest["session_id"] = j.get("session_id")
        if manifest["session_id"] is None:
            # session_id is generated inside run_qualification; recover from
            # chat_request.json or upload URL in execution.log
            cr = ev.path / "chat_request.json"
            if cr.exists():
                manifest["session_id"] = json.loads(
                    cr.read_text()).get("session_id")
        (ev.path / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False))
        os.chmod(ev.path / "run_manifest.json", 0o444)
    except Exception as e:  # noqa: BLE001
        logger.log(f"manifest write failed: {e}")
    logger.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
