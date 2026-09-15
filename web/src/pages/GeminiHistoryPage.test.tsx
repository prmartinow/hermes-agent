// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  getGeminiSessionHistories: vi.fn(),
  getGeminiAccountHistory: vi.fn(),
  getGeminiQuotaTimeline: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  api: apiMocks,
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

async function renderGeminiHistoryPage() {
  const { default: GeminiHistoryPage } = await import("./GeminiHistoryPage");
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter>
        <GeminiHistoryPage />
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

describe("GeminiHistoryPage Frontend", () => {
  it("renders page header and shows empty state when no sessions exist", async () => {
    apiMocks.getGeminiSessionHistories.mockResolvedValue({
      sessions: [],
      total: 0,
    });

    await renderGeminiHistoryPage();
    await waitFor(() => document.body.textContent?.includes("GEMINI ACCOUNT ROTATION HISTORY") ?? false);

    expect(document.body.textContent).toContain("GEMINI ACCOUNT ROTATION HISTORY");
    expect(document.body.textContent).toContain("No Gemini chat sessions found.");
    expect(apiMocks.getGeminiSessionHistories).toHaveBeenCalledTimes(1);
  });

  it("renders sessions table with Model column and populated session data", async () => {
    const mockSessions = [
      {
        session_id: "20260904_120000_test1",
        title: "Model Column Verification Session",
        is_subagent: false,
        model: "gemini-3.8-flash-high",
        current_account: "tnn@example.com",
        current_alias: "tnn",
        started_at: 1756000000,
        last_activity_at: 1756000500,
        message_count: 10,
        turns_count: 4,
        changes_count: 2,
        events: [
          {
            id: "turn_1",
            timestamp: "2026-09-04T12:00:00Z",
            turn_number: 1,
            event_type: "turn",
            to_alias: "tnn",
            to_account: "tnn@example.com",
            api_calls: 2,
            details: "Verify that model column displays properly",
          },
        ],
      },
    ];

    apiMocks.getGeminiSessionHistories.mockResolvedValue({
      sessions: mockSessions,
      total: 1,
    });

    await renderGeminiHistoryPage();
    await waitFor(() => document.body.textContent?.includes("Model Column Verification Session") ?? false);

    // Verify all table headers are rendered, especially MODEL
    const headers = Array.from(document.querySelectorAll("th")).map((th) => th.textContent?.trim());
    expect(headers).toContain("Chat");
    expect(headers).toContain("Type");
    expect(headers).toContain("Model");
    expect(headers).toContain("Acc");
    expect(headers).toContain("Turns");
    expect(headers).toContain("Rotations");
    expect(headers).toContain("Started");
    expect(headers).toContain("Last Activity");
    expect(headers).toContain("Action");

    // Verify row content
    expect(document.body.textContent).toContain("Model Column Verification Session");
    expect(document.body.textContent).toContain("20260904_120000_test1");
    expect(document.body.textContent).toContain("gemini-3.8-flash-high");
    expect(document.body.textContent).toContain("tnn");
    expect(document.body.textContent).toContain("User");

    // Verify Open Chat link exists
    const openLink = document.querySelector('a[href*="/chat?resume=20260904_120000_test1"]');
    expect(openLink).not.toBeNull();
    expect(openLink?.textContent).toContain("Open Chat");
  });

  it("expands a chat session to display detailed event timeline on click", async () => {
    const mockSessions = [
      {
        session_id: "20260904_120000_expanded",
        title: "Expandable Session",
        is_subagent: false,
        model: "gemini-3.8-flash-high",
        current_alias: "pm",
        started_at: 1756000000,
        last_activity_at: 1756000100,
        message_count: 2,
        turns_count: 1,
        changes_count: 0,
        events: [
          {
            id: "turn_1",
            timestamp: "2026-09-04T12:00:00Z",
            turn_number: 1,
            event_type: "turn",
            to_alias: "pm",
            to_account: "pm@example.com",
            api_calls: 1,
            details: "Inspect prompt event details",
          },
        ],
      },
    ];

    apiMocks.getGeminiSessionHistories.mockResolvedValue({
      sessions: mockSessions,
      total: 1,
    });

    await renderGeminiHistoryPage();
    await waitFor(() => document.body.textContent?.includes("Expandable Session") ?? false);

    // Click the main chat row to expand
    const row = document.querySelector("tr.cursor-pointer");
    expect(row).not.toBeNull();
    await act(async () => click(row));

    // Verify expanded sub-table details
    await waitFor(() => document.body.textContent?.includes("Inspect prompt event details") ?? false);
    expect(document.body.textContent).toContain("TURN #1");
    expect(document.body.textContent).toContain("pm");
    expect(document.body.textContent).toContain("(1 API call)");
  });

  it("filters subagents when SUB toggle is clicked", async () => {
    const mockSessions = [
      {
        session_id: "user-sess",
        title: "User Interactive Session",
        is_subagent: false,
        model: "gemini-3.8-flash-high",
        current_alias: "pm",
        started_at: 1756000000,
        last_activity_at: 1756000100,
        message_count: 2,
        turns_count: 1,
        changes_count: 0,
        events: [],
      },
      {
        session_id: "subagent-sess",
        title: "Background Subagent Session",
        is_subagent: true,
        model: "gemini-3.7-flash-high",
        current_alias: "tnn",
        started_at: 1756000000,
        last_activity_at: 1756000100,
        message_count: 2,
        turns_count: 1,
        changes_count: 0,
        events: [],
      },
    ];

    apiMocks.getGeminiSessionHistories.mockResolvedValue({
      sessions: mockSessions,
      total: 2,
    });

    await renderGeminiHistoryPage();
    await waitFor(() => document.body.textContent?.includes("User Interactive Session") ?? false);

    // By default, subagents are hidden
    expect(document.body.textContent).toContain("User Interactive Session");
    expect(document.body.textContent).not.toContain("Background Subagent Session");

    // Click SUB button
    const subButton = Array.from(document.querySelectorAll("button")).find(
      (b) => b.textContent?.trim() === "SUB"
    );
    expect(subButton).toBeDefined();
    await act(async () => click(subButton!));

    // Now subagent is visible
    await waitFor(() => document.body.textContent?.includes("Background Subagent Session") ?? false);
    expect(document.body.textContent).toContain("Background Subagent Session");
    expect(document.body.textContent).toContain("Sub");
  });

  it("sorts table rows when clicking the Model column header", async () => {
    const mockSessions = [
      {
        session_id: "sess-b",
        title: "Beta Session",
        is_subagent: false,
        model: "gemini-3.7-flash-high",
        current_alias: "pm",
        started_at: 1756000000,
        last_activity_at: 1756000100,
        message_count: 2,
        turns_count: 1,
        changes_count: 0,
        events: [],
      },
      {
        session_id: "sess-a",
        title: "Alpha Session",
        is_subagent: false,
        model: "gemini-3.8-flash-high",
        current_alias: "tnn",
        started_at: 1756000000,
        last_activity_at: 1756000100,
        message_count: 2,
        turns_count: 1,
        changes_count: 0,
        events: [],
      },
    ];

    apiMocks.getGeminiSessionHistories.mockResolvedValue({
      sessions: mockSessions,
      total: 2,
    });

    await renderGeminiHistoryPage();
    await waitFor(() => document.body.textContent?.includes("Alpha Session") ?? false);

    // Find the Model th
    const modelHeader = Array.from(document.querySelectorAll("th")).find(
      (th) => th.textContent?.includes("Model")
    );
    expect(modelHeader).toBeDefined();

    // Click to sort by Model ascending
    await act(async () => click(modelHeader!));

    const rowsAfterSort = Array.from(document.querySelectorAll("tbody tr")).map(
      (r) => r.textContent
    );
    // 3.7 should appear before 3.8 in ascending order
    const firstRowIndex37 = rowsAfterSort.findIndex((t) => t?.includes("gemini-3.7-flash-high"));
    const firstRowIndex38 = rowsAfterSort.findIndex((t) => t?.includes("gemini-3.8-flash-high"));
    expect(firstRowIndex37).toBeLessThan(firstRowIndex38);
  });

  it("renders error state when API request fails", async () => {
    apiMocks.getGeminiSessionHistories.mockRejectedValue(
      new Error("Network timeout loading session histories")
    );

    await renderGeminiHistoryPage();
    await waitFor(
      () => document.body.textContent?.includes("Network timeout loading session histories") ?? false
    );

    expect(document.body.textContent).toContain("Network timeout loading session histories");
  });
});
