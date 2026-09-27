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
    expect(apiMocks.getGeminiSessionHistories).toHaveBeenCalledWith({
      limit: 50,
      offset: 0,
      include_subagents: false,
      scope: "all",
    });
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

  it("filters subagents when SUB toggle is clicked and requests include_subagents", async () => {
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
    expect(apiMocks.getGeminiSessionHistories).toHaveBeenLastCalledWith({
      limit: 50,
      offset: 0,
      include_subagents: false,
      scope: "all",
    });

    // Click SUB button
    const subButton = Array.from(document.querySelectorAll("button")).find(
      (b) => b.textContent?.trim() === "SUB"
    );
    expect(subButton).toBeDefined();
    await act(async () => click(subButton!));

    // Now subagent is visible and API requested with include_subagents: true
    await waitFor(() => document.body.textContent?.includes("Background Subagent Session") ?? false);
    expect(document.body.textContent).toContain("Background Subagent Session");
    expect(document.body.textContent).toContain("Sub");
    expect(apiMocks.getGeminiSessionHistories).toHaveBeenLastCalledWith({
      limit: 50,
      offset: 0,
      include_subagents: true,
      scope: "all",
    });
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

  it("handles offset pagination with previous and next buttons", async () => {
    const mockSessionsPage1 = Array.from({ length: 50 }, (_, i) => ({
      session_id: `sess-${i + 1}`,
      title: `Conversation #${i + 1}`,
      is_subagent: false,
      model: "gemini-2.5-pro",
      current_alias: "acc1",
      started_at: 1756000000 - i * 100,
      last_activity_at: 1756000000 - i * 100,
      message_count: 2,
      turns_count: 1,
      changes_count: 0,
      events: [],
    }));

    const mockSessionsPage2 = Array.from({ length: 25 }, (_, i) => ({
      session_id: `sess-${i + 51}`,
      title: `Conversation #${i + 51}`,
      is_subagent: false,
      model: "gemini-2.5-pro",
      current_alias: "acc1",
      started_at: 1756000000 - (i + 50) * 100,
      last_activity_at: 1756000000 - (i + 50) * 100,
      message_count: 2,
      turns_count: 1,
      changes_count: 0,
      events: [],
    }));

    apiMocks.getGeminiSessionHistories.mockImplementation(async (options?: { offset?: number }) => {
      if (options?.offset === 50) {
        return {
          sessions: mockSessionsPage2,
          total: 75,
          offset: 50,
          limit: 50,
          has_more: false,
        };
      }
      return {
        sessions: mockSessionsPage1,
        total: 75,
        offset: 0,
        limit: 50,
        has_more: true,
      };
    });

    await renderGeminiHistoryPage();
    await waitFor(() => document.body.textContent?.includes("Conversation #1") ?? false);

    // Check pagination bar text
    expect(document.body.textContent).toContain("Showing 1–50 of 75 conversations");
    expect(document.body.textContent).toContain("Page 1 of 2");

    const prevButton = document.querySelector('button[aria-label="Previous page"]') as HTMLButtonElement;
    const nextButton = document.querySelector('button[aria-label="Next page"]') as HTMLButtonElement;
    expect(prevButton).not.toBeNull();
    expect(nextButton).not.toBeNull();
    expect(prevButton.disabled).toBe(true);
    expect(nextButton.disabled).toBe(false);

    // Click Next
    await act(async () => click(nextButton));
    await waitFor(() => document.body.textContent?.includes("Conversation #51") ?? false);

    expect(document.body.textContent).toContain("Showing 51–75 of 75 conversations");
    expect(document.body.textContent).toContain("Page 2 of 2");
    expect(prevButton.disabled).toBe(false);
    expect(nextButton.disabled).toBe(true);

    // Click Previous
    await act(async () => click(prevButton));
    await waitFor(() => document.body.textContent?.includes("Conversation #1") ?? false);

    expect(document.body.textContent).toContain("Showing 1–50 of 75 conversations");
    expect(document.body.textContent).toContain("Page 1 of 2");
  });

  it("renders turn model and provider including mixed and Unknown without sess.model fallback", async () => {
    const mockSessions = [
      {
        session_id: "prov-sess-1",
        title: "Model Provenance Verification",
        is_subagent: false,
        model: "session-fallback-model-do-not-use",
        current_alias: "acc1",
        started_at: 1756000000,
        last_activity_at: 1756000100,
        message_count: 6,
        turns_count: 3,
        changes_count: 0,
        events: [
          {
            id: "turn_1",
            timestamp: "2026-09-04T12:00:00Z",
            turn_number: 1,
            event_type: "turn",
            details: "Turn with explicit single model and provider",
            model: "gemini-2.5-pro",
            models: ["gemini-2.5-pro"],
            provider: "gemini-oauth",
            providers: ["gemini-oauth"],
            model_provenance: "message_metadata",
          },
          {
            id: "turn_2",
            timestamp: "2026-09-04T12:01:00Z",
            turn_number: 2,
            event_type: "turn",
            details: "Turn with mixed models and providers",
            model: "mixed",
            models: ["gemini-2.5-pro", "gemini-2.0-flash"],
            provider: "mixed",
            providers: ["gemini-oauth", "openai"],
            model_provenance: "mixed",
          },
          {
            id: "turn_3",
            timestamp: "2026-09-04T12:02:00Z",
            turn_number: 3,
            event_type: "turn",
            details: "Turn without model metadata (legacy/unknown)",
            model: null,
            provider: null,
            model_provenance: "unknown",
          },
        ],
      },
    ];

    apiMocks.getGeminiSessionHistories.mockResolvedValue({
      sessions: mockSessions,
      total: 1,
    });

    await renderGeminiHistoryPage();
    await waitFor(() => document.body.textContent?.includes("Model Provenance Verification") ?? false);

    // Click to expand
    const row = document.querySelector("tr.cursor-pointer");
    expect(row).not.toBeNull();
    await act(async () => click(row));

    await waitFor(() => document.body.textContent?.includes("Turn with explicit single model") ?? false);

    // Verify sub-table headers include Model and Provider
    const subHeaders = Array.from(document.querySelectorAll("table.bg-black\\/40 th")).map((th) =>
      th.textContent?.trim()
    );
    expect(subHeaders).toContain("Model");
    expect(subHeaders).toContain("Provider");

    // Turn 1 assertions
    expect(document.body.textContent).toContain("gemini-2.5-pro");
    expect(document.body.textContent).toContain("gemini-oauth");

    // Turn 2 assertions: mixed model and mixed provider
    expect(document.body.textContent).toContain("mixed");

    // Turn 3 assertions: Unknown model and provider, NOT sess.model
    expect(document.body.textContent).toContain("Unknown");
    // Ensure "session-fallback-model-do-not-use" only appears in the parent session Model column and is NEVER rendered in the unknown turn's sub-table cell
    const turnCells = Array.from(document.querySelectorAll("table.bg-black\\/40 td")).map(
      (td) => td.textContent?.trim()
    );
    expect(turnCells).toContain("Unknown");
    expect(turnCells).toContain("gemini-2.5-pro");
    expect(turnCells).toContain("gemini-oauth");
    expect(turnCells).toContain("mixed");
    expect(turnCells).not.toContain("session-fallback-model-do-not-use");
  });
});
