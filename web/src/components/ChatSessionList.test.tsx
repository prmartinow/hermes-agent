// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Ensure React knows this is an act environment
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

import { I18nProvider } from "@/i18n";
import type { SessionInfo } from "@/lib/api";
import { ChatSessionList } from "./ChatSessionList";

const mockSessions: SessionInfo[] = [
  {
    id: "session-1",
    title: "Research Paper Review",
    preview: "First turn preview text",
    model: "gemini-3.7-flash-high",
    started_at: Date.now() - 100000,
    ended_at: null,
    last_active: Date.now() - 60000,
    is_active: true,
    message_count: 14,
    total_message_count: 14,
    tool_call_count: 2,
    input_tokens: 1000,
    output_tokens: 500,
    source: "tui",
    account_alias: "prm",
  },
  {
    id: "session-2",
    title: "Untrusted Task",
    preview: "Second turn preview",
    model: "gemini-3.7-flash-high",
    started_at: Date.now() - 200000,
    ended_at: Date.now() - 120000,
    last_active: Date.now() - 120000,
    is_active: false,
    message_count: 5,
    total_message_count: 5,
    tool_call_count: 0,
    input_tokens: 200,
    output_tokens: 100,
    source: "cli",
    account_alias: null,
  },
  {
    id: "session-3",
    title: "CD-DOCI Tuning",
    preview: "Third turn preview",
    model: "gemini-3.7-flash-high",
    started_at: Date.now() - 500000,
    ended_at: null,
    last_active: Date.now() - 300000,
    is_active: false,
    message_count: 8,
    total_message_count: 20,
    tool_call_count: 4,
    input_tokens: 4000,
    output_tokens: 1200,
    source: "web",
    account_alias: "tnn",
  },
];

const apiMocks = vi.hoisted(() => ({
  getSessions: vi.fn(async () => ({
    sessions: mockSessions,
    total: mockSessions.length,
  })),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getSessions: apiMocks.getSessions,
  },
}));

let container: HTMLDivElement;
let root: Root;

async function render(ui: React.ReactNode) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter>
        <I18nProvider>{ui}</I18nProvider>
      </MemoryRouter>,
    );
  });
}

beforeEach(() => {
  apiMocks.getSessions.mockClear();
});

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
});

describe("ChatSessionList Subline & Account Alias Invariants", () => {
  it("renders the account alias exactly once per session item", async () => {
    await render(<ChatSessionList activeSessionId="session-1" />);

    await act(async () => {
      await Promise.resolve();
      await new Promise((r) => setTimeout(r, 20));
    });

    const sessionItems = container.querySelectorAll("button");
    const sessionRows = Array.from(sessionItems).filter((btn) =>
      btn.textContent?.includes("Research Paper Review") ||
      btn.textContent?.includes("Untrusted Task") ||
      btn.textContent?.includes("CD-DOCI Tuning")
    );
    expect(sessionRows.length).toBe(3);

    // Session 1 has account_alias = 'prm' -> exactly once
    const item1 = sessionRows[0];
    const text1 = item1.textContent || "";
    expect(text1).toContain("prm");
    const prmMatches = (text1.match(/\bprm\b/g) || []).length;
    expect(prmMatches).toBe(1);
    expect(text1).not.toContain("prm · prm");

    // Session 2 has account_alias = null -> no alias rendered
    const item2 = sessionRows[1];
    const text2 = item2.textContent || "";
    expect(text2).not.toContain("prm");
    expect(text2).not.toContain("tnn");

    // Session 3 has account_alias = 'tnn' -> exactly once
    const item3 = sessionRows[2];
    const text3 = item3.textContent || "";
    expect(text3).toContain("tnn");
    const tnnMatches = (text3.match(/\btnn\b/g) || []).length;
    expect(tnnMatches).toBe(1);
    expect(text3).not.toContain("tnn · tnn");
  });

  it("renders metadata subline with clean separator dots without duplicate delimiters", async () => {
    await render(<ChatSessionList activeSessionId="session-1" />);

    await act(async () => {
      await Promise.resolve();
      await new Promise((r) => setTimeout(r, 20));
    });

    const sessionItems = container.querySelectorAll("button");
    const sessionRows = Array.from(sessionItems).filter((btn) =>
      btn.textContent?.includes("Research Paper Review")
    );
    expect(sessionRows.length).toBe(1);

    const item1 = sessionRows[0];
    const subline = item1.querySelector("span.text-\\[0\\.6875rem\\]");
    expect(subline).not.toBeNull();

    // The subline should contain: timeAgo, msgs, source, account_alias
    const sublineText = subline?.textContent || "";
    expect(sublineText).toContain("14 msgs");
    expect(sublineText).toContain("tui");
    expect(sublineText).toContain("prm");

    // Verify separator spans (aria-hidden)
    const separators = subline?.querySelectorAll("span[aria-hidden]");
    expect(separators?.length).toBe(3);
  });

  it("handles compacted total message count tooltip properly", async () => {
    await render(<ChatSessionList activeSessionId="session-1" />);

    await act(async () => {
      await Promise.resolve();
      await new Promise((r) => setTimeout(r, 20));
    });

    const sessionItems = container.querySelectorAll("button");
    const item3 = Array.from(sessionItems).find((btn) =>
      btn.textContent?.includes("CD-DOCI Tuning")
    );
    expect(item3).toBeDefined();

    // Session 3 has total_message_count = 20, active message_count = 8
    const msgSpan = item3?.querySelector("span[title*='total msgs']");
    expect(msgSpan).not.toBeNull();
    expect(msgSpan?.getAttribute("title")).toBe("20 total msgs (8 active in context)");
    expect(msgSpan?.textContent).toBe("20 msgs");
  });
});
