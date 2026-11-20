import type { PtyConnectionState } from "@/lib/pty-reconnect";

/**
 * Warn when replay completion is missing; this timeout is not success.
 */
export const PTY_RESUME_LOADING_MAX_MS = 30000;

export const PTY_RESUME_LOADING_MESSAGE =
  "Please wait while the conversation loads…";

export interface ResumeLoadingOverlayInput {
  hasResumeTarget: boolean;
  ptyState: PtyConnectionState;
  hydrating: boolean;
}

/**
 * Keep the loading notice until the explicit replay boundary is parsed.
 * Receiving text or seeing a prompt is not proof that replay has ended.
 *
 * Reconnect / ended / closed states keep their own overlays and must not
 * stack this one on top.
 */
export function shouldShowResumeLoadingOverlay({
  hasResumeTarget,
  ptyState,
  hydrating,
}: ResumeLoadingOverlayInput): boolean {
  if (!hasResumeTarget || !hydrating) {
    return false;
  }
  if (
    ptyState === "reconnecting" ||
    ptyState === "closed" ||
    ptyState === "ended"
  ) {
    return false;
  }
  return ptyState === "connecting" || ptyState === "open";
}
