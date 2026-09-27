import math
import pytest

from agent.codex_usage_history_schema import (
    normalize_token_history,
    normalize_plan_history,
    MAX_LIST_BOUND,
)


def test_token_history_shape():
    payload = {
        "data_freshness_ts": "2026-09-27T08:00:00Z",
        "units": "tokens",
        "data": [
            {
                "date": "2026-09-26",
                "models": [
                    {
                        "model": "gpt-5",
                        "cached_text_input_tokens": 100,
                        "uncached_text_input_tokens": 200,
                        "text_output_tokens": 50,
                        "total_tokens": 350,
                    }
                ],
            }
        ],
    }
    res = normalize_token_history(payload)
    assert res["data_freshness_ts"] == "2026-09-27T08:00:00Z"
    assert res["units"] == "tokens"
    assert len(res["days"]) == 1
    day = res["days"][0]
    assert day["date"] == "2026-09-26"
    assert len(day["models"]) == 1
    m = day["models"][0]
    assert m == {
        "model": "gpt-5",
        "cached_text_input_tokens": 100,
        "uncached_text_input_tokens": 200,
        "text_output_tokens": 50,
        "total_tokens": 350,
    }


def test_token_history_alternative_days_key():
    payload = {
        "days": [
            {
                "date": "2026-09-25",
                "models": [],
            }
        ]
    }
    res = normalize_token_history(payload)
    assert len(res["days"]) == 1
    assert res["days"][0]["date"] == "2026-09-25"
    assert res["days"][0]["models"] == []
    assert res["data_freshness_ts"] is None
    assert res["units"] is None


def test_token_history_sensitive_and_extra_fields_absent():
    payload = {
        "data_freshness_ts": "2026-09-27T08:00:00Z",
        "units": "tokens",
        "group_by": "day",
        "breakdown_by": ["model"],
        "account_id": "acc_sensitive_123",
        "user_id": "usr_sensitive_456",
        "data": [
            {
                "date": "2026-09-26",
                "product_surface_usage_values": {"cli": 42.0},
                "groups": [{"group_id": "grp-1", "tokens": 1000}],
                "attribution": {"source": "ide"},
                "models": [
                    {
                        "model": "gpt-5",
                        "speed": "fast",
                        "credits": 12.5,
                        "cost_usd": "0.15",
                        "users": 1,
                        "threads": 2,
                        "turns": 3,
                        "on_demand_credits": 0.0,
                        "cached_text_input_tokens": 100,
                        "uncached_text_input_tokens": 200,
                        "text_output_tokens": 50,
                        "total_tokens": 350,
                    }
                ],
            }
        ],
    }
    res = normalize_token_history(payload)
    assert "account_id" not in res
    assert "user_id" not in res
    assert "group_by" not in res
    assert "breakdown_by" not in res

    day = res["days"][0]
    assert "product_surface_usage_values" not in day
    assert "groups" not in day
    assert "attribution" not in day

    m = day["models"][0]
    for sensitive_key in ["speed", "credits", "cost_usd", "users", "threads", "turns", "on_demand_credits"]:
        assert sensitive_key not in m
    assert set(m.keys()) == {
        "model",
        "cached_text_input_tokens",
        "uncached_text_input_tokens",
        "text_output_tokens",
        "total_tokens",
    }


def test_token_history_null_vs_zero():
    # Test zero tokens: preserved as 0
    zero_payload = {
        "data": [
            {
                "date": "2026-09-26",
                "models": [
                    {
                        "model": "gpt-5",
                        "cached_text_input_tokens": 0,
                        "uncached_text_input_tokens": 0,
                        "text_output_tokens": 0,
                        "total_tokens": 0,
                    }
                ],
            }
        ]
    }
    res_zero = normalize_token_history(zero_payload)
    m_zero = res_zero["days"][0]["models"][0]
    assert m_zero["cached_text_input_tokens"] == 0
    assert m_zero["uncached_text_input_tokens"] == 0
    assert m_zero["text_output_tokens"] == 0
    assert m_zero["total_tokens"] == 0

    # Test null tokens: stay None
    null_payload = {
        "data": [
            {
                "date": "2026-09-26",
                "models": [
                    {
                        "model": "gpt-5",
                        "cached_text_input_tokens": None,
                        "uncached_text_input_tokens": None,
                        "text_output_tokens": None,
                        "total_tokens": None,
                    }
                ],
            }
        ]
    }
    res_null = normalize_token_history(null_payload)
    m_null = res_null["days"][0]["models"][0]
    assert m_null["cached_text_input_tokens"] is None
    assert m_null["uncached_text_input_tokens"] is None
    assert m_null["text_output_tokens"] is None
    assert m_null["total_tokens"] is None

    # Test missing tokens: stay None
    missing_tokens_payload = {
        "data": [
            {
                "date": "2026-09-26",
                "models": [
                    {"model": "gpt-5"}
                ],
            }
        ]
    }
    res_missing = normalize_token_history(missing_tokens_payload)
    m_missing = res_missing["days"][0]["models"][0]
    assert m_missing["cached_text_input_tokens"] is None
    assert m_missing["uncached_text_input_tokens"] is None
    assert m_missing["text_output_tokens"] is None
    assert m_missing["total_tokens"] is None

    # Test models absent/null -> [] (not evidence zero)
    models_null_payload = {
        "data": [
            {"date": "2026-09-26", "models": None},
            {"date": "2026-09-27"},
        ]
    }
    res_models_null = normalize_token_history(models_null_payload)
    assert res_models_null["days"][0]["models"] == []
    assert res_models_null["days"][1]["models"] == []


