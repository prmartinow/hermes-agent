import type { Terminal } from "@xterm/xterm";
import type { Dispatch, RefObject, SetStateAction } from "react";

import { ReplayBoundaryGate, REPLAY_STALLED_MESSAGE, parseReplayStartControlMessage } from "./pty-replay-boundary";
import { PTY_RESUME_LOADING_MAX_MS } from "./pty-resume-loading";
import { parseResumeControlMessage, shouldFollowPtyOutput } from "./pty-scroll";

interface PtyOutputStreamOptions {
  term: Terminal;
  initialResume: string | null;
  stickToBottomRef: RefObject<boolean>;
  setHydrating(hydrating: boolean): void;
  setBanner: Dispatch<SetStateAction<string | null>>;
  onText(text: string): void;
}

/** Ordered ANSI delivery and explicit replay completion for one PTY socket. */
export class PtyOutputStream {
  private readonly gate = new ReplayBoundaryGate();
  private readonly decoder = new TextDecoder();
  private readonly boundaryHandler: { dispose(): void };
  private effectiveResume: string | null;
  private disposed = false;
  private replayActive = false;
  private replayFinished = false;
  private resizeReplaying = false;
  private pending: string[] = [];
  private frame: number | null = null;
  private replayTimer: ReturnType<typeof setTimeout> | null = null;
  private resizeTimer: ReturnType<typeof setTimeout> | null = null;

  private readonly options: PtyOutputStreamOptions;

  constructor(options: PtyOutputStreamOptions) {
    this.options = options;
    this.effectiveResume = options.initialResume;
    this.boundaryHandler = options.term.parser.registerOscHandler(777, data => this.boundary(data));
    if (options.initialResume) this.beginReplay();
    else options.setHydrating(false);
  }

  private clearReplayTimer(): void {
    if (this.replayTimer) clearTimeout(this.replayTimer);
    this.replayTimer = null;
  }

  private startReplay(): void {
    this.replayActive = true;
    this.replayFinished = false;
    this.clearReplayTimer();
    this.options.setHydrating(true);
    this.replayTimer = setTimeout(() => {
      this.replayTimer = null;
      if (!this.disposed && !this.replayFinished) this.options.setBanner(REPLAY_STALLED_MESSAGE);
    }, PTY_RESUME_LOADING_MAX_MS);
  }

  private beginReplay(): void {
    if (!this.gate.isPinned()) this.gate.reset();
    this.options.stickToBottomRef.current = true;
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    this.frame = null;
    this.pending = [];
    this.options.term.reset();
    this.options.term.clear();
    this.startReplay();
  }

  private finishReplay(): void {
    if (this.disposed || this.replayFinished) return;
    this.replayFinished = true;
    this.replayActive = false;
    this.clearReplayTimer();
    this.options.setBanner(previous => previous === REPLAY_STALLED_MESSAGE ? null : previous);
    this.options.setHydrating(false);
    if (this.options.stickToBottomRef.current && !this.options.term.hasSelection()) {
      this.options.term.scrollToBottom();
    }
  }

  private boundary(data: string): boolean {
    const boundary = this.gate.receive(data);
    if (!boundary) return false;
    if (boundary.phase === "begin") this.startReplay();
    else if (boundary.phase === "end") {
      this.options.term.write("", () => {
        if (!this.disposed && this.gate.complete(boundary.generation)) this.finishReplay();
      });
    } else if (this.gate.complete(boundary.generation)) {
      this.clearReplayTimer();
      this.replayActive = false;
      this.options.setHydrating(false);
      this.options.setBanner("Conversation replay failed. Reconnect to retry.");
    }
    return true;
  }

  receive(data: string | ArrayBuffer): void {
    if (this.disposed) return;
    if (typeof data === "string") {
      const start = parseReplayStartControlMessage(data);
      if (start) {
        this.effectiveResume ||= start.generation;
        this.gate.pin(start.generation);
        this.options.stickToBottomRef.current = true;
        this.startReplay();
        return;
      }
      const resume = parseResumeControlMessage(data);
      if (resume) {
        this.effectiveResume = resume;
        this.beginReplay();
        return;
      }
    }
    const text = typeof data === "string" ? data : this.decoder.decode(new Uint8Array(data), { stream: true });
    this.options.onText(text);
    if (!text) return;
    this.pending.push(text);
    if (this.pending.length > 30 || text.length > 8192) {
      if (this.frame !== null) cancelAnimationFrame(this.frame);
      this.frame = null;
      this.flush();
    } else if (this.frame === null) this.frame = requestAnimationFrame(() => this.flush());
  }

  private flush(): void {
    if (this.disposed || this.pending.length === 0) return;
    const batch = this.pending.join("");
    this.pending = [];
    this.frame = null;
    const { term, stickToBottomRef } = this.options;
    const follow = (this.resizeReplaying || shouldFollowPtyOutput(
      this.effectiveResume, stickToBottomRef.current, this.replayActive)) && !term.hasSelection();
    term.write(batch, follow ? () => {
      // A selection or wheel event after queueing wins over this follow decision.
      if (!this.disposed && stickToBottomRef.current && !term.hasSelection()) term.scrollToBottom();
    } : undefined);
    if (this.resizeReplaying) {
      if (this.resizeTimer) clearTimeout(this.resizeTimer);
      this.resizeTimer = setTimeout(() => {
        this.resizeTimer = null;
        this.resizeReplaying = false;
        if (!this.disposed && stickToBottomRef.current && !term.hasSelection()) term.scrollToBottom();
      }, 150);
    }
  }

  resized(): void {
    this.resizeReplaying = true;
    this.options.stickToBottomRef.current = true;
  }

  close(): void {
    if (this.disposed) return;
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    this.frame = null;
    this.flush();
    const tail = this.decoder.decode();
    if (tail) this.options.term.write(tail);
    this.clearReplayTimer();
  }

  dispose(): void {
    this.disposed = true;
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    if (this.resizeTimer) clearTimeout(this.resizeTimer);
    this.pending = [];
    this.clearReplayTimer();
    this.boundaryHandler.dispose();
  }
}
