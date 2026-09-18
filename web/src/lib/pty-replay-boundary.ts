/** Ordered PTY OSC boundaries. Text, prompts, and idle gaps are not completion. */
export interface ReplayBoundary {
  phase: "begin" | "end" | "abort";
  generation: string;
}

export const REPLAY_STALLED_MESSAGE =
  "Conversation replay is still loading; completion has not been confirmed.";

export class ReplayBoundaryGate {
  private generation: string | null = null;
  private finished = false;
  private endSeen = false;

  reset(): void {
    this.generation = null;
    this.finished = false;
    this.endSeen = false;
  }

  receive(data: string): ReplayBoundary | null {
    const match = /^hermes-replay;(begin|end|abort);([a-f0-9-]{36})$/.exec(data);
    if (!match) return null;
    const phase = match[1] as ReplayBoundary["phase"];
    const generation = match[2];
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
    if (this.generation !== generation || this.finished || !this.endSeen) return false;
    this.finished = true;
    return true;
  }
}
