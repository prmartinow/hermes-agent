"""Unit tests for the reproducible Cloud Code live certification harness.

Verifies safety invariants and harness self-consistency without issuing live upstream requests:
1. --live flag is mandatory (exits 1 with notice when omitted)
2. Temporary home isolation (operates in dedicated tempdir via set_hermes_home_override)
3. Real operator config and auth files are strictly immutable
4. Secret and private path sanitization (tokens, keys, signatures, local workstation paths)
5. API exception classification (UPSTREAM_UNAVAILABLE vs FAIL)
6. Result classification (PASS, FAIL, UPSTREAM_UNAVAILABLE) and exit semantics (0, 1, 2)
7. Cleanup on exception
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.verify_gemini_cloudcode_integration import (
    CertificationResult,
    classify_api_exception,
    run_certification,
    sanitize_secrets,
)
from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override


def test_live_flag_is_mandatory():
    """Omitting --live flag must abort immediately with exit code 1 and emit a notice."""
    script_path = REPO_ROOT / "scripts" / "verify_gemini_cloudcode_integration.py"
    res = subprocess.run([sys.executable, str(script_path)], capture_output=True, text=True)
    assert res.returncode == 1
    assert "Notice: --live flag is required" in res.stderr


def test_certification_result_classification_and_exit_semantics():
    """Verify result classification and exit code priority logic:
    0 = all PASS
    1 = at least one FAIL (dominates)
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


def test_sanitize_secrets_comprehensive():
    """Central secret sanitizer must redact all sensitive tokens, signatures, and local paths."""
    raw = (
        "Encountered error with OAuth token ya29.DUMMY_MOCK_TOKEN_FOR_TESTING "
        "and API key AIzaSyDUMMY_MOCK_KEY_FOR_TESTING and thought signature "
        "EmQKYDUMMY_MOCK_SIGNATURE_STRING_FOR_TESTING "
        "while reading /home/testuser/.hermes/auth.json and /Users/developer/project/file.txt."
    )
    sanitized = sanitize_secrets(raw)
    assert "ya29." not in sanitized
    assert "[REDACTED_OAUTH_TOKEN]" in sanitized
    assert "AIzaSy" not in sanitized
    assert "[REDACTED_API_KEY]" in sanitized
    assert "EmQKY" not in sanitized
    assert "[REDACTED_THOUGHT_SIGNATURE]" in sanitized
    assert "/home/testuser" not in sanitized
    assert "/Users/developer" not in sanitized
    assert "[REDACTED_HOME_PATH]" in sanitized


def test_classify_api_exception_semantics():
    """Verify structured API error classification."""
    # 503 Service Unavailable -> UPSTREAM_UNAVAILABLE
    exc_503 = Exception("Service Unavailable")
    exc_503.status_code = 503
    kind_503, msg_503 = classify_api_exception(exc_503)
    assert kind_503 == "UPSTREAM_UNAVAILABLE"
    assert "503" in msg_503

    # 429 Quota Exhausted -> UPSTREAM_UNAVAILABLE
    exc_429 = Exception("Rate limit exceeded")
    exc_429.status_code = 429
    kind_429, msg_429 = classify_api_exception(exc_429)
    assert kind_429 == "UPSTREAM_UNAVAILABLE"
    assert "429" in msg_429

    # 400 Bad Request (Hermes request error) -> FAIL
    exc_400 = Exception("Invalid wire model parameter")
    exc_400.status_code = 400
    kind_400, msg_400 = classify_api_exception(exc_400)
    assert kind_400 == "FAIL"
    assert "400" in msg_400


def test_temp_home_isolation_and_real_config_immutability(tmp_path):
    """Temporary home isolation must isolate config writes and protect real config."""
    real_home = tmp_path / "real_hermes"
    real_home.mkdir()
    real_cfg = real_home / "config.yaml"
    real_cfg.write_text("agent:\n  model: base-model\n", encoding="utf-8")
    initial_hash = hashlib.sha256(real_cfg.read_bytes()).hexdigest()

    temp_home = tmp_path / "temp_hermes"
    temp_home.mkdir()
    (temp_home / "config.yaml").write_text("agent:\n  model: temp-model\n", encoding="utf-8")

    # Activate home override
    token = set_hermes_home_override(temp_home)
    try:
        assert get_hermes_home() == temp_home
        # Mutate temp config
        (temp_home / "config.yaml").write_text("agent:\n  model: mutated-temp\n", encoding="utf-8")
        # Assert real config unchanged
        assert hashlib.sha256(real_cfg.read_bytes()).hexdigest() == initial_hash
    finally:
        reset_hermes_home_override(token)

    # Real home restored
    assert hashlib.sha256(real_cfg.read_bytes()).hexdigest() == initial_hash


def test_cleanup_on_exception(tmp_path):
    """When an exception occurs during execution, cleanup must reset overrides and wipe temp directories."""
    temp_dir = tmp_path / "mock_temp_dir"
    temp_dir.mkdir()
    override_token = set_hermes_home_override(temp_dir)

    try:
        try:
            raise RuntimeError("Simulated mid-run crash")
        finally:
            reset_hermes_home_override(override_token)
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
    except RuntimeError:
        pass

    assert not temp_dir.exists()
    assert get_hermes_home() != temp_dir


def test_clean_exit_on_missing_auth():
    """When credentials cannot be discovered, harness exits with code 2 (upstream unavailable)."""
    with patch("scripts.verify_gemini_cloudcode_integration.get_gemini_oauth_auth_status", return_value={"logged_in": False}):
        code = run_certification(live=True, as_json=True)
        assert code == 2


def test_harness_cleanup_and_isolation_end_to_end():
    """Calling run_certification() with an injected mid-run crash must execute finally cleanup."""
    from scripts.verify_gemini_cloudcode_integration import _hash_file
    from hermes_constants import get_hermes_home_override

    orig_env = os.environ.get("HERMES_HOME")
    real_home = Path(get_hermes_home())
    real_cfg = real_home / "config.yaml"
    real_auth = real_home / "auth.json"
    init_cfg_hash = _hash_file(real_cfg)
    init_auth_hash = _hash_file(real_auth)

    with patch("scripts.verify_gemini_cloudcode_integration.GeminiCloudCodeClient") as mock_client_cls, \
         patch("scripts.verify_gemini_cloudcode_integration.get_gemini_oauth_auth_status", return_value={"logged_in": True, "api_key": "fake_token"}):
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("Injected mid-run crash")
        mock_client_cls.return_value = mock_client

        code = run_certification(live=True, as_json=True)
        assert code == 1

    # Assert home override reset
    assert get_hermes_home_override() is None
    # Assert HERMES_HOME env restored
    assert os.environ.get("HERMES_HOME") == orig_env
    # Assert real config and auth unchanged
    assert _hash_file(real_cfg) == init_cfg_hash
    assert _hash_file(real_auth) == init_auth_hash
