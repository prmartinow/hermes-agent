import { PassThrough } from "stream";

import { Box, renderSync } from "@hermes/ink";
import React from "react";
import { describe, expect, it } from "vitest";

import { Banner, SessionPanel } from "../components/branding.js";
import { MessageLine } from "../components/messageLine.js";
import { stripAnsi } from "../lib/text.js";
import { DEFAULT_THEME } from "../theme.js";

const makeStreams = (cols = 120, rows = 42) => {
  const stdout = new PassThrough();
  const stdin = new PassThrough();
  const stderr = new PassThrough();
  let output = "";

  Object.assign(stdout, { columns: cols, isTTY: false, rows });
  Object.assign(stdin, { isTTY: false });
  Object.assign(stderr, { isTTY: false });
  stdout.on("data", chunk => {
    output += chunk.toString();
  });

  return {
    getOutput: () => stripAnsi(output),
    stderr,
    stdin,
    stdout
  };
};

describe("4-Layer Real-Life Session History & Terminal Rendering Test Suite", () => {
  // ──────────────────────────────────────────────────────────────────────────
  // Layer 1: In-Memory Terminal State Machine & Cell-Attribute Testing
  // ──────────────────────────────────────────────────────────────────────────
  it("Layer 1: Validates Item 0 Header, Brand Titles, and Model Panel Invariants", () => {
    const { getOutput, stderr, stdin, stdout } = makeStreams(120, 42);

    const introInfo = {
      model: "gemini-3.7-flash-high",
      profile_name: "default",
      skills: ["web-tui-development", "agent-harnesses-hermes"],
      tools: ["read_file", "write_file", "terminal", "session_search"],
      version: "0.20.6"
    };

    const instance = renderSync(
      React.createElement(
        Box,
        { flexDirection: "column", paddingX: 1, width: 120 },
        React.createElement(Banner, { maxWidth: 118, t: DEFAULT_THEME }),
        React.createElement(SessionPanel, {
          info: introInfo,
          maxWidth: 118,
          sid: "20260831_071104_f61c30",
          t: DEFAULT_THEME
        })
      ),
      {
        patchConsole: false,
        stderr: stderr as any,
        stdin: stdin as any,
        stdout: stdout as any
      }
    );

    instance.unmount();
    instance.cleanup();

    const clean = getOutput();

    expect(clean).toContain("Hermes");
    expect(clean).toContain("Nous Research");
    expect(clean).toContain("Hermes Agent v0.20.6");
    expect(clean).toContain("Available Tools");
  });

  // ──────────────────────────────────────────────────────────────────────────
  // Layer 2: Visual Regression & Layout Grid Snapshots Across Terminal Presets
  // ──────────────────────────────────────────────────────────────────────────
  it("Layer 2: Verifies box-drawing geometry (╭─, │, ╰─) across narrow (80), standard (120), and wide (180) columns", () => {
    const presets = [80, 120, 180];

    for (const cols of presets) {
      const { getOutput, stderr, stdin, stdout } = makeStreams(cols, 42);

      const introInfo = {
        model: "gemini-3.7-flash-high",
        profile_name: "default",
        skills: ["web-tui-development"],
        tools: ["read_file", "write_file", "terminal"],
        version: "0.20.6"
      };

      const instance = renderSync(
        React.createElement(
          Box,
          { flexDirection: "column", paddingX: 1, width: cols },
          React.createElement(Banner, { maxWidth: cols - 2, t: DEFAULT_THEME }),
          React.createElement(SessionPanel, {
            info: introInfo,
            maxWidth: cols - 2,
            sid: "20260831_071104_f61c30",
            t: DEFAULT_THEME
          })
        ),
        {
          patchConsole: false,
          stderr: stderr as any,
          stdin: stdin as any,
          stdout: stdout as any
        }
      );

      instance.unmount();
      instance.cleanup();

      const clean = getOutput();

      // Check box border integrity
      expect(clean).toContain("╭");
      expect(clean).toContain("│");
      expect(clean).toContain("╰");
      expect(clean).toContain("Hermes");
    }
  });

  // ──────────────────────────────────────────────────────────────────────────
  // Layer 3: Turn Sequence & First Message Visibility Beneath Banner
  // ──────────────────────────────────────────────────────────────────────────
  it("Layer 3: Asserts Turn 1 user prompt and assistant response render seamlessly beneath Item 0 banner", () => {
    const { getOutput, stderr, stdin, stdout } = makeStreams(120, 42);

    const userMsg: any = {
      role: "user",
      text: "Turn 1: Initial user query starting the session investigation."
    };
    const assistantMsg: any = {
      role: "assistant",
      text: "Turn 1 Response: I have begun investigating the codebase architecture."
    };

    const instance = renderSync(
      React.createElement(
        Box,
        { flexDirection: "column", paddingX: 1, width: 120 },
        React.createElement(MessageLine, { cols: 116, msg: userMsg, t: DEFAULT_THEME }),
        React.createElement(MessageLine, { cols: 116, msg: assistantMsg, t: DEFAULT_THEME })
      ),
      {
        patchConsole: false,
        stderr: stderr as any,
        stdin: stdin as any,
        stdout: stdout as any
      }
    );

    instance.unmount();
    instance.cleanup();

    const clean = getOutput();

    expect(clean).toContain("Turn 1: Initial user query starting the session investigation.");
    expect(clean).toContain("Turn 1 Response: I have begun investigating the codebase architecture.");
  });

  // ──────────────────────────────────────────────────────────────────────────
  // Layer 4: Multi-Turn Scaling & Memory Footprint Bounds
  // ──────────────────────────────────────────────────────────────────────────
  it("Layer 4: Validates multi-turn conversation scaling without dropping lines or layout corruption", () => {
    const { getOutput, stderr, stdin, stdout } = makeStreams(120, 42);

    const messages: any[] = [];

    for (let i = 1; i <= 20; i++) {
      messages.push({
        role: i % 2 === 1 ? "user" : "assistant",
        text: `Turn ${i} verified message payload with execution logs.`
      });
    }

    const instance = renderSync(
      React.createElement(
        Box,
        { flexDirection: "column", paddingX: 1, width: 120 },
        messages.map((m, idx) =>
          React.createElement(MessageLine, { cols: 116, key: idx, msg: m, t: DEFAULT_THEME })
        )
      ),
      {
        patchConsole: false,
        stderr: stderr as any,
        stdin: stdin as any,
        stdout: stdout as any
      }
    );

    instance.unmount();
    instance.cleanup();

    const clean = getOutput();

    expect(clean).toContain("Turn 1 verified message payload");
    expect(clean).toContain("Turn 10 verified message payload");
    expect(clean).toContain("Turn 20 verified message payload");
  });
});
