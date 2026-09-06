# Thinking Traces Stream Segmentation & Architecture

This document formalizes the technical investigation, wire-level mechanics, and architectural design for partitioning continuous reasoning/thinking token streams into discrete, chronological thinking cards.

---

## 1. Executive Summary

During generative inference with reasoning models (e.g. Gemini 3 / Claude thinking models), the model emits structured thought traces prior to producing visible prose or issuing tool calls.

In unsegmented streaming implementations, thinking deltas accumulate into a single growing string buffer. This leads to two major UI degradations:
1. **Compounding Monoliths**: Multiple sequential thought paragraphs (often with distinct phases, step titles, and semantic boundaries) are welded into a single expanding block.
2. **Chronological Stream Inversion**: When an agent executes a multi-step turn (`Thinking 1 -> Tool Call -> Thinking 2 -> Tool Call -> Final Response`), a single unpartitioned reasoning container locks to the bottom, causing tool calls and subsequent reasoning phases to render out of chronological order.

By decoupling the thinking stream into **discrete thinking segments** partitioned by titles, headings, and paragraph boundaries, each reasoning block settles in its natural historical position as an immutable record while only the currently active phase streams live.

---

## 2. Wire Protocol & Stream Dissection

In Server-Sent Events (SSE) streaming (`:streamGenerateContent?alt=sse`), inference tokens arrive as discrete event frames containing candidate content parts:

```json
/* Thought Frame 1 */
{
  "response": {
    "candidates": [{
      "content": {
        "parts": [{
          "thought": true,
          "text": "**Step 1: Identifying Architectural Invariants**\nAnalyzing system constraints and isolating components..."
        }]
      }
    }]
  }
}

/* Thought Frame 2 (Structural Boundary) */
{
  "response": {
    "candidates": [{
      "content": {
        "parts": [{
          "thought": true,
          "text": "\n\n**Step 2: Evaluating Trade-offs & Amplification**\nExamining read/write amplification across storage layers..."
        }]
      }
    }]
  }
}

/* Tool Call Frame (Reasoning Complete for Phase) */
{
  "response": {
    "candidates": [{
      "content": {
        "parts": [{
          "functionCall": {
            "name": "read_file",
            "args": {"path": "config.yaml"}
          }
        }]
      }
    }]
  }
}
```

### Boundary Delimiters on the Wire:
1. **Title & Step Delimiters**: Markdown headings (`### <Heading>`) or bold titles (`**Step N: <Title>**`, `**<Phase Name>**`).
2. **Paragraph Breaks**: Double newline sequences (`\n\n` or `\n{3,}`) preceding new capitalized thoughts.
3. **Execution Mode Transitions**: The transition from `thought: true` to `thought: false` or to a `functionCall`.

---

## 3. CLI Binary Architecture Insights

Analysis of CLI binary implementations indicates a clear separation of concerns between transient HUD rendering and persistent session history:

1. **Transient Active Step Display**: The CLI extracts the latest thought paragraph and title to update a live status HUD/spinner.
2. **Sealed Step Archiving**: As new thought steps ignite or tool calls execute, completed reasoning blocks are frozen with their associated duration.
3. **Persistence Policy**: Telemetry tracks reasoning token counts (`thoughts_token_count`) while raw ephemeral thinking traces are kept structured without polluting flat conversational contexts.

---

## 4. Segmented Thinking Architecture

To render thinking traces cleanly, the stream reducer decomposes the reasoning pipeline into discrete **Thinking Cards**:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Transcript Segment Stream                       │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  🧠 [Thinking: Step 1: Architectural Analysis]                 ✓ 1.2s  │
│     Analyzing system constraints and isolating components...           │
│                                                                        │
│  🔧 [Tool: read_file(path="config.yaml")]                      ✓ 0.1s  │
│     Returned 42 lines.                                                 │
│                                                                        │
│  🧠 [Thinking: Step 2: Trade-off Evaluation]                   ✓ 2.4s  │
│     Evaluating amplification factors and latency profiles...           │
│                                                                        │
│  💬 [Assistant Response]                                       ● Live  │
│     Based on the architectural analysis, here are the recommendations: │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Implementation Design

### A. Dynamic Stream Segmenter (`turnController.ts`)
* Maintain active reasoning state and monitor incoming token deltas for boundary patterns (`\n\n**...**` or `\n\n###`).
* When a boundary pattern is detected:
  1. Finalize the active thinking segment: trim trailing whitespace, calculate token count, stamp completion timestamp, and seal it.
  2. Instantiate a fresh thinking segment at the bottom of `streamSegments`.
* When a tool call starts (`recordToolStart`):
  1. Immediately close and seal any active thinking segment.
  2. Append the tool call segment chronologically underneath.

### B. Segment Rendering & UI (`thinking.tsx`)
* Each thinking segment renders as an isolated, collapsible card.
* Completed cards display a subtle execution badge (`✓ <duration>s`) and remain stationary during scroll.
* Active cards display the live streaming cursor and pulsing activity spinner.

---

## 6. Quality & Security Safeguards

1. **Monotonic Ordering**: Segments are append-only. A completed segment is never modified or pushed behind a newer tool call.
2. **Buffer Bounds**: Delimiter matching operates on bounded sliding tails (`O(1)` per token) to prevent `O(N^2)` string parsing overhead on long streams.
3. **Fallback Safety**: If no titles or headings are present, the segmenter defaults to paragraph breaks (`\n\n`) or seals on the tool call / prose transition.