def test_token_history_invalid_values():
    # Payload not dict
    with pytest.raises(ValueError, match="Payload must be a dictionary"):
        normalize_token_history("not-a-dict")
    with pytest.raises(ValueError, match="Payload must be a dictionary"):
        normalize_token_history(None)

    # Missing data / days
    with pytest.raises(ValueError, match="Payload missing required 'data' or 'days' field"):
        normalize_token_history({})

    # Data not a list
    with pytest.raises(ValueError, match="Days must be a list"):
        normalize_token_history({"data": "not-a-list"})

    # Day not dict
    with pytest.raises(ValueError, match="Day entry at index 0 must be a dictionary"):
        normalize_token_history({"data": ["not-a-day-dict"]})

    # Day missing date
    with pytest.raises(ValueError, match="missing valid 'date' string"):
        normalize_token_history({"data": [{"date": 12345}]})
    with pytest.raises(ValueError, match="missing valid 'date' string"):
        normalize_token_history({"data": [{}]})

    # Day models not a list
    with pytest.raises(ValueError, match="'models' must be a list or null"):
        normalize_token_history({"data": [{"date": "2026-09-26", "models": "invalid"}]})

    # Model not dict
    with pytest.raises(ValueError, match="Model at day 0 index 0 must be a dictionary"):
        normalize_token_history({"data": [{"date": "2026-09-26", "models": ["invalid"]}]})

    # Model missing model name
    with pytest.raises(ValueError, match="must have 'model' string"):
        normalize_token_history({"data": [{"date": "2026-09-26", "models": [{}]}]})
    with pytest.raises(ValueError, match="must have 'model' string"):
        normalize_token_history({"data": [{"date": "2026-09-26", "models": [{"model": 123}]}]})

    # Tokens negative
    with pytest.raises(ValueError, match="finite and non-negative"):
        normalize_token_history({"data": [{"date": "2026-09-26", "models": [{"model": "m", "total_tokens": -5}]}]})

    # Tokens bool not numeric
    with pytest.raises(ValueError, match="must be an integer"):
        normalize_token_history({"data": [{"date": "2026-09-26", "models": [{"model": "m", "total_tokens": True}]}]})

    # Tokens non-integer float
    with pytest.raises(ValueError, match="must be an integer"):
        normalize_token_history({"data": [{"date": "2026-09-26", "models": [{"model": "m", "total_tokens": 12.34}]}]})

    # Tokens non-finite float
    with pytest.raises(ValueError, match="finite and non-negative"):
        normalize_token_history({"data": [{"date": "2026-09-26", "models": [{"model": "m", "total_tokens": float("inf")}]}]})

    # Boundary > 10000 rows
    oversized_days = [{"date": f"2026-01-{i:04d}", "models": []} for i in range(MAX_LIST_BOUND + 1)]
    with pytest.raises(ValueError, match="exceeds maximum bound of 10000 rows"):
        normalize_token_history({"data": oversized_days})


def test_plan_history_shape():
    payload = {
        "data_as_of": "2026-09-27T08:00:00Z",
        "coverage_start": "2026-09-20T00:00:00Z",
        "coverage_complete": True,
        "approximate": False,
        "periods": [
            {
                "id": "period-1",
                "starts_at": "2026-09-20T00:00:00Z",
                "ends_at": "2026-09-20T05:00:00Z",
                "window_minutes": 300,
                "plan_type": "pro",
                "used_basis_points": 150.5,
                "accounting_complete": True,
            }
        ],
    }
    res = normalize_plan_history(payload)
    assert res["data_as_of"] == "2026-09-27T08:00:00Z"
    assert res["coverage_start"] == "2026-09-20T00:00:00Z"
    assert res["coverage_complete"] is True
    assert res["approximate"] is False
    assert len(res["periods"]) == 1
    p = res["periods"][0]
    assert p == {
        "starts_at": "2026-09-20T00:00:00Z",
        "ends_at": "2026-09-20T05:00:00Z",
        "window_minutes": 300,
        "plan_type": "pro",
        "used_basis_points": 150.5,
        "accounting_complete": True,
    }


