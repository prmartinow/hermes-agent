import { describe, expect, it } from "vitest";

import {
  PTY_RESUME_LOADING_MAX_MS,
  shouldShowResumeLoadingOverlay,
} from "./pty-resume-loading";

it("uses the replay timeout only as a positive warning deadline", () => {
  expect(PTY_RESUME_LOADING_MAX_MS).toBeGreaterThan(0);
});

describe("shouldShowResumeLoadingOverlay", () => {
  it("shows while a resume target is connecting or open and still hydrating", () => {
    expect(
      shouldShowResumeLoadingOverlay({
        hasResumeTarget: true,
        ptyState: "connecting",
        hydrating: true,
      }),
    ).toBe(true);
    expect(
      shouldShowResumeLoadingOverlay({
        hasResumeTarget: true,
        ptyState: "open",
        hydrating: true,
      }),
    ).toBe(true);
  });

  it("hides when there is no resume target", () => {
    expect(
      shouldShowResumeLoadingOverlay({
        hasResumeTarget: false,
        ptyState: "connecting",
        hydrating: true,
      }),
    ).toBe(false);
  });

  it("hides once hydration finishes", () => {
    expect(
      shouldShowResumeLoadingOverlay({
        hasResumeTarget: true,
        ptyState: "open",
        hydrating: false,
      }),
    ).toBe(false);
  });

  it("defers to reconnect / closed / ended overlays", () => {
    for (const ptyState of ["reconnecting", "closed", "ended"] as const) {
      expect(
        shouldShowResumeLoadingOverlay({
          hasResumeTarget: true,
          ptyState,
          hydrating: true,
        }),
      ).toBe(false);
    }
  });
});
