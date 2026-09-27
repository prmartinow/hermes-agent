// @vitest-environment jsdom
import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";

vi.hoisted(() => {
  process.env.NODE_ENV = "test";
});

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { ReactNode } from "react";
import CodexUsageHistoryPanel from "./CodexUsageHistoryPanel";
import type { CodexUsageHistoryResponse } from "../lib/api";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const apiMocks = vi.hoisted(() => ({
  getCodexUsageHistory: vi.fn(),
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

beforeEach(() => {
  apiMocks.getCodexUsageHistory.mockReset();
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
        }),
      );

      await render(<CodexUsageHistoryPanel mode="history" />);

      expect(container.textContent).toContain("Loading OpenAI Codex usage & quota data");
      expect(container.querySelector(".animate-spin")).not.toBeNull();

      await act(async () => {
        resolvePromise!(createBaseMockResponse());
      });
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

  describe("Zero vs Null differentiation", () => {
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

      apiMocks.getCodexUsageHistory.mockResolvedValue(response);
      await render(<CodexUsageHistoryPanel mode="timeline" />);
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
  });

  describe("Provider basis points (bps) conversion and styling thresholds", () => {
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

      apiMocks.getCodexUsageHistory.mockResolvedValue(response);
      await render(<CodexUsageHistoryPanel mode="timeline" />);
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
  });

  describe("Incomplete and Approximate indicators", () => {
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

      apiMocks.getCodexUsageHistory.mockResolvedValue(response);
      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-usage-panel"]')));

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

      apiMocks.getCodexUsageHistory.mockResolvedValue(response);
      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-usage-panel"]')));

      expect(container.textContent).toContain("Coverage Complete");
      expect(container.textContent).not.toContain("Approximate Data");
    });
  });

  describe("Unavailable, Error, and Empty states", () => {
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

    it("renders Plan Limit Error banner when timeline status is invalid_response", async () => {
      const response = createBaseMockResponse({
        plan_limit_history: {
          status: "invalid_response",
          error: "Corrupted schema in plan windows payload",
          data: null,
        },
      });

      apiMocks.getCodexUsageHistory.mockResolvedValue(response);
      await render(<CodexUsageHistoryPanel mode="timeline" />);
      await waitFor(() => Boolean(container.querySelector('[data-testid="codex-usage-panel"]')));

      expect(container.textContent).toContain("Plan Limit Error (invalid_response)");
      expect(container.textContent).toContain("Corrupted schema in plan windows payload");
    });

    it("renders top-level API error banner when request promise rejects", async () => {
      apiMocks.getCodexUsageHistory.mockRejectedValue(new Error("Connection refused: 503 Service Unavailable"));

      await render(<CodexUsageHistoryPanel mode="history" />);
      await waitFor(() => container.textContent?.includes("Connection refused") ?? false);

      expect(container.textContent).toContain("Connection refused: 503 Service Unavailable");
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
  });

  describe("Mode switching, unmount, and abort signals", () => {
    it("passes AbortSignal to api.getCodexUsageHistory and aborts on unmount", async () => {
      let passedSignal: AbortSignal | undefined;
      apiMocks.getCodexUsageHistory.mockImplementation((opts?: { signal?: AbortSignal }) => {
        passedSignal = opts?.signal;
        return new Promise(() => {}); // never resolves
      });

      await render(<CodexUsageHistoryPanel mode="history" />);

      expect(apiMocks.getCodexUsageHistory).toHaveBeenCalledWith(
        expect.objectContaining({ days: 7 }),
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

    it("clears data and re-fetches when mode prop changes", async () => {
      apiMocks.getCodexUsageHistory.mockResolvedValue(createBaseMockResponse());

      await render(<CodexUsageHistoryPanel mode="history" />);
      await waitFor(() => container.textContent?.includes("Daily Token Usage") ?? false);
      expect(apiMocks.getCodexUsageHistory).toHaveBeenCalledTimes(1);

      // Switch mode to timeline
      await act(async () => {
        root?.render(<CodexUsageHistoryPanel mode="timeline" />);
      });

      await waitFor(() => container.textContent?.includes("Plan Quota Timeline") ?? false);
      expect(apiMocks.getCodexUsageHistory).toHaveBeenCalledTimes(2);
    });
  });
});
