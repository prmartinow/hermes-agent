// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  getGeminiQuotaTimeline: vi.fn(),
  getGeminiSessionHistories: vi.fn(),
  getGeminiAccountHistory: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: apiMocks,
}));

let container: HTMLDivElement;
let root: Root;
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

async function waitFor(cond: () => boolean, timeoutMs = 5000) {
  const start = Date.now();
  while (!cond()) {
    if (Date.now() - start > timeoutMs) throw new Error("waitFor: condition never became true");
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
  }
}

function click(el: Element | null) {
  if (!el) throw new Error("element not rendered");
  el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
}

async function renderGeminiQuotaTimelinePage() {
  const { default: GeminiQuotaTimelinePage } = await import("./GeminiQuotaTimelinePage");
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter>
        <GeminiQuotaTimelinePage />
      </MemoryRouter>
    );
  });
}

beforeEach(() => {
  for (const fn of Object.values(apiMocks)) fn.mockReset();
});

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
});

describe("GeminiQuotaTimelinePage Frontend", () => {
  it("renders page header and calls api with default 24h gemini model group", async () => {
    apiMocks.getGeminiQuotaTimeline.mockResolvedValue({
      timespan: "24h",
      model_group: "gemini",
      interval_minutes: 15,
      total_intervals: 1,
      generated_at: "2026-09-04T12:00:00Z",
      accounts_meta: [
        {
          account_id: 1,
          alias: "pm",
          email: "pm@example.com",
          logged_in: true,
          current_5h_pct: 95.0,
          current_5h_reset: "2h 10m",
          current_7d_pct: 98.0,
          current_rank: 1,
        },
        {
          account_id: 2,
          alias: "tnn",
          email: "tnn@example.com",
          logged_in: true,
          current_5h_pct: 80.0,
          current_5h_reset: "45m",
          current_7d_pct: 90.0,
          current_rank: 2,
        },
      ],
      intervals: [
        {
          epoch: 1756000000,
          timestamp: "2026-09-04T12:00:00Z",
          time_label: "12:00",
          date_label: "Sep 4",
          is_current: true,
          accounts: {
            "1": {
              account_id: 1,
              alias: "pm",
              cap_5h: 95.0,
              reset_5h: "2h 10m",
              cap_7d: 98.0,
              rank: 1,
              score: 3.8,
              turns: 5,
              logged_in: true,
            },
            "2": {
              account_id: 2,
              alias: "tnn",
              cap_5h: 80.0,
              reset_5h: "45m",
              cap_7d: 90.0,
              rank: 2,
              score: 3.2,
              turns: 2,
              logged_in: true,
            },
          },
        },
      ],
    });

    await renderGeminiQuotaTimelinePage();
    await waitFor(
      () =>
        document.body.textContent?.includes(
          "Gemini 5-Account Quota & Rank Timeline"
        ) ?? false
    );

    expect(document.body.textContent).toContain(
      "Gemini 5-Account Quota & Rank Timeline"
    );
    expect(apiMocks.getGeminiQuotaTimeline).toHaveBeenCalledWith({
      timespan: "24h",
      model_group: "gemini",
    });

    // Check account meta headers are rendered
    expect(document.body.textContent).toContain("pm");
    expect(document.body.textContent).toContain("tnn");

    // Check interval data rendered
        expect(document.body.textContent).toContain("95%");
    expect(document.body.textContent).toContain("80%");
  });

  it("switches model group to claude partner models on button click", async () => {
    apiMocks.getGeminiQuotaTimeline.mockResolvedValue({
      timespan: "24h",
      model_group: "gemini",
      interval_minutes: 15,
      total_intervals: 0,
      generated_at: "2026-09-04T12:00:00Z",
      accounts_meta: [],
      intervals: [],
    });

    await renderGeminiQuotaTimelinePage();
    await waitFor(
      () =>
        document.body.textContent?.includes(
          "Gemini 5-Account Quota & Rank Timeline"
        ) ?? false
    );

    // Find Claude & 3P Quota button
    const claudeButton = Array.from(document.querySelectorAll("button")).find(
      (b) => b.textContent?.includes("Claude & 3P Quota")
    );
    expect(claudeButton).toBeDefined();

    // Prepare mock for claude model group
    apiMocks.getGeminiQuotaTimeline.mockResolvedValue({
      timespan: "24h",
      model_group: "claude",
      interval_minutes: 15,
      total_intervals: 0,
      generated_at: "2026-09-04T12:00:00Z",
      accounts_meta: [],
      intervals: [],
    });

    await act(async () => click(claudeButton!));

    await waitFor(
      () =>
        apiMocks.getGeminiQuotaTimeline.mock.calls.some(
          (call) => call[0]?.model_group === "claude"
        )
    );

    expect(apiMocks.getGeminiQuotaTimeline).toHaveBeenCalledWith({
      timespan: "24h",
      model_group: "claude",
    });
  });

  it("displays error message when timeline API call fails", async () => {
    apiMocks.getGeminiQuotaTimeline.mockRejectedValue(
      new Error("Failed to load Gemini quota timeline: server down")
    );

    await renderGeminiQuotaTimelinePage();
    await waitFor(
      () =>
        document.body.textContent?.includes(
          "Failed to load Gemini quota timeline: server down"
        ) ?? false
    );

    expect(document.body.textContent).toContain(
      "Failed to load Gemini quota timeline: server down"
    );
  });
});
