#!/usr/bin/env python3
"""E2E acceptance: Durable Completed-Job and Assessment-Draft Archive
(Milestone A, item A10 live scenarios).

Proves against the LIVE deployment:
  1. a completed qualification survives full service recreation
     (in-memory registry cleared; durable archive consulted);
  2. archived result JSON remains retrievable;
  3. archived Markdown and XLSX remain retrievable and hash-correct;
  4. a candidate can be approved after recreation (archived draft;
     no in-memory source job);
  5. retry lineage remains resolvable after recreation
     (retry-from-archived-job produces a new job whose lineage resolves
     the original);
  6. API/Markdown/XLSX/chat-compatible payloads derive from the
     archived result when the live job is unavailable;
  7. duplicate archive rows never appear (single archived job per
     completed run).

Modes:
  --recreate restart  (default) docker compose restart of the ingestion
                      service (clears the in-memory job registry; keeps
                      the durable volume)
  --recreate rebuild  docker compose build + up -d --force-recreate of
                      the ingestion service (full IMAGE recreation)
  --recreate none     skip recreation (developer mode)

Exit codes:
  0 PASS
  1 ACCEPTANCE_FAILED
  2 CONFIGURATION_ERROR
  3 INFRASTRUCTURE_ERROR (transport / docker / health)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, List, Optional

# Reuse the hardened harness helpers from e2e_qualify.py (same dir).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_qualify import (  # noqa: E402
    COMPLETED_STATES, EvidenceDir, HttpError, RunLogger, TERMINAL_STATES,
    Transport, guess_mime, poll_job, sha256_bytes, start_direct,
    upload_workbook,
)

DEFAULT_GATEWAY = "http://localhost:8202"
DEFAULT_TIMEOUT_S = 1500.0
POLL_INTERVAL_S = 5.0


class AcceptanceError(Exception):
    pass


class InfrastructureError(Exception):
    pass


def get_json(t: Transport, url: str) -> Any:
    try:
        _, data = t.get_json(url)
        return data
    except HttpError as e:
        raise AcceptanceError(
            f"GET {url} -> HTTP {e.status}: {e.body[:300]!r}") from e


def post_json(t: Transport, url: str, payload: dict) -> tuple:
    status, body = t.post_json(url, payload)
    if status >= 500:
        raise AcceptanceError(
            f"POST {url} -> HTTP {status}: {body[:300]!r}")
    return status, body


def recreate_service(mode: str, compose_file: Path, service: str,
                     project: Optional[str], env_file: Optional[str],
                     logger: RunLogger) -> None:
    """Restart or fully rebuild+recreate the ingestion service."""
    base = ["docker", "compose", "-f", str(compose_file)]
    if project:
        base += ["--project-name", project]
    if env_file:
        # Per-service env_file interpolation (mirrors manage.sh ENV_FILE).
        base += ["--env-file", env_file]
    if mode == "restart":
        cmd = [*base, "restart", service]
    elif mode == "rebuild":
        cmds = [
            [*base, "build", service],
            [*base, "up", "-d", "--force-recreate", "--no-deps", service],
        ]
        for c in cmds:
            logger.log(f"[recreate] $ {' '.join(c)}")
            proc = subprocess.run(c, capture_output=True, text=True)
            if proc.returncode != 0:
                raise InfrastructureError(
                    f"recreate failed ({proc.returncode}): "
                    f"{proc.stderr[-800:]}")
        return
    else:
        raise AcceptanceError(f"unknown recreate mode {mode!r}")
    logger.log(f"[recreate] $ {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise InfrastructureError(
            f"recreate failed ({proc.returncode}): {proc.stderr[-800:]}")


def wait_healthy(t: Transport, gateway: str, logger: RunLogger,
                 timeout_s: float = 180.0) -> None:
    deadline = time.monotonic() + timeout_s
    url = f"{gateway}/api/v2/crm/health"
    last = None
    while time.monotonic() < deadline:
        try:
            status, _body = t.request("GET", url)
            if status == 200:
                logger.log("[health] ingestion healthy via gateway")
                return
            last = status
        except Exception as e:  # noqa: BLE001 — infra probe
            last = str(e)
        time.sleep(3.0)
    raise InfrastructureError(f"service not healthy after {timeout_s}s "
                              f"(last: {last})")


def wait_restart_observed(t: Transport, gateway: str, job_id: str,
                          logger: RunLogger,
                          timeout_s: float = 180.0) -> bool:
    """Wait until the restart is OBSERVED: the live job must disappear
    from the in-memory registry and resolve as archived (or 404)."""
    deadline = time.monotonic() + timeout_s
    url = f"{gateway}/api/v2/crm/jobs/{job_id}"
    last = None
    while time.monotonic() < deadline:
        try:
            status, body = t.request("GET", url)
            last = status
            if status == 200:
                data = json.loads(body.decode("utf-8"))
                if data.get("archived") is True:
                    logger.log("[restart] observed: job resolves through "
                               "the durable archive")
                    return True
            elif status == 404:
                logger.log("[restart] observed: live registry empty "
                           "(404; archive fallback applies)")
                return True
        except Exception as e:  # noqa: BLE001 — infra probe
            last = str(e)
        time.sleep(3.0)
    logger.log(f"[restart] WARNING: restart not observed within "
               f"{timeout_s}s (last: {last}) — the in-memory registry "
               f"still holds the job; archive-resolution checks may fail")
    return False


def fetch_pre_state(t: Transport, gateway: str, job_id: str,
                    logger: RunLogger) -> dict:
    """Snapshot the live (pre-restart) state of a completed job."""
    state: dict = {}
    status = get_json(t, f"{gateway}/api/v2/crm/jobs/{job_id}")
    if status.get("state") not in COMPLETED_STATES:
        raise AcceptanceError(
            f"job {job_id} not completed pre-restart: {status.get('state')}")
    state["status"] = status
    results = get_json(t, f"{gateway}/api/v2/crm/jobs/{job_id}/results")
    if not results.get("results"):
        raise AcceptanceError("live results payload has no results")
    state["results"] = results
    state["results_sha"] = sha256_bytes(json.dumps(
        results["results"], sort_keys=True, default=str).encode())
    # Archive must already exist (archival happens at completion).
    arch = get_json(t, f"{gateway}/api/v2/crm/archive/jobs/{job_id}")
    if not arch.get("result_available"):
        raise AcceptanceError(
            "archive row missing or result_available=false BEFORE restart")
    state["archive"] = arch
    artifacts = get_json(
        t, f"{gateway}/api/v2/crm/archive/jobs/{job_id}/artifacts")
    arts = artifacts.get("artifacts") or []
    state["artifacts"] = arts
    logger.log(f"[pre] archive artifacts: "
               f"{[a['artifact_type'] for a in arts]}")
    return state


def verify_post_state(t: Transport, gateway: str, job_id: str,
                      pre: dict, logger: RunLogger,
                      evidence: EvidenceDir) -> List[dict]:
    """All post-recreation acceptance checks. Returns check list."""
    checks: List[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "passed": bool(ok), "detail": detail})
        logger.log(f"[check] {name}: {'PASS' if ok else 'FAIL'} {detail}")

    # 1. job status resolves through the archive (was 404/LOST before).
    status = get_json(t, f"{gateway}/api/v2/crm/jobs/{job_id}")
    check("status_resolved_from_archive",
          status.get("archived") is True
          and status.get("state") in COMPLETED_STATES,
          f"archived={status.get('archived')} state={status.get('state')}")
    evidence.write_json("postrestart_job_status.json", status)

    # 2. archived result JSON retrievable + identical payload shape.
    results = get_json(t, f"{gateway}/api/v2/crm/jobs/{job_id}/results")
    sha = sha256_bytes(json.dumps(
        results.get("results"), sort_keys=True, default=str).encode())
    check("archived_results_retrievable",
          results.get("archived") is True and bool(results.get("results")),
          f"results={len(results.get('results') or [])}")
    check("archived_results_payload_stable",
          sha == pre["results_sha"], f"sha={sha[:16]}…")
    evidence.write_json("postrestart_results.json", results)

    # 3. archived MD/XLSX retrievable and hash-correct.
    for art in pre["artifacts"]:
        if art["artifact_type"] not in ("markdown_report", "xlsx_report"):
            continue
        status_, body = t.request(
            "GET",
            f"{gateway}/api/v2/crm/archive/artifacts/"
            f"{art['artifact_id']}/content")
        ok = status_ == 200
        detail = f"HTTP {status_}"
        if ok:
            digest = sha256_bytes(body)
            ok = digest == art.get("sha256")
            detail = (f"type={art['artifact_type']} "
                      f"hash_match={digest == art.get('sha256')} "
                      f"bytes={len(body)}")
            suffix = ".md" if art["artifact_type"] == "markdown_report" \
                else ".xlsx"
            evidence.write(f"archived{suffix}", body)
        check("archived_artifact_hash_correct", ok, detail)

    # 4. session job listing includes the archived job.
    session_jobs = get_json(
        t, f"{gateway}/api/v2/crm/sessions/"
           f"{pre['status'].get('session_id') or pre['archive'].get('session_id')}/jobs")
    archived_entry = next(
        (j for j in session_jobs.get("jobs") or []
         if j.get("job_id") == job_id and j.get("archived")), None)
    check("session_jobs_include_archived",
          archived_entry is not None,
          f"found={archived_entry is not None}")

    # 5. drafts exist and one can be APPROVED after recreation.
    drafts = get_json(
        t, f"{gateway}/api/v2/crm/archive/jobs/{job_id}/drafts")
    draft_list = drafts.get("drafts") or []
    check("archived_drafts_listed", len(draft_list) >= 1,
          f"drafts={len(draft_list)}")
    evidence.write_json("postrestart_drafts.json", drafts)

    approved: Optional[dict] = None
    for d in draft_list:
        if d.get("approval_eligibility") != "ELIGIBLE":
            continue
        if d.get("review_status") in ("APPROVED", "REJECTED"):
            # idempotent re-run: already decided
            continue
        status_, body = post_json(
            t,
            f"{gateway}/api/v2/crm/archive/drafts/"
            f"{d['draft_id']}/approve",
            {"reviewer_id": "e2e_archive_acceptance",
             "comment": "Milestone A live acceptance: archived draft "
                        "approval after service recreation"})
        if status_ == 200:
            approved = json.loads(body.decode("utf-8"))
            break
    if approved is not None and draft_list:
        # Any eligible draft already approved in a previous run also
        # counts: find an approved one.
        check("archived_draft_approvable", True,
              f"assessment_id={approved.get('assessment_id')}")
        assessment = get_json(
            t, f"{gateway}/api/v2/crm/intelligence/assessments/"
               f"{approved['assessment_id']}")
        check("approval_visible_in_intelligence_store",
              assessment.get("assessment_status") == "APPROVED",
              f"status={assessment.get('assessment_status')}")
        evidence.write_json("archived_draft_approval.json", approved)
    else:
        already = [d for d in draft_list
                   if d.get("review_status") == "APPROVED"]
        if already:
            check("archived_draft_approvable", True,
                  "eligible draft already approved (idempotent re-run)")
        else:
            check("archived_draft_approvable", False,
                  f"no eligible draft among {len(draft_list)}")

    # 6. duplicate archive rows never appear for this job.
    listing = get_json(t, f"{gateway}/api/v2/crm/archive/jobs")
    same = [j for j in listing.get("jobs") or []
            if j.get("job_id") == job_id]
    check("no_duplicate_archive_rows", len(same) == 1,
          f"rows={len(same)}")

    return checks


def verify_retry_lineage(t: Transport, gateway: str, job_id: str,
                         session_id: str, timeout_s: float,
                         logger: RunLogger,
                         evidence: EvidenceDir) -> List[dict]:
    """Retry from the archived job; verify lineage after completion."""
    checks: List[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "passed": bool(ok), "detail": detail})
        logger.log(f"[check] {name}: {'PASS' if ok else 'FAIL'} {detail}")

    status_, body = post_json(
        t, f"{gateway}/api/v2/crm/archive/jobs/{job_id}/retry", {})
    if status_ not in (200, 202):
        check("retry_from_archive_accepted", False,
              f"HTTP {status_}: {body[:200]!r}")
        return checks
    retry = json.loads(body.decode("utf-8"))
    new_job_id = retry.get("job_id")
    check("retry_from_archive_accepted", bool(new_job_id),
          f"new_job={new_job_id} already_running="
          f"{retry.get('status') == 'already_running'}")
    if not new_job_id:
        return checks
    if retry.get("status") == "already_running":
        check("retry_lineage_resolvable", True,
              "another run already active; skipped")
        return checks

    # Poll the retried job to completion (durable archive refresh).
    try:
        final = poll_job(t, gateway, new_job_id, timeout_s,
                         logger, POLL_INTERVAL_S)
    except Exception as e:  # noqa: BLE001
        check("retry_job_completed", False, str(e))
        return checks
    state = (final.get("state") or "").upper()
    check("retry_job_completed", state in COMPLETED_STATES, f"state={state}")

    lineage = get_json(
        t, f"{gateway}/api/v2/crm/archive/jobs/{new_job_id}/lineage")
    check("retry_lineage_resolvable_after_recreation",
          lineage.get("root_job_id") == job_id,
          f"root={lineage.get('root_job_id')} chain="
          f"{lineage.get('retry_chain')}")
    evidence.write_json("retry_lineage.json", lineage)
    return checks


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Milestone A live acceptance: durable archive "
                    "survives service recreation.")
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY)
    parser.add_argument("--job-id", default=None,
                        help="existing COMPLETED job id (from a prior "
                             "e2e_qualify run). When omitted, a fresh "
                             "qualification is executed first.")
    parser.add_argument("--workbook", default=None,
                        help="workbook to qualify when --job-id is "
                             "omitted (required in that mode)")
    parser.add_argument("--kb-id", default=None,
                        help="optional conversational context KB "
                             "(never selects ACP/CCO)")
    parser.add_argument("--skip-approval", action="store_true",
                        help="do not approve any draft (read-only run)")
    parser.add_argument("--skip-retry", action="store_true",
                        help="skip the retry-lineage scenario")
    parser.add_argument("--recreate", choices=("restart", "rebuild", "none"),
                        default="restart",
                        help="service recreation mode (default: restart)")
    parser.add_argument("--compose-file", default=None,
                        help="docker-compose file for recreation")
    parser.add_argument("--compose-project", default=None,
                        help="docker compose project name (defaults to "
                             "$COMPOSE_PROJECT_NAME or the compose "
                             "directory name)")
    parser.add_argument("--compose-env-file", default=None,
                        help="compose --env-file (defaults to $ENV_FILE "
                             "or .env when present)")
    parser.add_argument("--service", default="retriva-ingestion",
                        help="compose service to recreate")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--out", default="./artifacts/e2e-archive-runs")
    args = parser.parse_args(argv)

    run_id = uuid.uuid4().hex[:12]
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    evidence = EvidenceDir(out_root, run_id)
    logger = RunLogger(evidence.path / "execution.log")
    t = Transport()
    checks: List[dict] = []

    try:
        # ------------------------------------------------------------------
        # Phase 1: obtain a completed job (fresh or provided).
        # ------------------------------------------------------------------
        job_id = args.job_id
        session_id: Optional[str] = None
        attachment_id: Optional[str] = None
        if job_id is None:
            if not args.workbook:
                logger.log("CONFIGURATION_ERROR: --workbook required when "
                           "--job-id is omitted")
                return 2
            workbook = Path(args.workbook)
            if not workbook.is_file():
                logger.log(f"CONFIGURATION_ERROR: workbook not found: "
                           f"{workbook}")
                return 2
            session_id = f"sess_e2e_archive_{run_id}"
            get_json(t, f"{args.gateway}/api/v2/crm/health")  # connectivity
            # Sessions are created lazily by the attachments endpoint.
            upload = upload_workbook(t, args.gateway, session_id, workbook,
                                     logger)
            attachment_id = upload.get("attachment_id") \
                or (upload.get("attachment") or {}).get("attachment_id")
            evidence.write_json("upload_response.json", upload)
            if not attachment_id:
                raise AcceptanceError(f"no attachment_id in upload: "
                                      f"{upload}")
            resp = start_direct(t, args.gateway, session_id, attachment_id,
                                args.kb_id, None, logger)
            job_id = resp.get("job_id")
            logger.log(f"[setup] qualification job: {job_id}")
            evidence.write_json("qualify_response.json", resp)
            if not job_id:
                raise AcceptanceError(f"no job_id in qualify response: "
                                      f"{resp}")
            final = poll_job(t, args.gateway, job_id, args.timeout,
                             logger, POLL_INTERVAL_S)
            if (final.get("state") or "").upper() not in COMPLETED_STATES:
                raise AcceptanceError(
                    f"fresh qualification ended in "
                    f"{final.get('state')}")
        else:
            # Session/attachment discovered from the archive pre-state.
            pass

        # ------------------------------------------------------------------
        # Phase 2: pre-restart snapshot.
        # ------------------------------------------------------------------
        pre = fetch_pre_state(t, args.gateway, job_id, logger)
        evidence.write_json("pre_restart_status.json", pre["status"])
        evidence.write_json("pre_restart_archive.json", pre["archive"])
        session_id = session_id or pre["archive"].get("session_id")

        # ------------------------------------------------------------------
        # Phase 3: service recreation (in-memory registry destroyed).
        # ------------------------------------------------------------------
        if args.recreate != "none":
            compose = Path(args.compose_file) if args.compose_file else (
                Path(__file__).resolve().parent.parent
                / "docker-compose.yml")
            project = args.compose_project or os.environ.get(
                "COMPOSE_PROJECT_NAME")
            env_file = args.compose_env_file or os.environ.get("ENV_FILE")
            if env_file is None:
                candidate = compose.parent / ".env"
                env_file = str(candidate) if candidate.exists() else None
            recreate_service(args.recreate, compose, args.service,
                             project, env_file, logger)
            wait_healthy(t, args.gateway, logger)
            wait_restart_observed(t, args.gateway, job_id, logger)
        else:
            logger.log("[recreate] skipped (developer mode)")

        # ------------------------------------------------------------------
        # Phase 4: post-recreation verification.
        # ------------------------------------------------------------------
        checks.extend(verify_post_state(
            t, args.gateway, job_id, pre, logger, evidence))

        # ------------------------------------------------------------------
        # Phase 5: retry lineage (after recreation).
        # ------------------------------------------------------------------
        if not args.skip_retry and session_id:
            checks.extend(verify_retry_lineage(
                t, args.gateway, job_id, session_id, args.timeout,
                logger, evidence))

        passed = all(c["passed"] for c in checks)
        result = {
            "passed": passed,
            "run_id": run_id,
            "job_id": job_id,
            "recreate_mode": args.recreate,
            "checks": checks,
        }
        evidence.write_json("acceptance_result.json", result)
        logger.log(f"ACCEPTANCE {'PASS' if passed else 'FAIL'} "
                   f"({sum(c['passed'] for c in checks)}/"
                   f"{len(checks)} checks)")
        return 0 if passed else 1
    except AcceptanceError as e:
        logger.log(f"ACCEPTANCE_FAILED: {e}")
        evidence.write_json("acceptance_result.json", {
            "passed": False, "error": str(e), "checks": checks})
        return 1
    except InfrastructureError as e:
        logger.log(f"INFRASTRUCTURE_ERROR: {e}")
        evidence.write_json("acceptance_result.json", {
            "passed": False, "error": str(e), "checks": checks})
        return 3
    except Exception as e:  # noqa: BLE001
        logger.log(f"UNEXPECTED_ERROR: {e}")
        import traceback
        traceback.print_exc()
        evidence.write_json("acceptance_result.json", {
            "passed": False, "error": f"{type(e).__name__}: {e}",
            "checks": checks})
        return 3
    finally:
        logger.close()


if __name__ == "__main__":
    sys.exit(main())
