"""Deterministic mocked test suite for scripts/e2e_qualify.py.

NO live Web Research, NO live LLM, NO live SearXNG, NO network. All HTTP
interactions go through a fake Transport (http_fn) that replays scripted
responses. Covers the spec's ~35 scenarios: direct/chat success, strict
chat, fallback, KB-independence of ACP/CCO bindings, session scoping,
no-ingestion, binding mismatches, job failure/cancel/expiry/loss, timeout,
polling resilience, artifact validation (invalid XLSX, HTML-as-XLSX,
invalid MD, reconciliation mismatch), prompt fidelity (Unicode, multiline,
hashes), run manifest, artifact hashes, and the full exit-code contract.

Run:  python3 -m pytest tests/test_e2e_qualify.py -q
  or: python3 tests/test_e2e_qualify.py           (unittest fallback)
"""

from __future__ import annotations

import io
import json
import sys
import time
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import e2e_qualify as eq  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def make_xlsx(sheets=("Qualification", "Average Customer Profile",
                      "Source Record Reconciliation")) -> bytes:
    """Minimal valid XLSX (ZIP with xl/workbook.xml listing sheets)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        sheet_xml = "".join(
            f'<sheet name="{s}" sheetId="{i+1}" r:id="rId{i+1}"/>'
            for i, s in enumerate(sheets))
        zf.writestr(
            "xl/workbook.xml",
            f'<?xml version="1.0"?><workbook><sheets>{sheet_xml}</sheets></workbook>')
        zf.writestr("[Content_Types].xml", "<Types/>")
    return buf.getvalue()


def make_md(h1="Qualification Report", h2s=("Acme GmbH",)) -> bytes:
    parts = [f"# {h1}\n"]
    for h in h2s:
        parts.append(f"## {h}\nTier A.\n")
    return "".join(parts).encode("utf-8")


def make_workbook() -> bytes:
    # Any bytes work for upload (the fake accepts them); use a real-ish XLSX.
    return make_xlsx(("Leads",))


def ok_results(n=2, cco="cco_global_1", cco_ver=3, cco_hash="abc123",
               acp="acp_global_1", acp_ver=2, invariant=True,
               reported=15) -> dict:
    return {
        "results": [
            {"name": f"Company {i}", "tier": "A",
             "global_cco_id": cco, "global_cco_version": cco_ver,
             "global_cco_source_hash": cco_hash}
            for i in range(n)
        ],
        "source_reconciliation": {
            "total_rows_detected": reported, "total_rows_accepted": reported,
            "total_rows_reported": reported, "invariant_ok": invariant,
        },
        "global_cco": {"global_cco_id": cco, "name": "CCO", "version": cco_ver,
                        "source_hash": cco_hash, "offering_families": []},
        "acp_family": [{"acp_id": acp, "acp_version": acp_ver, "label": "ACP"}],
    }


def ok_job(state="COMPLETED", session_id=None, attachment_id=None,
           acp="acp_global_1", acp_ver=2, cco="cco_global_1", cco_ver=3,
           warnings=None) -> dict:
    return {
        "job_id": "job_1", "state": state, "progress": 1.0,
        "session_id": session_id, "attachment_id": attachment_id,
        "acp_id": acp, "acp_version": acp_ver,
        "portfolio_id": cco, "portfolio_version": cco_ver,
        "artifact_ids": ["art_md", "art_xlsx"],
        "warnings": warnings or [], "error": None,
    }


class FakeHTTP:
    """Scripted HTTP responder. Records requests for assertions."""

    def __init__(self):
        self.requests = []          # (method, url, body, headers)
        self.override = None        # optional callable(method,url,body,headers)
        self.session_jobs = []
        self.job = None
        self.results = None
        self.artifacts = None       # {artifact_id: (filename, media_type, bytes)}
        self.upload_status = 201
        self.poll_sequence = []     # list of job dicts to return in order
        self.chat_reply = None

    # -- route plumbing ------------------------------------------------------
    def __call__(self, method, url, body, headers):
        self.requests.append((method, url, body, headers))
        if self.override is not None:
            return self.override(method, url, body, headers)
        return self._route(method, url, body, headers)

    def _route(self, method, url, body, headers):
        if method == "POST" and "/attachments" in url:
            if self.upload_status != 201:
                return self.upload_status, b'{"detail": "upload failed"}'
            return 201, json.dumps({
                "attachment_id": "att_1", "session_id": "sess_from_upload",
                "original_filename": "wb.xlsx", "status": "stored",
            }).encode()
        if method == "POST" and "/gateway/chat" in url:
            if self.chat_reply is None:
                return 502, b'{"detail": "chat down"}'
            return 200, json.dumps(self.chat_reply).encode()
        if method == "POST" and "/api/v2/crm/qualify" in url:
            return 202, json.dumps({
                "status": "accepted", "job_id": "job_1",
            }).encode()
        if method == "GET" and "/sessions/" in url and "/jobs" in url:
            return 200, json.dumps({"session_id": "s", "jobs": self.session_jobs}).encode()
        if method == "GET" and "/crm/jobs/" in url and "/results" in url:
            if self.results is None:
                return 409, b'{"detail": "not ready"}'
            return 200, json.dumps(self.results).encode()
        if method == "GET" and "/crm/jobs/" in url:
            if self.poll_sequence:
                self.job = self.poll_sequence.pop(0)
            if self.job is None:
                return 404, b'{"detail": "not found"}'
            return 200, json.dumps(self.job).encode()
        if method == "GET" and "/artifacts/" in url and url.endswith("/content"):
            aid = url.split("/artifacts/")[1].split("/")[0]
            fname, mtype, data = self.artifacts[aid]
            return 200, data
        if method == "GET" and "/artifacts/" in url:
            aid = url.split("/artifacts/")[1].split("/")[0]
            fname, mtype, data = self.artifacts[aid]
            return 200, json.dumps({"filename": fname, "media_type": mtype}).encode()
        return 404, b'{"detail": "unrouted"}'


def std_artifacts():
    return {
        "art_md": ("qualification_report.md", "text/markdown", make_md()),
        "art_xlsx": ("qualification_report.xlsx",
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     make_xlsx()),
    }


class Harness:
    """Runs main() with a fake HTTP and temp evidence dir."""

    def __init__(self, tmpdir: Path, fake: FakeHTTP):
        self.tmpdir = tmpdir
        self.fake = fake

    def run(self, argv: list) -> tuple:
        out = self.tmpdir / "runs"
        argv = list(argv) + ["--out", str(out)]
        with patch.object(eq.Transport, "_stdlib_http", self.fake):
            code = eq.main(argv)
        runs = sorted(out.iterdir())
        assert runs, "no evidence dir created"
        self.ev = runs[-1]
        return code, self.ev


def make_harness(tmpdir: Path, **fake_kwargs) -> Harness:
    fake = FakeHTTP()
    for k, v in fake_kwargs.items():
        setattr(fake, k, v)
    return Harness(tmpdir, fake)


def base_args(mode="direct", workbook=None, tmpdir=None, **kw):
    wb = tmpdir / "wb.xlsx"
    wb.write_bytes(make_workbook())
    argv = ["--workbook", str(wb), "--mode", mode, "--timeout", "1"]
    for k, v in kw.items():
        flag = f"--{k.replace('_', '-')}"
        if isinstance(v, bool):
            if v:
                argv.append(flag)
        else:
            argv += [flag, str(v)]
    return argv


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDirectMode(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def _ok_fake(self):
        f = FakeHTTP()
        f.job = ok_job()
        f.results = ok_results()
        f.artifacts = std_artifacts()
        return f

    def test_direct_success_pass(self):
        h = Harness(self.tmp, self._ok_fake())
        code, ev = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 0)
        log = (ev / "execution.log").read_text()
        self.assertIn("outcome=PASS", log)
        self.assertTrue((ev / "run_manifest.json").exists())
        self.assertTrue((ev / "qualification_report.xlsx").exists())
        self.assertTrue((ev / "qualification_report.md").exists())
        self.assertTrue((ev / "acceptance_result.json").exists())

    def test_direct_no_kb_in_request(self):
        f = self._ok_fake()
        h = Harness(self.tmp, f)
        h.run(base_args(tmpdir=self.tmp))
        qualify = [r for r in f.requests if r[0] == "POST" and "crm/qualify" in r[1]][0]
        body = json.loads(qualify[2])
        self.assertNotIn("kb_id", body)

    def test_direct_context_kb_sent_as_kb_id(self):
        f = self._ok_fake()
        h = Harness(self.tmp, f)
        h.run(base_args(tmpdir=self.tmp, context_kb="cust_0007"))
        qualify = [r for r in f.requests if r[0] == "POST" and "crm/qualify" in r[1]][0]
        body = json.loads(qualify[2])
        self.assertEqual(body.get("kb_id"), "cust_0007")

    def test_direct_job_failed_exit3(self):
        f = self._ok_fake()
        f.job = ok_job(state="FAILED")
        f.job["error"] = "boom"
        h = Harness(self.tmp, f)
        code, ev = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 3)
        self.assertIn("outcome=JOB_FAILED", (ev / "execution.log").read_text())

    def test_direct_job_cancelled_exit3(self):
        f = self._ok_fake()
        f.job = ok_job(state="CANCELLED")
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 3)

    def test_direct_job_expired_exit3(self):
        f = self._ok_fake()
        f.job = ok_job(state="EXPIRED")
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 3)

    def test_results_410_exit3(self):
        f = self._ok_fake()
        f.job = ok_job()
        f.results = None
        f.override = lambda m, u, b, h: (
            (410, b'{"detail": "gone"}')
            if m == "GET" and "/results" in u else f._route(m, u, b, h))
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 3)

    def test_upload_failure_exit8(self):
        f = self._ok_fake()
        f.upload_status = 500
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 8)

    def test_qualify_endpoint_down_exit8(self):
        f = self._ok_fake()
        f.override = lambda m, u, b, h: (
            (503, b"down") if m == "POST" and "crm/qualify" in u
            else f._route(m, u, b, h))
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 8)

    def test_completed_with_warnings_pass(self):
        f = self._ok_fake()
        f.job = ok_job(state="COMPLETED_WITH_WARNINGS", warnings=["w1"])
        h = Harness(self.tmp, f)
        code, ev = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 0)
        self.assertIn("outcome=PASS_WITH_WARNINGS",
                      (ev / "execution.log").read_text())

    def test_completed_with_warnings_acceptance_fail_exit1(self):
        f = self._ok_fake()
        f.job = ok_job(state="COMPLETED_WITH_WARNINGS")
        f.results = ok_results(invariant=False)
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 1)

    def test_zero_results_acceptance_fail_exit1(self):
        f = self._ok_fake()
        f.results = ok_results(n=0)
        h = Harness(self.tmp, f)
        code, ev = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 1)
        acc = json.loads((ev / "acceptance_result.json").read_text())
        self.assertFalse(acc["passed"])

    def test_reconciliation_invariant_false_exit1(self):
        f = self._ok_fake()
        f.results = ok_results(invariant=False)
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 1)


class TestChatMode(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def _fake_with_tool(self, **job_kw):
        f = FakeHTTP()
        f.chat_reply = {
            "role": "assistant", "content": "ok",
            "agent": {"iterations": 2, "stopped_reason": "done",
                      "tool_calls": [{"iteration": 1,
                                      "tool": "qualify_candidates",
                                      "args": {}, "ok": True}]},
        }
        # session job must match the run's attachment_id ("att_1")
        f.session_jobs = [ok_job(attachment_id="att_1", **job_kw)]
        f.job = ok_job(**job_kw)
        f.results = ok_results()
        f.artifacts = std_artifacts()
        return f

    def _fake_without_tool(self):
        f = self._fake_with_tool()
        f.chat_reply["agent"]["tool_calls"] = [
            {"iteration": 1, "tool": "get_qualification_readiness",
             "args": {}, "ok": True}]
        return f

    def test_strict_chat_success_exit0(self):
        f = self._fake_with_tool()
        h = Harness(self.tmp, f)
        code, ev = h.run(base_args(mode="chat", tmpdir=self.tmp,
                                    chat_prompt="Qualify the companies."))
        self.assertEqual(code, 0)
        self.assertTrue((ev / "chat_request.json").exists())
        self.assertTrue((ev / "chat_response.json").exists())
        self.assertTrue((ev / "tool_call.json").exists())

    def test_strict_chat_no_kb_ids_sent(self):
        f = self._fake_with_tool()
        h = Harness(self.tmp, f)
        h.run(base_args(mode="chat", tmpdir=self.tmp,
                        chat_prompt="Qualify the companies."))
        chat = [r for r in f.requests if "gateway/chat" in r[1]][0]
        body = json.loads(chat[2])
        self.assertNotIn("kb_ids", body)

    def test_chat_context_kb_sent_as_kb_ids(self):
        f = self._fake_with_tool()
        h = Harness(self.tmp, f)
        h.run(base_args(mode="chat", tmpdir=self.tmp,
                        chat_prompt="Qualify.",
                        chat_context_kb="cust_0007"))
        chat = [r for r in f.requests if "gateway/chat" in r[1]][0]
        body = json.loads(chat[2])
        self.assertEqual(body.get("kb_ids"), ["cust_0007"])

    def test_strict_chat_tool_not_triggered_exit2(self):
        f = self._fake_without_tool()
        h = Harness(self.tmp, f)
        code, ev = h.run(base_args(mode="chat", tmpdir=self.tmp,
                                   chat_prompt="Qualify."))
        self.assertEqual(code, 2)
        self.assertIn("outcome=CHAT_TOOL_NOT_TRIGGERED",
                      (ev / "execution.log").read_text())
        # no direct qualify POST happened
        self.assertFalse(any("crm/qualify" in r[1] and r[0] == "POST"
                             for r in f.requests))

    def test_fallback_disabled_by_default(self):
        f = self._fake_without_tool()
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(mode="chat", tmpdir=self.tmp,
                                  chat_prompt="Qualify."))
        self.assertEqual(code, 2)

    def test_fallback_enabled_exit9_on_success(self):
        f = self._fake_without_tool()
        h = Harness(self.tmp, f)
        code, ev = h.run(base_args(mode="chat", tmpdir=self.tmp,
                                   chat_prompt="Qualify.",
                                   allow_direct_fallback=True))
        self.assertEqual(code, 9)
        self.assertIn("outcome=CHAT_FAILED_DIRECT_FALLBACK_SUCCEEDED",
                      (ev / "execution.log").read_text())

    def test_fallback_and_strict_mutually_exclusive_exit6(self):
        f = self._fake_with_tool()
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(mode="chat", tmpdir=self.tmp,
                                   chat_prompt="Qualify.",
                                   allow_direct_fallback=True,
                                   strict_chat=True))
        self.assertEqual(code, 6)

    def test_chat_requires_exactly_one_prompt_exit6(self):
        f = self._fake_with_tool()
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(mode="chat", tmpdir=self.tmp,
                                   chat_prompt="a", chat_prompt_file="b"))
        self.assertEqual(code, 6)
        code, _ = h.run(base_args(mode="chat", tmpdir=self.tmp))
        self.assertEqual(code, 6)

    def test_chat_prompt_file_exact_bytes_unicode(self):
        prompt = "Qualify émigré companies — naïve résumé\nline2: 日本語\n"
        pfile = self.tmp / "prompt.txt"
        pfile.write_text(prompt, encoding="utf-8")
        f = self._fake_with_tool()
        h = Harness(self.tmp, f)
        code, ev = h.run(base_args(mode="chat", tmpdir=self.tmp,
                                   chat_prompt_file=str(pfile)))
        self.assertEqual(code, 0)
        chat = [r for r in f.requests if "gateway/chat" in r[1]][0]
        body = json.loads(chat[2])
        self.assertEqual(body["message"], prompt)
        stored = (ev / "prompt.txt").read_bytes()
        self.assertEqual(stored, pfile.read_bytes())
        self.assertEqual((ev / "prompt.sha256").read_text().strip(),
                         eq.sha256_bytes(pfile.read_bytes()))

    def test_chat_down_exit8(self):
        f = self._fake_with_tool()
        f.chat_reply = None  # 502
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(mode="chat", tmpdir=self.tmp,
                                  chat_prompt="Qualify."))
        self.assertEqual(code, 8)

    def test_context_kb_same_bindings_across_kbs(self):
        """Different context KBs must NOT change ACP/CCO bindings."""
        bindings = []
        for kb in (None, "cust_0007", "other_kb"):
            import tempfile
            tmp = Path(tempfile.mkdtemp())
            f = self._fake_with_tool()
            h = Harness(tmp, f)
            kw = {"chat_prompt": "Qualify."}
            if kb:
                kw["chat_context_kb"] = kb
            code, _ = h.run(base_args(mode="chat", tmpdir=tmp, **kw))
            self.assertEqual(code, 0)
            res = json.loads((h.ev / "results.json").read_text())
            bindings.append((
                res["global_cco"]["global_cco_id"],
                res["global_cco"]["source_hash"],
                res["acp_family"][0]["acp_id"],
            ))
        self.assertEqual(len(set(bindings)), 1)


class TestPolling(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def _base_fake(self):
        f = FakeHTTP()
        f.job = ok_job()
        f.results = ok_results()
        f.artifacts = std_artifacts()
        return f

    def test_timeout_exit4(self):
        f = self._base_fake()
        f.job = ok_job(state="EXTRACTING_CANDIDATES")
        h = Harness(self.tmp, f)
        with patch.object(eq, "POLL_INTERVAL_S", 0.01):
            code, _ = h.run(base_args(tmpdir=self.tmp, timeout=0.05))
        self.assertEqual(code, 4)

    def test_job_lost_404_exit3(self):
        f = self._base_fake()
        f.poll_sequence = [ok_job(state="RESEARCHING"), None]  # then 404
        h = Harness(self.tmp, f)
        with patch.object(eq, "POLL_INTERVAL_S", 0.01):
            code, ev = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 3)
        self.assertIn("outcome=JOB_LOST", (ev / "execution.log").read_text())
        js = json.loads((ev / "job_status.json").read_text())
        self.assertEqual(js["state"], "JOB_LOST")

    def test_transient_errors_recovered(self):
        f = self._base_fake()
        state = {"errs": 0}

        def call(method, url, body, headers):
            if method == "GET" and "/crm/jobs/" in url and "/results" not in url:
                if state["errs"] < 2:
                    state["errs"] += 1
                    return 502, b"tmp"
                f.job = ok_job()
            return f._route(method, url, body, headers)
        f.override = call
        h = Harness(self.tmp, f)
        with patch.object(eq, "POLL_INTERVAL_S", 0.01):
            code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 0)

    def test_repeated_transient_errors_job_lost(self):
        f = self._base_fake()

        def call(method, url, body, headers):
            if method == "GET" and "/crm/jobs/" in url and "/results" not in url:
                return 503, b"tmp"
            return f._route(method, url, body, headers)
        f.override = call
        h = Harness(self.tmp, f)
        with patch.object(eq, "POLL_INTERVAL_S", 0.01):
            code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 3)


class TestArtifacts(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def _fake(self, artifacts, job=None):
        f = FakeHTTP()
        f.job = job or ok_job()
        if not artifacts:
            f.job = dict(f.job)
            f.job["artifact_ids"] = []
        f.results = ok_results()
        f.artifacts = artifacts
        return f

    def test_invalid_xlsx_exit5(self):
        f = self._fake({"art_md": ("r.md", "text/markdown", make_md()),
                        "art_xlsx": ("r.xlsx", "application/vnd"
                      ".openxmlformats-officedocument.spreadsheetml.sheet",
                      b"not a zip at all")})
        h = Harness(self.tmp, f)
        code, ev = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 5)
        # invalid bytes preserved for forensics
        self.assertEqual((ev / "qualification_report.xlsx").read_bytes(),
                         b"not a zip at all")

    def test_html_as_xlsx_exit5(self):
        f = self._fake({"art_md": ("r.md", "text/markdown", make_md()),
                        "art_xlsx": ("r.xlsx", "application/vnd"
                      ".openxmlformats-officedocument.spreadsheetml.sheet",
                      b"<html><body>502 Bad Gateway</body></html>")})
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 5)

    def test_xlsx_missing_worksheet_exit5(self):
        f = self._fake({"art_md": ("r.md", "text/markdown", make_md()),
                        "art_xlsx": ("r.xlsx", "application/vnd"
                      ".openxmlformats-officedocument.spreadsheetml.sheet",
                      make_xlsx(("OnlyOne",)))})
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 5)

    def test_invalid_md_exit5(self):
        f = self._fake({"art_md": ("r.md", "text/markdown",
                                   b"<html><body>error</body></html>"),
                        "art_xlsx": ("r.xlsx", "application/vnd"
                      ".openxmlformats-officedocument.spreadsheetml.sheet",
                      make_xlsx())})
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 5)

    def test_empty_md_exit5(self):
        f = self._fake({"art_md": ("r.md", "text/markdown", b""),
                        "art_xlsx": ("r.xlsx", "application/vnd"
                      ".openxmlformats-officedocument.spreadsheetml.sheet",
                      make_xlsx())})
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 5)

    def test_non_utf8_md_exit5(self):
        f = self._fake({"art_md": ("r.md", "text/markdown",
                                   b"# \xff\xfe broken"),
                        "art_xlsx": ("r.xlsx", "application/vnd"
                      ".openxmlformats-officedocument.spreadsheetml.sheet",
                      make_xlsx())})
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 5)

    def test_zero_artifacts_exit5(self):
        f = self._fake({})
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 5)

    def test_artifact_hashes_written(self):
        f = self._fake(std_artifacts())
        h = Harness(self.tmp, f)
        code, ev = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 0)
        hashes = json.loads((ev / "artifact_hashes.json").read_text())
        self.assertEqual(
            hashes["qualification_report.xlsx"]["sha256"],
            eq.sha256_bytes(make_xlsx()))


class TestBindings(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def _fake(self, job=None, results=None):
        f = FakeHTTP()
        f.job = job or ok_job()
        f.results = results or ok_results()
        f.artifacts = std_artifacts()
        return f

    def test_wrong_session_binding_exit7(self):
        f = self._fake(job=ok_job(session_id="OTHER_SESSION"))
        h = Harness(self.tmp, f)
        code, ev = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 7)
        self.assertIn("outcome=BINDING_MISMATCH",
                      (ev / "execution.log").read_text())

    def test_wrong_attachment_binding_exit7(self):
        f = self._fake(job=ok_job(attachment_id="OTHER_ATT"))
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 7)

    def test_missing_acp_binding_exit7(self):
        j = ok_job()
        j["acp_id"] = None
        f = self._fake(job=j)
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 7)

    def test_cco_mismatch_job_vs_results_exit7(self):
        f = self._fake(results=ok_results(cco="cco_A"))
        f.job = ok_job(cco="cco_B")
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 7)

    def test_result_missing_cco_id_exit7(self):
        res = ok_results()
        res["results"][0].pop("global_cco_id")
        f = self._fake(results=res)
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 7)


class TestConfigAndEvidence(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def test_missing_workbook_exit6(self):
        f = FakeHTTP()
        h = Harness(self.tmp, f)
        code, _ = h.run(["--workbook", str(self.tmp / "nope.xlsx"),
                         "--out", str(self.tmp / "runs")])
        self.assertEqual(code, 6)

    def test_direct_mode_rejects_chat_prompt_exit6(self):
        f = FakeHTTP()
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp, chat_prompt="x"))
        self.assertEqual(code, 6)

    def test_direct_mode_rejects_chat_context_kb_exit6(self):
        f = FakeHTTP()
        h = Harness(self.tmp, f)
        code, _ = h.run(base_args(tmpdir=self.tmp, chat_context_kb="k"))
        self.assertEqual(code, 6)

    def test_workbook_hash_evidence(self):
        f = FakeHTTP()
        f.job = ok_job()
        f.results = ok_results()
        f.artifacts = std_artifacts()
        h = Harness(self.tmp, f)
        code, ev = h.run(base_args(tmpdir=self.tmp))
        self.assertEqual(code, 0)
        wb = (ev / "input_workbook.xlsx").read_bytes()
        self.assertEqual((ev / "input_workbook.sha256").read_text().strip(),
                         eq.sha256_bytes(wb))

    def test_run_manifest_fields(self):
        f = FakeHTTP()
        f.job = ok_job()
        f.results = ok_results()
        f.artifacts = std_artifacts()
        h = Harness(self.tmp, f)
        code, ev = h.run(base_args(tmpdir=self.tmp))
        m = json.loads((ev / "run_manifest.json").read_text())
        for key in ("run_id", "mode", "outcome", "exit_code", "gateway",
                    "workbook", "workbook_sha256", "prompt_sha256",
                    "session_id", "attachment_id", "job_id", "context_kb",
                    "evidence_dir"):
            self.assertIn(key, m)
        self.assertEqual(m["outcome"], "PASS")
        self.assertEqual(m["exit_code"], 0)
        self.assertEqual(m["job_id"], "job_1")
        self.assertEqual(m["attachment_id"], "att_1")

    def test_evidence_files_immutable(self):
        f = FakeHTTP()
        f.job = ok_job()
        f.results = ok_results()
        f.artifacts = std_artifacts()
        h = Harness(self.tmp, f)
        code, ev = h.run(base_args(tmpdir=self.tmp))
        mode = (ev / "input_workbook.xlsx").stat().st_mode
        self.assertEqual(mode & 0o222, 0, "evidence file must be read-only")

    def test_upload_is_session_scoped_no_ingestion(self):
        """The upload request must target the session attachments endpoint
        and the qualify request must reference session+attachment only —
        no KB ingestion endpoint is ever called."""
        f = FakeHTTP()
        f.job = ok_job()
        f.results = ok_results()
        f.artifacts = std_artifacts()
        h = Harness(self.tmp, f)
        h.run(base_args(tmpdir=self.tmp))
        urls = " ".join(r[1] for r in f.requests)
        self.assertIn("/api/v2/sessions/", urls)
        self.assertIn("/attachments", urls)
        # No ingestion endpoints touched
        self.assertNotIn("/ingest", urls)
        self.assertNotIn("/parse", urls)
        # Upload body is multipart with the workbook bytes
        up = [r for r in f.requests if r[0] == "POST" and "/attachments" in r[1]][0]
        self.assertIn(b"multipart/form-data", up[3]["Content-Type"].encode()
                      if isinstance(up[3]["Content-Type"], str)
                      else b"multipart/form-data")

    def test_exit_code_contract_documented(self):
        """Every outcome maps to its documented exit code."""
        expected = {
            "PASS": 0, "PASS_WITH_WARNINGS": 0,
            "QUALIFICATION_INCOMPLETE": 1, "REGRESSION_FAILED": 1,
            "CHAT_TOOL_NOT_TRIGGERED": 2,
            "JOB_FAILED": 3, "JOB_CANCELLED": 3, "JOB_EXPIRED": 3, "JOB_LOST": 3,
            "TIMEOUT": 4, "ARTIFACT_INVALID": 5, "CONFIGURATION_ERROR": 6,
            "BINDING_MISMATCH": 7, "INFRASTRUCTURE_ERROR": 8,
            "CHAT_FAILED_DIRECT_FALLBACK_SUCCEEDED": 9,
        }
        self.assertEqual(eq.EXIT_CODES, expected)


class TestUnitHelpers(unittest.TestCase):
    def test_validate_xlsx_rejects_garbage(self):
        self.assertTrue(eq.validate_xlsx(b"garbage", {}))

    def test_validate_xlsx_accepts_valid(self):
        self.assertEqual(eq.validate_xlsx(make_xlsx(), ok_results()), [])

    def test_validate_md_accepts_valid(self):
        self.assertEqual(eq.validate_markdown(make_md(), ok_results()), [])

    def test_validate_md_rejects_no_h1(self):
        self.assertTrue(eq.validate_markdown(b"just text\n", ok_results()))

    def test_verify_bindings_ok(self):
        run = {"session_id": "s1", "attachment_id": "a1"}
        job = ok_job(session_id="s1", attachment_id="a1")
        self.assertEqual(eq.verify_bindings(run, job, ok_results()), [])

    def test_verify_bindings_session_mismatch(self):
        run = {"session_id": "s1", "attachment_id": "a1"}
        job = ok_job(session_id="s2", attachment_id="a1")
        problems = eq.verify_bindings(run, job, ok_results())
        self.assertTrue(any("session_id" in p for p in problems))

    def test_build_chat_request_omits_kb_ids(self):
        req = eq.build_chat_request("s", "hello", ["a"], None)
        self.assertNotIn("kb_ids", req)
        req2 = eq.build_chat_request("s", "hello", ["a"], "kb1")
        self.assertEqual(req2["kb_ids"], ["kb1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
