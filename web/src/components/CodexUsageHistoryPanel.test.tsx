// @vitest-environment jsdom
import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";

vi.hoisted(() => {
  process.env.NODE_ENV = "test";
});

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { ReactNode } from "react";
import CodexUsageHistoryPanel from "./CodexUsageHistoryPanel";
import type {
  CodexQuotaTimelineResponse,
  CodexUsageHistoryResponse,
} from "../lib/api";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const apiMocks = vi.hoisted(() => ({
  getCodexUsageHistory: vi.fn(),
  getCodexQuotaTimeline: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  api: apiMocks,
}));

vi.mock("@/lib/api", () => ({
  api: apiMocks,
}));

let container: HTMLDivElement;
let root: Root | null = null;

async function render(ui: ReactNode) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(ui);
  });
}

async function waitFor(cond: () => boolean, timeoutMs = 4000) {
  const start = Date.now();
  while (!cond()) {
    if (Date.now() - start > timeoutMs) {
      throw new Error("waitFor: condition timed out");
    }
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
  }
}

function createBaseMockResponse(overrides?: Partial<CodexUsageHistoryResponse>): CodexUsageHistoryResponse {
  return {
    provider: "openai-codex",
    fetched_at: "2026-09-27T10:00:00Z",
    account_id: "org-test-123",
    daily_token_usage_breakdown: {
      status: "ok",
      error: null,
      data: {
        data_freshness_ts: "2026-09-27T09:55:00Z",
        units: "tokens",
        days: [
          {
            date: "2026-09-26",
            models: [
              {
                model: "gpt-5-codex",
                cached_text_input_tokens: 1500,
                uncached_text_input_tokens: 3000,
                text_output_tokens: 800,
                total_tokens: 5300,
              },
            ],
          },
        ],
      },
    },
    plan_limit_history: {
      status: "ok",
      error: null,
      data: {
        data_as_of: "2026-09-27T09:55:00Z",
        coverage_start: "2026-09-20T00:00:00Z",
        coverage_complete: true,
        approximate: false,
        periods: [
          {
            starts_at: "2026-09-26T00:00:00Z",
            ends_at: "2026-09-27T00:00:00Z",
            window_minutes: 1440,
            plan_type: "team-standard",
            used_basis_points: 4500,
            accounting_complete: true,
          },
        ],
      },
    },
    ...overrides,
  };
}

function createBaseMockTimelineResponse(overrides?: Partial<CodexQuotaTimelineResponse>): CodexQuotaTimelineResponse {
  return {
    provider: "openai-codex",
    days: 7,
    attribution_status: "correlated_only",
    external_usage_possible: true,
    activity_scope: "profile_local_activity",
    confirmed_same_account: false,
    rows: [
      {
        id: 1,
        observation_id: "obs_1",
        observed_at: 1774780800,
        time_label: "2026-09-27 10:00:00",
        account_id: "org-test-123",
        status: "ok",
        error_code: null,
        plan_type: "team-standard",
        primary_used_percent: 45.0,
        primary_reset_at: 1774798800,
        primary_window_seconds: 18000,
        secondary_used_percent: 20.0,
        secondary_reset_at: 1775385600,
        secondary_window_seconds: 604800,
        created_at: 1774780801,
      },
    ],
    snapshots: [
      {
        id: 1,
        observation_id: "obs_1",
        observed_at: 1774780800,
        time_label: "2026-09-27 10:00:00",
        account_id: "org-test-123",
        status: "ok",
        error_code: null,
        plan_type: "team-standard",
        primary_used_percent: 45.0,
        primary_reset_at: 1774798800,
        primary_window_seconds: 18000,
        secondary_used_percent: 20.0,
        secondary_reset_at: 1775385600,
        secondary_window_seconds: 604800,
        created_at: 1774780801,
      },
    ],
    intervals: [
      {
        account_id: "org-test-123",
        window_id: "primary",
        start_time: 1774777200,
        end_time: 1774780800,
        duration_seconds: 3600,
        start_used_percent: 40.0,
        end_used_percent: 45.0,
        delta_used_percent: 5.0,
        start_reset_at: 1774798800,
        end_reset_at: 1774798800,
        reset_at_changed: false,
        kind: "usage",
        status: "ok",
        description: "Depletion observed",
        checkpoint_delta: {
          account_id: "org-test-123",
          start_checkpoint_id: "chk_1",
          end_checkpoint_id: "chk_2",
          start_observed_at: 1774777200,
          end_observed_at: 1774780800,
          status: "ok",
          has_baseline: true,
          has_discontinuity: false,
          attribution_status: "correlated_only",
          external_usage_possible: true,
          activity_scope: "profile_local_activity",
          confirmed_same_account: false,
          total_delta: {
            api_call_count: 3,
            input_tokens: 1200,
            output_tokens: 450,
            cache_read_tokens: 300,
            cache_write_tokens: 50,
            cache_tokens: 350,
            total_tokens: 1650,
          },
          known_delta: {
            api_call_count: 3,
            input_tokens: 1200,
            output_tokens: 450,
            cache_read_tokens: 300,
            cache_write_tokens: 50,
            cache_tokens: 350,
            total_tokens: 1650,
          },
          discontinuity_reasons: [],
          session_deltas: [
            {
              session_id: "sess_alpha_12345",
              model: "gpt-5-codex",
              task: "codegen",
              status: "ok",
              is_discontinuity: false,
              has_baseline: true,
              activity_scope: "profile_local_activity",
              confirmed_same_account: false,
              baseline_counters: {
                api_call_count: 10,
                input_tokens: 5000,
                output_tokens: 2000,
                cache_read_tokens: 1000,
                cache_write_tokens: 200,
                cache_tokens: 1200,
                total_tokens: 7000,
              },
              current_counters: {
                api_call_count: 13,
                input_tokens: 6200,
                output_tokens: 2450,
                cache_read_tokens: 1300,
                cache_write_tokens: 250,
                cache_tokens: 1550,
                total_tokens: 8650,
              },
              delta_counters: {
                api_call_count: 3,
                input_tokens: 1200,
                output_tokens: 450,
                cache_read_tokens: 300,
                cache_write_tokens: 50,
                cache_tokens: 350,
                total_tokens: 1650,
              },
              reason: null,
            },
          ],
        },
      },
    ],
    checkpoint_deltas: [],
    ...overrides,
  };
}