def test_plan_history_sensitive_and_breakdowns_omitted():
    payload = {
        "data_as_of": "2026-09-27T08:00:00Z",
        "coverage_start": "2026-09-20T00:00:00Z",
        "coverage_complete": True,
        "approximate": False,
        "boundary_tolerance_seconds": 60,
        "periods": [
            {
                "id": "internal-period-id-xyz",
                "starts_at": "2026-09-20T00:00:00Z",
                "ends_at": "2026-09-20T05:00:00Z",
                "window_minutes": 300,
                "plan_type": "pro",
                "used_basis_points": 500,
                "accounting_complete": True,
                "breakdowns": [
                    {
                        "dimension": "model",
                        "rows": [{"key": "gpt-5", "basis_points": 500}],
                    }
                ],
            }
        ],
    }
    res = normalize_plan_history(payload)
    assert "boundary_tolerance_seconds" not in res
    p = res["periods"][0]
    assert "id" not in p
    assert "breakdowns" not in p
    assert set(p.keys()) == {
        "starts_at",
        "ends_at",
        "window_minutes",
        "plan_type",
        "used_basis_points",
        "accounting_complete",
    }


def test_plan_history_null_vs_zero():
    # Test zero used_basis_points: preserved as 0
    zero_payload = {
        "periods": [
            {
                "starts_at": "2026-09-20T00:00:00Z",
                "ends_at": "2026-09-20T05:00:00Z",
                "window_minutes": 0,
                "used_basis_points": 0,
                "accounting_complete": False,
            }
        ]
    }
    res_zero = normalize_plan_history(zero_payload)
    p_zero = res_zero["periods"][0]
    assert p_zero["window_minutes"] == 0
    assert p_zero["used_basis_points"] == 0
    assert p_zero["accounting_complete"] is False

    # Test null values preserved as None
    null_payload = {
        "data_as_of": None,
        "coverage_start": None,
        "coverage_complete": None,
        "approximate": None,
        "periods": [
            {
                "starts_at": "2026-09-20T00:00:00Z",
                "ends_at": "2026-09-20T05:00:00Z",
                "window_minutes": None,
                "plan_type": None,
                "used_basis_points": None,
                "accounting_complete": None,
            }
        ],
    }
    res_null = normalize_plan_history(null_payload)
    assert res_null["data_as_of"] is None
    assert res_null["coverage_start"] is None
    assert res_null["coverage_complete"] is None
    assert res_null["approximate"] is None
    p_null = res_null["periods"][0]
    assert p_null["window_minutes"] is None
    assert p_null["plan_type"] is None
    assert p_null["used_basis_points"] is None
    assert p_null["accounting_complete"] is None


def test_plan_history_invalid_values():
    # Payload not dict
    with pytest.raises(ValueError, match="Payload must be a dictionary"):
        normalize_plan_history("not-a-dict")
    with pytest.raises(ValueError, match="Payload must be a dictionary"):
        normalize_plan_history(None)

    # Missing periods
    with pytest.raises(ValueError, match="missing required 'periods' field"):
        normalize_plan_history({})

    # Periods not list
    with pytest.raises(ValueError, match="Periods must be a list"):
        normalize_plan_history({"periods": 123})

    # Period not dict
    with pytest.raises(ValueError, match="Period at index 0 must be a dictionary"):
        normalize_plan_history({"periods": ["not-a-dict"]})

    # Missing starts_at / ends_at
    with pytest.raises(ValueError, match="missing valid 'starts_at' string"):
        normalize_plan_history({"periods": [{"ends_at": "2026-09-20T05:00:00Z"}]})
    with pytest.raises(ValueError, match="missing valid 'ends_at' string"):
        normalize_plan_history({"periods": [{"starts_at": "2026-09-20T00:00:00Z"}]})

    # Bool not numeric (used_basis_points = True)
    with pytest.raises(ValueError, match="must be a number"):
        normalize_plan_history({
            "periods": [{
                "starts_at": "2026-09-20T00:00:00Z",
                "ends_at": "2026-09-20T05:00:00Z",
                "used_basis_points": True,
            }]
        })

    # Numeric not bool (coverage_complete = 1)
    with pytest.raises(ValueError, match="must be a boolean"):
        normalize_plan_history({
            "coverage_complete": 1,
            "periods": [],
        })

    # Accounting complete not bool (accounting_complete = 0)
    with pytest.raises(ValueError, match="must be a boolean"):
        normalize_plan_history({
            "periods": [{
                "starts_at": "2026-09-20T00:00:00Z",
                "ends_at": "2026-09-20T05:00:00Z",
                "accounting_complete": 0,
            }]
        })

    # Negative quota/basis points
    with pytest.raises(ValueError, match="finite and non-negative"):
        normalize_plan_history({
            "periods": [{
                "starts_at": "2026-09-20T00:00:00Z",
                "ends_at": "2026-09-20T05:00:00Z",
                "used_basis_points": -1.0,
            }]
        })

    # Exceeds max rows
    oversized_periods = [
        {"starts_at": "2026-09-20T00:00:00Z", "ends_at": "2026-09-20T05:00:00Z"}
        for _ in range(MAX_LIST_BOUND + 1)
    ]
    with pytest.raises(ValueError, match="exceeds maximum bound of 10000 rows"):
        normalize_plan_history({"periods": oversized_periods})
