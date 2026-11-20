/** Ordered PTY OSC boundaries. Text, prompts, and idle gaps are not completion. */
export interface ReplayBoundary {
  phase: "begin" | "end" | "abort";
  generation: string;
}

export interface ReplayStartControl {
  type: "replay-start";
  generation: string;
}

export const REPLAY_STALLED_MESSAGE =
  "Conversation replay is still loading; completion has not been confirmed.";

export function parseReplayStartControlMessage(data: string): ReplayStartControl | null {
  try {
    const parsed = JSON.parse(data);
    if (
      parsed &&
      typeof parsed === "object" &&
      parsed.type === "replay-start" &&
      typeof parsed.generation === "string" &&
      /^[a-f0-9-]{36}$/.test(parsed.generation)
    ) {
      return { type: "replay-start", generation: parsed.generation };
    }
  } catch {
    /* not JSON */
  }
  return null;
}

export class ReplayBoundaryGate {
  private generation: string | null = null;
  private finished = false;
  private endSeen = false;
  private pinnedGeneration: string | null = null;

  reset(): void {
    this.generation = null;
    this.finished = false;
    this.endSeen = false;
    this.pinnedGeneration = null;
  }

  pin(generation: string): void {
    this.pinnedGeneration = generation;
    this.generation = null;
    this.finished = false;
    this.endSeen = false;
  }

  isPinned(): boolean {
    return this.pinnedGeneration !== null;
  }

  getPinnedGeneration(): string | null {
    return this.pinnedGeneration;
  }

  receive(data: string): ReplayBoundary | null {
    const match = /^hermes-replay;(begin|end|abort);([a-f0-9-]{36})$/.exec(data);
    if (!match) return null;
    const phase = match[1] as ReplayBoundary["phase"];
    const generation = match[2];

    if (this.pinnedGeneration !== null && generation !== this.pinnedGeneration) {
      return null;
    }

    if (phase === "begin") {
      this.generation = generation;
      this.finished = false;
      this.endSeen = false;
    } else {
      // A ring-buffer snapshot may start after the begin marker was wiped.
      if (this.generation !== null && this.generation !== generation) return null;
      if (this.finished) return null;
      this.generation = generation;
      this.endSeen = true;
    }
    return { phase, generation };
  }

  complete(generation: string): boolean {
    if (this.pinnedGeneration !== null && generation !== this.pinnedGeneration) return false;
    if (this.generation !== generation || this.finished || !this.endSeen) return false;
    this.finished = true;
    this.pinnedGeneration = null;
    return true;
  }
}