beforeEach(() => {
  apiMocks.getCodexUsageHistory.mockReset();
  apiMocks.getCodexQuotaTimeline.mockReset();
  apiMocks.getCodexUsageHistory.mockResolvedValue(createBaseMockResponse());
  apiMocks.getCodexQuotaTimeline.mockResolvedValue(createBaseMockTimelineResponse());
});

afterEach(async () => {
  if (root) {
    await act(async () => {
      root?.unmount();
    });
    root = null;
  }
  container?.remove();
});

describe("CodexUsageHistoryPanel", () => {
  describe("Loading and basic header rendering", () => {
    it("renders loading spinner while request is unresolved", async () => {
      let resolvePromise: (value: CodexUsageHistoryResponse) => void;
      apiMocks.getCodexUsageHistory.mockReturnValue(
        new Promise<CodexUsageHistoryResponse>((resolve) => {
          resolvePromise = resolve;
        })
      );

      await render(<CodexUsageHistoryPanel mode="history" />);
      expect(container.textContent).toContain("Loading OpenAI Codex");

      await act(async () => {
        resolvePromise(createBaseMockResponse());
      });
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-usage-panel"] table')));
      expect(container.textContent).not.toContain("Loading OpenAI Codex");
    });

    it("renders top-level scope banner, account ID, and mode badge once loaded", async () => {
      apiMocks.getCodexUsageHistory.mockResolvedValue(createBaseMockResponse());

      await render(<CodexUsageHistoryPanel mode="history" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-usage-panel"] table')));

      expect(container.textContent).toContain("OpenAI Codex");
      expect(container.textContent).toContain("Daily Token Usage");
      expect(container.textContent).toContain("Scope:");
      expect(container.textContent).toContain("Account-wide (not Hermes-session-specific)");
      expect(container.textContent).toContain("org-test-123");
      expect(container.textContent).toContain("tokens");
    });
  });

  describe("History mode: Daily Token Usage Breakdown", () => {
    it("renders daily token usage breakdown table with model rows", async () => {
      apiMocks.getCodexUsageHistory.mockResolvedValue(createBaseMockResponse());
      await render(<CodexUsageHistoryPanel mode="history" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-usage-panel"] table')));

      expect(apiMocks.getCodexUsageHistory).toHaveBeenCalledWith(
        expect.objectContaining({ days: 7 })
      );

      const table = container.querySelector("table");
      expect(table).not.toBeNull();
      const headers = Array.from(table!.querySelectorAll("th")).map((th) => th.textContent?.trim());
      expect(headers).toEqual(["Date", "Model", "Cached Input", "Uncached Input", "Output", "Provider Total"]);

      const cells = Array.from(table!.querySelectorAll("tbody tr td")).map((td) => td.textContent?.trim());
      expect(cells[0]).toBe("2026-09-26");
      expect(cells[1]).toBe("gpt-5-codex");
      expect(cells[2]).toBe("1,500");
      expect(cells[3]).toBe("3,000");
      expect(cells[4]).toBe("800");
      expect(cells[5]).toBe("5,300");
    });

    it("renders empty model days with em-dash row in history mode", async () => {
      const response = createBaseMockResponse({
        daily_token_usage_breakdown: {
          status: "ok",
          error: null,
          data: {
            data_freshness_ts: "2026-09-27T09:55:00Z",
            units: "tokens",
            days: [
              {
                date: "2026-09-20",
                models: [],
              },
            ],
          },
        },
      });

      apiMocks.getCodexUsageHistory.mockResolvedValue(response);
      await render(<CodexUsageHistoryPanel mode="history" />);
      await waitFor(() => Boolean(container.querySelector("tbody tr")));

      const cells = Array.from(container.querySelectorAll("tbody tr td")).map((c) => c.textContent?.trim());
      expect(cells[0]).toBe("2026-09-20");
      expect(cells[1]).toBe("—");
      expect(cells[2]).toBe("—");
    });

    it("renders 0 tokens as '0' and null/undefined tokens as '—' in history mode", async () => {
      const response = createBaseMockResponse({
        daily_token_usage_breakdown: {
          status: "ok",
          error: null,
          data: {
            data_freshness_ts: "2026-09-27T09:55:00Z",
            units: "tokens",
            days: [
              {
                date: "2026-09-25",
                models: [
                  {
                    model: "codex-zero-model",
                    cached_text_input_tokens: 0,
                    uncached_text_input_tokens: 0,
                    text_output_tokens: 0,
                    total_tokens: 0,
                  },
                  {
                    model: "codex-null-model",
                    cached_text_input_tokens: null,
                    uncached_text_input_tokens: null,
                    text_output_tokens: null,
                    total_tokens: null,
                  },
                ],
              },
            ],
          },
        },
      });

      apiMocks.getCodexUsageHistory.mockResolvedValue(response);
      await render(<CodexUsageHistoryPanel mode="history" />);
      await waitFor(() => Boolean(container.querySelector("tbody tr")));

      const rows = container.querySelectorAll("tbody tr");
      expect(rows.length).toBe(2);

      // Row 0: zero values must be "0", not "—"
      const zeroRowCells = Array.from(rows[0].querySelectorAll("td")).map((c) => c.textContent?.trim());
      expect(zeroRowCells[1]).toBe("codex-zero-model");
      expect(zeroRowCells[2]).toBe("0");
      expect(zeroRowCells[3]).toBe("0");
      expect(zeroRowCells[4]).toBe("0");
      expect(zeroRowCells[5]).toBe("0");

      // Row 1: null values must be "—"
      const nullRowCells = Array.from(rows[1].querySelectorAll("td")).map((c) => c.textContent?.trim());
      expect(nullRowCells[1]).toBe("codex-null-model");
      expect(nullRowCells[2]).toBe("—");
      expect(nullRowCells[3]).toBe("—");
      expect(nullRowCells[4]).toBe("—");
      expect(nullRowCells[5]).toBe("—");
    });

    it("falls back to 'unknown' for null or empty account_id and units", async () => {
      const response = createBaseMockResponse({
        account_id: null,
        daily_token_usage_breakdown: {
          status: "ok",
          error: null,
          data: {
            data_freshness_ts: "2026-09-27T09:55:00Z",
            units: null,
            days: [],
          },
        },
      });

      apiMocks.getCodexUsageHistory.mockResolvedValue(response);
      await render(<CodexUsageHistoryPanel mode="history" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-usage-panel"]')));

      const text = container.textContent ?? "";
      expect(text).toContain("unknown");
    });

    it("renders empty message when history days is empty", async () => {
      const response = createBaseMockResponse({
        daily_token_usage_breakdown: {
          status: "ok",
          error: null,
          data: {
            data_freshness_ts: "2026-09-27T09:55:00Z",
            units: "tokens",
            days: [],
          },
        },
      });

      apiMocks.getCodexUsageHistory.mockResolvedValue(response);
      await render(<CodexUsageHistoryPanel mode="history" />);
      await waitFor(() => container.textContent?.includes("No token usage recorded") ?? false);

      expect(container.textContent).toContain("No token usage recorded for the past 7 days.");
    });

    it("renders Feature Unavailable banner for history breakdown", async () => {
      const response = createBaseMockResponse({
        daily_token_usage_breakdown: {
          status: "unavailable",
          error: "Account tier does not support daily breakdown telemetry",
          data: null,
        },
      });

      apiMocks.getCodexUsageHistory.mockResolvedValue(response);
      await render(<CodexUsageHistoryPanel mode="history" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-usage-panel"]')));

      expect(container.textContent).toContain("Feature Unavailable");
      expect(container.textContent).toContain("Daily token usage breakdown is unavailable or unsupported");
      expect(container.textContent).toContain("Account tier does not support daily breakdown telemetry");
    });

    it("renders Usage Error banner when history status is error", async () => {
      const response = createBaseMockResponse({
        daily_token_usage_breakdown: {
          status: "error",
          error: "Upstream rate limit from telemetry service",
          data: null,
        },
      });

      apiMocks.getCodexUsageHistory.mockResolvedValue(response);
      await render(<CodexUsageHistoryPanel mode="history" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-usage-panel"]')));

      expect(container.textContent).toContain("Usage Error (error)");
      expect(container.textContent).toContain("Upstream rate limit from telemetry service");
    });
  });

  describe("Timeline mode: Persisted Codex Quota Timeline and Correlation", () => {
    it("fetches GET /api/codex/quota-timeline?days=7&window_id=primary on mount and renders data", async () => {
      const timelineRes = createBaseMockTimelineResponse();
      apiMocks.getCodexQuotaTimeline.mockResolvedValue(timelineRes);

      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-interval-card"]')));

      // Verified exact endpoint parameters (no db_path ever)
      expect(apiMocks.getCodexQuotaTimeline).toHaveBeenCalledWith(
        expect.objectContaining({
          days: 7,
          window_id: "primary",
        })
      );
      const callArgs = apiMocks.getCodexQuotaTimeline.mock.calls[0][0];
      expect(callArgs.db_path).toBeUndefined();

      // Verified Scope label exact text
      expect(container.textContent).toContain(
        "Profile-local activity; account match unverified; external usage possible"
      );

      // Verified Account Quota Delta callout (clearly account-level, NOT per-session charge)
      expect(container.textContent).toContain(
        "Account Quota Delta (Account-level, NOT per-session charge):"
      );
      expect(container.textContent).toContain("+5.00%");
      expect(container.textContent).toContain("40.0% → 45.0%");

      // Verified session delta row
      const sessionRow = container.querySelector('[data-testid="session-delta-row"]');
      expect(sessionRow).not.toBeNull();
      expect(sessionRow!.textContent).toContain("sess_alpha_12345");
      expect(sessionRow!.textContent).toContain("gpt-5-codex");
      expect(sessionRow!.textContent).toContain("codegen");
      expect(sessionRow!.textContent).toContain("3"); // calls
      expect(sessionRow!.textContent).toContain("350"); // cached input
      expect(sessionRow!.textContent).toContain("1,200"); // uncached input
      expect(sessionRow!.textContent).toContain("450"); // output
      expect(sessionRow!.textContent).toContain("OK");
    });

    it("renders empty state when intervals are empty", async () => {
      const timelineRes = createBaseMockTimelineResponse({
        intervals: [],
        snapshots: [],
      });
      apiMocks.getCodexQuotaTimeline.mockResolvedValue(timelineRes);

      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-timeline-empty"]')));

      expect(container.textContent).toContain(
        "No observed quota intervals recorded for the past 7 days."
      );
    });

    it("renders error state when quota timeline fetch fails", async () => {
      apiMocks.getCodexQuotaTimeline.mockRejectedValue(
        new Error("Connection refused: 503 Service Unavailable")
      );

      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-timeline-error"]')));

      expect(container.textContent).toContain("Connection refused: 503 Service Unavailable");
    });

    it("renders observation gap interval with gap badge and unknown delta notice", async () => {
      const gapRes = createBaseMockTimelineResponse({
        intervals: [
          {
            account_id: "org-test-123",
            window_id: "primary",
            start_time: 1774777200,
            end_time: 1774780800,
            duration_seconds: 3600,
            start_used_percent: 40.0,
            end_used_percent: 40.0,
            delta_used_percent: null,
            start_reset_at: 1774798800,
            end_reset_at: 1774798800,
            reset_at_changed: false,
            kind: "gap",
            status: "gap",
            description: "Observation gap or collection error (gap)",
            checkpoint_delta: {
              account_id: "org-test-123",
              start_checkpoint_id: "chk_1",
              end_checkpoint_id: "chk_2",
              start_observed_at: 1774777200,
              end_observed_at: 1774780800,
              status: "unknown",
              has_baseline: false,
              has_discontinuity: true,
              total_delta: null,
              known_delta: {
                api_call_count: 0,
                input_tokens: 0,
                output_tokens: 0,
                cache_read_tokens: 0,
                cache_write_tokens: 0,
                cache_tokens: 0,
                total_tokens: 0,
              },
              discontinuity_reasons: ["Interval is an observation gap or error; usage delta is unknown"],
              session_deltas: [],
            },
          },
        ],
      });

      apiMocks.getCodexQuotaTimeline.mockResolvedValue(gapRes);
      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="interval-gap"]')));

      expect(container.textContent).toContain("Observation Gap");
      expect(container.textContent).toContain(
        "Observation gap or collection error; usage delta is unknown."
      );
      expect(container.querySelector('[data-testid="delta-unknown"]')?.textContent).toContain("— (Unknown)");
    });

    it("renders quota reset or replenishment interval with reset badge and explanation", async () => {
      const resetRes = createBaseMockTimelineResponse({
        intervals: [
          {
            account_id: "org-test-123",
            window_id: "primary",
            start_time: 1774777200,
            end_time: 1774780800,
            duration_seconds: 3600,
            start_used_percent: 85.0,
            end_used_percent: 10.0,
            delta_used_percent: null,
            start_reset_at: 1774798800,
            end_reset_at: 1774816800,
            reset_at_changed: true,
            kind: "reset_or_replenishment",
            status: "reset_or_replenishment",
            description: "Quota reset occurred",
            checkpoint_delta: {
              account_id: "org-test-123",
              start_checkpoint_id: "chk_1",
              end_checkpoint_id: "chk_2",
              start_observed_at: 1774777200,
              end_observed_at: 1774780800,
              status: "reset_or_replenishment",
              has_baseline: true,
              has_discontinuity: true,
              total_delta: null,
              known_delta: {
                api_call_count: 0,
                input_tokens: 0,
                output_tokens: 0,
                cache_read_tokens: 0,
                cache_write_tokens: 0,
                cache_tokens: 0,
                total_tokens: 0,
              },
              discontinuity_reasons: ["Quota reset occurred"],
              session_deltas: [],
            },
          },
        ],
      });

      apiMocks.getCodexQuotaTimeline.mockResolvedValue(resetRes);
      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="interval-reset"]')));

      expect(container.textContent).toContain("Quota Reset / Replenishment");
      expect(container.textContent).toContain(
        "Quota reset or replenishment occurred over interval; quota delta is not regular depletion."
      );
      expect(container.textContent).toContain("Reset Shifted");
    });

    it("handles missing baseline without masquerading lifetime counter totals as deltas", async () => {
      const missingBaselineRes = createBaseMockTimelineResponse({
        intervals: [
          {
            account_id: "org-test-123",
            window_id: "primary",
            start_time: 1774777200,
            end_time: 1774780800,
            duration_seconds: 3600,
            start_used_percent: 20.0,
            end_used_percent: 25.0,
            delta_used_percent: 5.0,
            start_reset_at: 1774798800,
            end_reset_at: 1774798800,
            reset_at_changed: false,
            kind: "usage",
            status: "ok",
            description: "Usage observed",
            checkpoint_delta: {
              account_id: "org-test-123",
              start_checkpoint_id: null,
              end_checkpoint_id: "chk_2",
              start_observed_at: null,
              end_observed_at: 1774780800,
              status: "missing_baseline",
              has_baseline: false,
              has_discontinuity: true,
              total_delta: null,
              known_delta: {
                api_call_count: 0,
                input_tokens: 0,
                output_tokens: 0,
                cache_read_tokens: 0,
                cache_write_tokens: 0,
                cache_tokens: 0,
                total_tokens: 0,
              },
              discontinuity_reasons: ["Start checkpoint missing baseline"],
              session_deltas: [
                {
                  session_id: "sess_missing_base",
                  model: "gpt-5-codex",
                  task: "refactor",
                  status: "missing_baseline",
                  is_discontinuity: true,
                  has_baseline: false,
                  baseline_counters: null,
                  current_counters: {
                    api_call_count: 50,
                    input_tokens: 99999, // LIFETIME COUNTER - must NOT appear as delta!
                    output_tokens: 44444,
                    cache_read_tokens: 11111,
                    cache_write_tokens: 2222,
                    cache_tokens: 13333,
                    total_tokens: 144443,
                  },
                  delta_counters: null, // delta is unknown
                  reason: "No baseline endpoint found in retention window",
                },
              ],
            },
          },
        ],
      });

      apiMocks.getCodexQuotaTimeline.mockResolvedValue(missingBaselineRes);
      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="missing-baseline-badge"]')));

      const sessionRow = container.querySelector('[data-testid="session-delta-row"]');
      expect(sessionRow).not.toBeNull();
      // Crucial test: lifetime counter 99,999 or 99999 MUST NOT be masqueraded as delta
      expect(sessionRow!.textContent).not.toContain("99,999");
      expect(sessionRow!.textContent).not.toContain("99999");
      expect(sessionRow!.textContent).not.toContain("44,444");
      expect(sessionRow!.textContent).toContain("—");
      expect(sessionRow!.textContent).toContain("Missing Baseline");

      expect(container.textContent).toContain(
        "Interval activity delta total is unknown (missing baseline). Lifetime counter totals are not masqueraded as deltas."
      );
    });

    it("renders multiple sessions and models accurately within the same interval", async () => {
      const multiRes = createBaseMockTimelineResponse({
        intervals: [
          {
            account_id: "org-test-123",
            window_id: "primary",
            start_time: 1774777200,
            end_time: 1774780800,
            duration_seconds: 3600,
            start_used_percent: 10.0,
            end_used_percent: 18.0,
            delta_used_percent: 8.0,
            start_reset_at: 1774798800,
            end_reset_at: 1774798800,
            reset_at_changed: false,
            kind: "usage",
            status: "ok",
            description: "Concurrent usage",
            checkpoint_delta: {
              account_id: "org-test-123",
              start_checkpoint_id: "chk_1",
              end_checkpoint_id: "chk_2",
              start_observed_at: 1774777200,
              end_observed_at: 1774780800,
              status: "ok",
              has_baseline: true,
              has_discontinuity: false,
              total_delta: {
                api_call_count: 5,
                input_tokens: 3500,
                output_tokens: 1200,
                cache_read_tokens: 800,
                cache_write_tokens: 100,
                cache_tokens: 900,
                total_tokens: 4700,
              },
              known_delta: {
                api_call_count: 5,
                input_tokens: 3500,
                output_tokens: 1200,
                cache_read_tokens: 800,
                cache_write_tokens: 100,
                cache_tokens: 900,
                total_tokens: 4700,
              },
              discontinuity_reasons: [],
              session_deltas: [
                {
                  session_id: "sess_111",
                  model: "gpt-5-codex",
                  task: "main",
                  status: "ok",
                  is_discontinuity: false,
                  has_baseline: true,
                  baseline_counters: null,
                  current_counters: null,
                  delta_counters: {
                    api_call_count: 3,
                    input_tokens: 2000,
                    output_tokens: 700,
                    cache_read_tokens: 500,
                    cache_write_tokens: 50,
                    cache_tokens: 550,
                    total_tokens: 2700,
                  },
                  reason: null,
                },
                {
                  session_id: "sess_222",
                  model: "codex-mini",
                  task: "summarize",
                  status: "ok",
                  is_discontinuity: false,
                  has_baseline: true,
                  baseline_counters: null,
                  current_counters: null,
                  delta_counters: {
                    api_call_count: 2,
                    input_tokens: 1500,
                    output_tokens: 500,
                    cache_read_tokens: 300,
                    cache_write_tokens: 50,
                    cache_tokens: 350,
                    total_tokens: 2000,
                  },
                  reason: null,
                },
              ],
            },
          },
        ],
      });

      apiMocks.getCodexQuotaTimeline.mockResolvedValue(multiRes);
      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelectorAll('[data-testid="session-delta-row"]').length === 2));

      const rows = container.querySelectorAll('[data-testid="session-delta-row"]');
      expect(rows[0].textContent).toContain("sess_111");
      expect(rows[0].textContent).toContain("gpt-5-codex");
      expect(rows[0].textContent).toContain("main");
      expect(rows[0].textContent).toContain("2,000");

      expect(rows[1].textContent).toContain("sess_222");
      expect(rows[1].textContent).toContain("codex-mini");
      expect(rows[1].textContent).toContain("summarize");
      expect(rows[1].textContent).toContain("1,500");
    });

    it("switches window between primary and secondary via window selector buttons", async () => {
      apiMocks.getCodexQuotaTimeline.mockResolvedValue(createBaseMockTimelineResponse());
      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-window-secondary"]')));

      expect(apiMocks.getCodexQuotaTimeline).toHaveBeenCalledWith(
        expect.objectContaining({ window_id: "primary" })
      );

      const secBtn = container.querySelector('[data-testid="codex-window-secondary"]') as HTMLButtonElement;
      await act(async () => {
        secBtn.click();
      });

      await waitFor(() => apiMocks.getCodexQuotaTimeline.mock.calls.length >= 2);
      expect(apiMocks.getCodexQuotaTimeline).toHaveBeenLastCalledWith(
        expect.objectContaining({ window_id: "secondary", days: 7 })
      );

      // Verify no db_path was sent on secondary window request
      const secCallArgs = apiMocks.getCodexQuotaTimeline.mock.calls[1][0];
      expect(secCallArgs.db_path).toBeUndefined();
    });

    it("toggles between Persisted Quota Timeline and Provider Plan History (On-demand)", async () => {
      apiMocks.getCodexQuotaTimeline.mockResolvedValue(createBaseMockTimelineResponse());
      apiMocks.getCodexUsageHistory.mockResolvedValue(createBaseMockResponse());

      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-source-provider-plan"]')));

      // In persisted mode, quota timeline view is active
      expect(container.querySelector('[data-testid="codex-quota-timeline-view"]')).not.toBeNull();

      // Click Provider Plan History toggle
      const planBtn = container.querySelector('[data-testid="codex-source-provider-plan"]') as HTMLButtonElement;
      await act(async () => {
        planBtn.click();
      });

      await waitFor(() => Boolean(container.querySelector("table")));
      expect(apiMocks.getCodexUsageHistory).toHaveBeenCalledWith(
        expect.objectContaining({ days: 7 })
      );
      expect(container.textContent).toContain("Window Range");
      expect(container.textContent).toContain("Duration");
      expect(container.textContent).toContain("Plan Type");

      // Click back to Persisted Quota Timeline toggle
      const persistedBtn = container.querySelector('[data-testid="codex-source-persisted"]') as HTMLButtonElement;
      await act(async () => {
        persistedBtn.click();
      });

      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-quota-timeline-view"]')));
      expect(container.querySelector('[data-testid="codex-quota-timeline-view"]')).not.toBeNull();
    });
  });

  describe("Provider Plan History (On-demand) view details", () => {
    it("renders zero and null used_basis_points and accounting_complete without crashing", async () => {
      const response = createBaseMockResponse({
        plan_limit_history: {
          status: "ok",
          error: null,
          data: {
            data_as_of: "2026-09-27T09:55:00Z",
            coverage_start: "2026-09-20T00:00:00Z",
            coverage_complete: true,
            approximate: false,
            periods: [
              {
                starts_at: "2026-09-26T00:00:00Z",
                ends_at: "2026-09-27T00:00:00Z",
                window_minutes: 0,
                plan_type: "team-standard",
                used_basis_points: 0,
                accounting_complete: false,
              },
              {
                starts_at: "2026-09-25T00:00:00Z",
                ends_at: "2026-09-26T00:00:00Z",
                window_minutes: null,
                plan_type: null,
                used_basis_points: null,
                accounting_complete: null,
              },
            ],
          },
        },
      });

      apiMocks.getCodexUsageHistory.mockResolvedValue(response);
      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-source-provider-plan"]')));

      const planBtn = container.querySelector('[data-testid="codex-source-provider-plan"]') as HTMLButtonElement;
      await act(async () => {
        planBtn.click();
      });

      await waitFor(() => Boolean(container.querySelector("tbody tr")));
      const rows = container.querySelectorAll("tbody tr");
      expect(rows.length).toBe(2);

      // Row 0: zero values
      const row0 = rows[0];
      expect(row0.textContent).toContain("0m");
      expect(row0.textContent).toContain("0%");
      expect(row0.textContent).toContain("Pending");

      // Row 1: null values
      const row1 = rows[1];
      expect(row1.textContent).toContain("—");
      expect(row1.textContent).not.toContain("Complete");
      expect(row1.textContent).not.toContain("Pending");
    });

    it("renders zero bps as '0%' with progress bar, null bps as '—' with no progress bar", async () => {
      const response = createBaseMockResponse({
        plan_limit_history: {
          status: "ok",
          error: null,
          data: {
            data_as_of: "2026-09-27T09:55:00Z",
            coverage_start: "2026-09-20T00:00:00Z",
            coverage_complete: true,
            approximate: false,
            periods: [
              {
                starts_at: "2026-09-26T00:00:00Z",
                ends_at: "2026-09-27T00:00:00Z",
                window_minutes: 0,
                plan_type: "free",
                used_basis_points: 0,
                accounting_complete: false,
              },
              {
                starts_at: "2026-09-25T00:00:00Z",
                ends_at: "2026-09-26T00:00:00Z",
                window_minutes: null,
                plan_type: null,
                used_basis_points: null,
                accounting_complete: null,
              },
            ],
          },
        },
      });

      apiMocks.getCodexQuotaTimeline.mockResolvedValue(createBaseMockTimelineResponse());
      apiMocks.getCodexUsageHistory.mockResolvedValue(response);

      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-source-provider-plan"]')));

      const planBtn = container.querySelector('[data-testid="codex-source-provider-plan"]') as HTMLButtonElement;
      await act(async () => {
        planBtn.click();
      });

      await waitFor(() => Boolean(container.querySelector("tbody tr")));

      const rows = container.querySelectorAll("tbody tr");
      expect(rows.length).toBe(2);

      // Row 0: zero values
      const row0 = rows[0];
      expect(row0.textContent).toContain("0m");
      expect(row0.textContent).toContain("0%");
      expect(row0.textContent).toContain("Pending"); // accounting_complete === false
      // Progress bar exists for zero (0% width)
      const bar0 = row0.querySelector('div[style*="width: 0%"]');
      expect(bar0).not.toBeNull();

      // Row 1: null values
      const row1 = rows[1];
      expect(row1.textContent).toContain("—");
      // accounting_complete === null renders "—"
      expect(row1.textContent).not.toContain("Complete");
      expect(row1.textContent).not.toContain("Pending");
      // Progress bar should not exist for null bps
      const barContainer = row1.querySelector(".w-24.bg-midground\\/20");
      expect(barContainer).toBeNull();
    });

    it("converts bps to percentage and applies correct color thresholds", async () => {
      const response = createBaseMockResponse({
        plan_limit_history: {
          status: "ok",
          error: null,
          data: {
            data_as_of: "2026-09-27T09:55:00Z",
            coverage_start: "2026-09-20T00:00:00Z",
            coverage_complete: true,
            approximate: false,
            periods: [
              {
                starts_at: "2026-09-26T00:00:00Z",
                ends_at: "2026-09-26T02:00:00Z",
                window_minutes: 120, // 2h (120m)
                plan_type: "pro",
                used_basis_points: 5000, // 50% -> normal text-foreground
                accounting_complete: true,
              },
              {
                starts_at: "2026-09-26T02:00:00Z",
                ends_at: "2026-09-26T04:00:00Z",
                window_minutes: 120,
                plan_type: "pro",
                used_basis_points: 7550, // 75.5% -> amber text-amber-400
                accounting_complete: true,
              },
              {
                starts_at: "2026-09-26T04:00:00Z",
                ends_at: "2026-09-26T06:00:00Z",
                window_minutes: 120,
                plan_type: "pro",
                used_basis_points: 9200, // 92% -> rose text-rose-400
                accounting_complete: true,
              },
            ],
          },
        },
      });

      apiMocks.getCodexQuotaTimeline.mockResolvedValue(createBaseMockTimelineResponse());
      apiMocks.getCodexUsageHistory.mockResolvedValue(response);

      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-source-provider-plan"]')));

      const planBtn = container.querySelector('[data-testid="codex-source-provider-plan"]') as HTMLButtonElement;
      await act(async () => {
        planBtn.click();
      });

      await waitFor(() => Boolean(container.querySelector("tbody tr")));

      const rows = container.querySelectorAll("tbody tr");
      expect(rows.length).toBe(3);

      // Period 1: 50%
      expect(rows[0].textContent).toContain("2h (120m)");
      expect(rows[0].textContent).toContain("50%");
      const span50 = rows[0].querySelector("td:nth-child(4) span.text-foreground");
      expect(span50?.textContent).toContain("50%");
      const bar50 = rows[0].querySelector(".bg-purple-500");
      expect(bar50).not.toBeNull();

      // Period 2: 75.5% (amber)
      expect(rows[1].textContent).toContain("75.5%");
      const span75 = rows[1].querySelector("span.text-amber-400");
      expect(span75?.textContent).toContain("75.5%");
      const bar75 = rows[1].querySelector(".bg-amber-500");
      expect(bar75).not.toBeNull();

      // Period 3: 92% (rose)
      expect(rows[2].textContent).toContain("92%");
      const span92 = rows[2].querySelector("span.text-rose-400");
      expect(span92?.textContent).toContain("92%");
      const bar92 = rows[2].querySelector(".bg-rose-500");
      expect(bar92).not.toBeNull();
    });

    it("renders Coverage Incomplete and Approximate Data badges in timeline mode", async () => {
      const response = createBaseMockResponse({
        plan_limit_history: {
          status: "ok",
          error: null,
          data: {
            data_as_of: "2026-09-27T09:55:00Z",
            coverage_start: "2026-09-20T00:00:00Z",
            coverage_complete: false,
            approximate: true,
            periods: [],
          },
        },
      });

      apiMocks.getCodexQuotaTimeline.mockResolvedValue(createBaseMockTimelineResponse());
      apiMocks.getCodexUsageHistory.mockResolvedValue(response);

      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-source-provider-plan"]')));

      const planBtn = container.querySelector('[data-testid="codex-source-provider-plan"]') as HTMLButtonElement;
      await act(async () => {
        planBtn.click();
      });

      await waitFor(() => container.textContent?.includes("Coverage Incomplete") ?? false);

      expect(container.textContent).toContain("Coverage Incomplete");
      expect(container.textContent).toContain("Approximate Data");
      expect(container.textContent).toContain("No plan limit periods recorded.");
    });

    it("renders Coverage Complete badge when coverage_complete is true", async () => {
      const response = createBaseMockResponse({
        plan_limit_history: {
          status: "ok",
          error: null,
          data: {
            data_as_of: "2026-09-27T09:55:00Z",
            coverage_start: "2026-09-20T00:00:00Z",
            coverage_complete: true,
            approximate: false,
            periods: [],
          },
        },
      });

      apiMocks.getCodexQuotaTimeline.mockResolvedValue(createBaseMockTimelineResponse());
      apiMocks.getCodexUsageHistory.mockResolvedValue(response);

      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-source-provider-plan"]')));

      const planBtn = container.querySelector('[data-testid="codex-source-provider-plan"]') as HTMLButtonElement;
      await act(async () => {
        planBtn.click();
      });

      await waitFor(() => container.textContent?.includes("Coverage Complete") ?? false);

      expect(container.textContent).toContain("Coverage Complete");
      expect(container.textContent).not.toContain("Approximate Data");
    });

    it("renders Plan Limit Error banner when timeline status is invalid_response", async () => {
      const response = createBaseMockResponse({
        plan_limit_history: {
          status: "invalid_response",
          error: "Corrupted schema in plan windows payload",
          data: null,
        },
      });

      apiMocks.getCodexQuotaTimeline.mockResolvedValue(createBaseMockTimelineResponse());
      apiMocks.getCodexUsageHistory.mockResolvedValue(response);

      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-source-provider-plan"]')));

      const planBtn = container.querySelector('[data-testid="codex-source-provider-plan"]') as HTMLButtonElement;
      await act(async () => {
        planBtn.click();
      });

      await waitFor(() => container.textContent?.includes("Plan Limit Error (invalid_response)") ?? false);

      expect(container.textContent).toContain("Plan Limit Error (invalid_response)");
      expect(container.textContent).toContain("Corrupted schema in plan windows payload");
    });
  });

  describe("API error and mode switching", () => {
    it("clears data and re-fetches when mode prop changes", async () => {
      apiMocks.getCodexUsageHistory.mockResolvedValue(createBaseMockResponse());
      apiMocks.getCodexQuotaTimeline.mockResolvedValue(createBaseMockTimelineResponse());

      await render(<CodexUsageHistoryPanel mode="history" />);
      await waitFor(() => container.textContent?.includes("Daily Token Usage") ?? false);
      expect(apiMocks.getCodexUsageHistory).toHaveBeenCalledTimes(1);

      // Switch mode to timeline
      await act(async () => {
        root?.render(<CodexUsageHistoryPanel mode="timeline" />);
      });

      await waitFor(() => container.textContent?.includes("Plan Quota Timeline") ?? false);
      expect(apiMocks.getCodexQuotaTimeline).toHaveBeenCalledTimes(1);
    });

    it("passes AbortSignal to api.getCodexQuotaTimeline and aborts on unmount", async () => {
      let receivedSignal: AbortSignal | undefined;
      apiMocks.getCodexQuotaTimeline.mockImplementation((opts?: { signal?: AbortSignal }) => {
        receivedSignal = opts?.signal;
        return new Promise(() => {}); // never resolves
      });

      await render(<CodexUsageHistoryPanel mode="timeline" />);
      expect(apiMocks.getCodexQuotaTimeline).toHaveBeenCalledWith(
        expect.objectContaining({ signal: expect.any(Object) })
      );
      expect(receivedSignal?.aborted).toBe(false);

      await act(async () => {
        root?.unmount();
        root = null;
      });
      expect(receivedSignal?.aborted).toBe(true);
    });

    it("renders top-level API error banner when request promise rejects", async () => {
      apiMocks.getCodexUsageHistory.mockRejectedValue(new Error("Connection refused: 503 Service Unavailable"));

      await render(<CodexUsageHistoryPanel mode="history" />);
      await waitFor(() => container.textContent?.includes("Connection refused") ?? false);

      expect(container.textContent).toContain("Connection refused: 503 Service Unavailable");
    });

    it("passes AbortSignal to api.getCodexUsageHistory and aborts on unmount", async () => {
      let passedSignal: AbortSignal | undefined;
      apiMocks.getCodexUsageHistory.mockImplementation((opts?: { signal?: AbortSignal }) => {
        passedSignal = opts?.signal;
        return new Promise(() => {}); // never resolves
      });

      await render(<CodexUsageHistoryPanel mode="history" />);

      expect(apiMocks.getCodexUsageHistory).toHaveBeenCalledWith(
        expect.objectContaining({ days: 7 })
      );
      expect(passedSignal).toBeDefined();
      expect(passedSignal?.aborted).toBe(false);

      // Unmount
      await act(async () => {
        root?.unmount();
        root = null;
      });

      expect(passedSignal?.aborted).toBe(true);
    });

    it("does not report error when request is aborted via AbortError", async () => {
      const abortError = new DOMException("The user aborted a request.", "AbortError");
      apiMocks.getCodexUsageHistory.mockRejectedValue(abortError);

      await render(<CodexUsageHistoryPanel mode="history" />);
      // Give ticks for promise rejection to process
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 30));
      });

      // No error banner should be displayed
      expect(container.textContent).not.toContain("AbortError");
      expect(container.textContent).not.toContain("Failed to load");
    });
  });
});
