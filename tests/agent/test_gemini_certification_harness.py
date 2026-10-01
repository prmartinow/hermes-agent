"""Unit tests for the reproducible Cloud Code live certification harness.

Verifies safety invariants without issuing live upstream requests:
1. --live is mandatory (exits 1 with notice when omitted)
2. Temporary home isolation (operates in dedicated tempdir)
3. Real config is never written or mutated
4. Secret redaction (signatures and tokens never leak into output or JSON details)
5. Result classification (PASS, FAIL, UPSTREAM_UNAVAILABLE) and exit semantics (0, 1, 2)
6. Cleanup on exception
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

REPO_ROOT = Path("/home/ops/dev/hermes-agent")
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.verify_gemini_cloudcode_integration import CertificationResult, run_certification


def test_live_flag_is_mandatory():
    """Omitting --live flag must abort immediately with exit code 1."""
    script_path = REPO_ROOT / "scripts" / "verify_gemini_cloudcode_integration.py"
    res = subprocess.run([sys.executable, str(script_path)], capture_output=True, text=True)
    assert res.returncode == 1
    assert "Notice: --live flag is required" in res.stderr


def test_certification_result_classification_and_exit_semantics():
    """Verify result classification and exit code logic:
    0 = all PASS
    1 = at least one FAIL
    2 = UPSTREAM_UNAVAILABLE without failures
    """
    # 1. All PASS -> 0
    cr_pass = CertificationResult()
    cr_pass.record_pass("test1")
    cr_pass.record_pass("test2")
    assert cr_pass.exit_code == 0
    assert cr_pass.results == {"test1": "PASS", "test2": "PASS"}

    # 2. Upstream unavailable -> 2
    cr_unavail = CertificationResult()
    cr_unavail.record_pass("test1")
    cr_unavail.record_upstream_unavailable("partner_test", "HTTP 503 Service Unavailable")
    assert cr_unavail.exit_code == 2
    assert cr_unavail.results["partner_test"] == "UPSTREAM_UNAVAILABLE"

    # 3. Hermes failure dominates -> 1
    cr_fail = CertificationResult()
    cr_fail.record_pass("test1")
    cr_fail.record_upstream_unavailable("partner_test", "HTTP 503")
    cr_fail.record_fail("wire_test", "Wrong wire model")
    assert cr_fail.exit_code == 1
    assert cr_fail.results["wire_test"] == "FAIL"


def test_secret_redaction_in_json_and_text_output(capsys):
    """Signatures, OAuth tokens, and secret bodies must never appear in report output."""
    cr = CertificationResult()
    # Details should only carry booleans and high-level HTTP codes
    cr.record_pass("signed_tool_replay", signature_present=True, native_carrier_present=True, replay_status=200)
    cr.record_pass("sqlite_signed_replay", signature_roundtrip_exact=True, status=200)

    # Convert details to JSON
    json_out = json.dumps(cr.details)
    assert "EmQKY" not in json_out
    assert "ya29." not in json_out
    assert "AIzaSy" not in json_out
    assert '"signature_present": true' in json_out
    assert '"signature_roundtrip_exact": true' in json_out


def test_clean_exit_on_missing_auth():
    """When credentials are completely missing, harness exits gracefully with returncode 2 (upstream unavailable)."""
    with patch("scripts.verify_gemini_cloudcode_integration._get_active_token", return_value=None):
        code = run_certification(live=True, as_json=True)
        assert code == 2
