import React from "react";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Clock,
  Layers,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import type {
  CodexQuotaInterval,
  CodexQuotaTimelineResponse,
  CodexSessionActivityDelta,
} from "../lib/api";

export interface CodexQuotaTimelineViewProps {
  timelineData: CodexQuotaTimelineResponse | null;
  loading: boolean;
  error: string | null;
  selectedWindow: "primary" | "secondary";
  onSelectWindow: (windowId: "primary" | "secondary") => void;
  formatTimestamp: (ts: string | number | null | undefined) => string;
  formatTokens: (n: number | null | undefined) => string;
  formatDuration: (minutes: number | null | undefined) => string;
}

export default function CodexQuotaTimelineView({
  timelineData,
  loading,
  error,
  selectedWindow,
  onSelectWindow,
  formatTimestamp,
  formatTokens,
  formatDuration,
}: CodexQuotaTimelineViewProps) {
  const intervals = timelineData?.intervals || [];
  const accountId =
    timelineData?.intervals?.[0]?.account_id ||
    timelineData?.rows?.[0]?.account_id ||
    "unknown";

  return (
    <div className="flex flex-col gap-5 w-full font-mono text-foreground" data-testid="codex-quota-timeline-view">
      {/* Timeline Controls & Scope Banner */}
      <div className="border border-midground/20 rounded-lg p-4 bg-black/40 flex flex-col gap-3">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-midground/15 pb-3">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs text-text-secondary font-semibold uppercase tracking-wider">
              Quota Window:
            </span>
            <div className="inline-flex rounded-md p-0.5 bg-midground/20 border border-midground/20">
              <button
                type="button"
                onClick={() => onSelectWindow("primary")}
                data-testid="codex-window-primary"
                className={`px-3 py-1 text-xs rounded transition-colors ${
                  selectedWindow === "primary"
                    ? "bg-purple-600 text-white font-bold shadow-sm"
                    : "text-text-secondary hover:text-foreground"
                }`}
              >
                Primary Window
              </button>
              <button
                type="button"
                onClick={() => onSelectWindow("secondary")}
                data-testid="codex-window-secondary"
                className={`px-3 py-1 text-xs rounded transition-colors ${
                  selectedWindow === "secondary"
                    ? "bg-purple-600 text-white font-bold shadow-sm"
                    : "text-text-secondary hover:text-foreground"
                }`}
              >
                Secondary Window
              </button>
            </div>
          </div>

          {/* Scope notice banner */}
          <div
            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded bg-amber-500/10 border border-amber-500/30 text-xs text-amber-300"
            data-testid="codex-scope-notice"
          >
            <ShieldAlert className="w-3.5 h-3.5 text-amber-400 shrink-0" />
            <span className="font-semibold text-amber-200">Scope:</span>
            <span>Profile-local activity; account match unverified; external usage possible</span>
          </div>
        </div>

        {/* Timeline Metadata */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 text-xs pt-1">
          <div className="flex flex-col">
            <span className="text-[10px] text-text-secondary/70 uppercase">Account ID</span>
            <span className="font-mono text-foreground truncate" title={accountId}>
              {accountId}
            </span>
          </div>
          <div className="flex flex-col">
            <span className="text-[10px] text-text-secondary/70 uppercase">Time Span</span>
            <span className="font-mono text-foreground">
              {timelineData?.days ?? 7} Days History
            </span>
          </div>
          <div className="flex flex-col">
            <span className="text-[10px] text-text-secondary/70 uppercase">Observed Intervals</span>
            <span className="font-mono text-foreground">
              {intervals.length} intervals ({timelineData?.snapshots?.length ?? 0} snapshots)
            </span>
          </div>
          <div className="flex flex-col">
            <span className="text-[10px] text-text-secondary/70 uppercase">Attribution Status</span>
            <span className="font-mono text-purple-300">
              {timelineData?.attribution_status || "correlated_only"}
            </span>
          </div>
        </div>
      </div>

      {/* Loading state */}
      {loading && !timelineData && (
        <div className="border border-midground/20 rounded-lg p-12 bg-black/30 flex flex-col items-center justify-center gap-3 text-text-secondary">
          <RefreshCw className="w-6 h-6 animate-spin text-purple-400" />
          <span className="text-xs">Loading OpenAI Codex quota timeline…</span>
        </div>
      )}

      {/* Error state */}
      {error && (
        <div
          className="p-4 text-xs text-rose-400 bg-rose-500/10 border border-rose-500/20 rounded-lg flex items-center gap-2"
          data-testid="codex-timeline-error"
        >
          <ShieldAlert className="w-4 h-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* Empty intervals state */}
      {!loading && !error && intervals.length === 0 && (
        <div
          className="py-12 text-center text-xs text-text-secondary border border-midground/20 rounded-lg bg-black/30"
          data-testid="codex-timeline-empty"
        >
          No observed quota intervals recorded for the past {timelineData?.days ?? 7} days.
        </div>
      )}

      {/* Intervals List */}
      {!loading && intervals.length > 0 && (
        <div className="flex flex-col gap-4">
          {intervals.map((inv: CodexQuotaInterval, idx: number) => {
            const isGap = inv.kind === "gap" || inv.status === "gap";
            const isReset = inv.kind === "reset_or_replenishment" || inv.status === "reset_or_replenishment";
            const chkDelta = inv.checkpoint_delta;
            const sessionDeltas: CodexSessionActivityDelta[] = chkDelta?.session_deltas || [];
            const durationMinutes = Math.round(inv.duration_seconds / 60);

            return (
              <div
                key={`interval-${inv.start_time}-${inv.end_time}-${idx}`}
                className="border border-midground/20 rounded-lg overflow-hidden bg-black/30 flex flex-col"
                data-testid="codex-interval-card"
              >
                {/* Interval Header */}
                <div className="p-3.5 bg-midground/10 border-b border-midground/15 flex flex-col md:flex-row md:items-center justify-between gap-3 text-xs">
                  <div className="flex items-center gap-2.5 flex-wrap">
                    <span className="font-bold text-foreground flex items-center gap-1.5">
                      <Clock className="w-3.5 h-3.5 text-purple-400 shrink-0" />
                      {formatTimestamp(inv.start_time)}
                      <ArrowRight className="w-3 h-3 text-midground" />
                      {formatTimestamp(inv.end_time)}
                    </span>
                    <span className="text-text-secondary text-[11px] font-mono">
                      ({formatDuration(durationMinutes)})
                    </span>

                    {/* Interval Kind Badge */}
                    {isGap && (
                      <span
                        className="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-amber-500/10 text-amber-400 border border-amber-500/30 inline-flex items-center gap-1"
                        data-testid="interval-gap"
                      >
                        <AlertTriangle className="w-3 h-3" />
                        Observation Gap
                      </span>
                    )}
                    {isReset && (
                      <span
                        className="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-blue-500/10 text-blue-300 border border-blue-500/30 inline-flex items-center gap-1"
                        data-testid="interval-reset"
                      >
                        <Layers className="w-3 h-3" />
                        Quota Reset / Replenishment
                      </span>
                    )}
                    {!isGap && !isReset && (
                      <span className="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-purple-500/10 text-purple-300 border border-purple-500/30">
                        {inv.kind || "Usage"}
                      </span>
                    )}
                    {inv.reset_at_changed && (
                      <span className="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-cyan-500/10 text-cyan-300 border border-cyan-500/30">
                        Reset Shifted
                      </span>
                    )}
                  </div>

                  {/* Account Quota Delta Callout - Clearly account-level, NOT per-session charge */}
                  <div className="flex items-center gap-2 bg-black/50 px-3 py-1.5 rounded border border-midground/20">
                    <span className="text-[11px] text-text-secondary font-medium">
                      Account Quota Delta (Account-level, NOT per-session charge):
                    </span>
                    {inv.delta_used_percent !== null && inv.delta_used_percent !== undefined ? (
                      <span
                        className={`font-mono font-bold ${
                          inv.delta_used_percent > 0
                            ? "text-rose-400"
                            : inv.delta_used_percent < 0
                            ? "text-emerald-400"
                            : "text-foreground"
                        }`}
                      >
                        {inv.delta_used_percent > 0 ? "+" : ""}
                        {inv.delta_used_percent.toFixed(2)}%
                        <span className="text-[10px] text-text-secondary font-normal ml-1">
                          ({inv.start_used_percent !== null ? `${inv.start_used_percent.toFixed(1)}%` : "—"} →{" "}
                          {inv.end_used_percent !== null ? `${inv.end_used_percent.toFixed(1)}%` : "—"})
                        </span>
                      </span>
                    ) : (
                      <span className="font-mono text-text-secondary italic" data-testid="delta-unknown">
                        — (Unknown)
                      </span>
                    )}
                  </div>
                </div>

                {/* Interval Description if not standard */}
                {inv.description && (
                  <div className="px-3.5 py-1.5 bg-black/20 text-[11px] text-text-secondary border-b border-midground/10">
                    {inv.description}
                  </div>
                )}

                {/* Correlated Activity Details */}
                <div className="p-3.5 flex flex-col gap-2.5">
                  <div className="flex items-center justify-between text-[11px] text-text-secondary border-b border-midground/10 pb-1.5">
                    <span className="font-semibold text-foreground">
                      Correlated Session Activity
                    </span>
                    <span className="text-[10px] text-amber-400/90 font-mono">
                      Profile-local activity; account match unverified; external usage possible
                    </span>
                  </div>

                  {/* Discontinuity / Gap / Reset Alerts */}
                  {isGap && (
                    <div className="p-3 text-xs bg-amber-500/10 text-amber-300 rounded border border-amber-500/20 flex items-center gap-2">
                      <AlertTriangle className="w-4 h-4 shrink-0 text-amber-400" />
                      <span>Observation gap or collection error; usage delta is unknown.</span>
                    </div>
                  )}

                  {!isGap && isReset && (
                    <div className="p-3 text-xs bg-blue-500/10 text-blue-300 rounded border border-blue-500/20 flex items-center gap-2">
                      <Layers className="w-4 h-4 shrink-0 text-blue-400" />
                      <span>Quota reset or replenishment occurred over interval; quota delta is not regular depletion.</span>
                    </div>
                  )}

                  {!isGap && !isReset && chkDelta?.status === "missing_checkpoint" && (
                    <div className="p-3 text-xs bg-midground/10 text-text-secondary rounded border border-midground/20 flex items-center gap-2">
                      <AlertTriangle className="w-4 h-4 shrink-0 text-midground" />
                      <span>Missing checkpoint; usage delta is unknown.</span>
                    </div>
                  )}

                  {!isGap && !isReset && chkDelta?.status !== "missing_checkpoint" && sessionDeltas.length === 0 && (
                    <div className="py-4 text-center text-xs text-text-secondary italic">
                      No local Hermes session activity recorded during this interval.
                    </div>
                  )}

                  {/* Session Deltas Table */}
                  {!isGap && !isReset && sessionDeltas.length > 0 && (
                    <div className="overflow-x-auto">
                      <table className="w-full text-left text-xs border-collapse font-mono">
                        <thead>
                          <tr className="bg-midground/10 text-text-secondary text-[11px] uppercase tracking-wider select-none border-b border-midground/20">
                            <th className="py-2 px-3">Session ID</th>
                            <th className="py-2 px-3">Model</th>
                            <th className="py-2 px-3">Task</th>
                            <th className="py-2 px-3 text-right">Calls</th>
                            <th className="py-2 px-3 text-right">Cached Input</th>
                            <th className="py-2 px-3 text-right">Uncached Input</th>
                            <th className="py-2 px-3 text-right">Output</th>
                            <th className="py-2 px-3 text-right">Status</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-midground/10">
                          {sessionDeltas.map((sd: CodexSessionActivityDelta, sdIdx: number) => {
                            const hasDelta = sd.has_baseline && sd.delta_counters !== null;
                            const d = sd.delta_counters;
                            const cached = d ? (d.cache_tokens ?? (d.cache_read_tokens + d.cache_write_tokens)) : null;

                            return (
                              <tr
                                key={`sd-${sd.session_id}-${sd.model}-${sdIdx}`}
                                className="hover:bg-midground/5"
                                data-testid="session-delta-row"
                              >
                                <td className="py-2 px-3 whitespace-nowrap text-foreground font-medium">
                                  <span className="truncate max-w-[140px] inline-block align-middle" title={sd.session_id}>
                                    {sd.session_id || "—"}
                                  </span>
                                </td>
                                <td className="py-2 px-3 whitespace-nowrap">
                                  <span className="px-2 py-0.5 bg-midground/10 text-purple-300 rounded text-[11px] border border-midground/20">
                                    {sd.model || "—"}
                                  </span>
                                </td>
                                <td className="py-2 px-3 whitespace-nowrap text-text-secondary">
                                  {sd.task || "—"}
                                </td>
                                <td className="py-2 px-3 text-right text-text-secondary">
                                  {hasDelta && d ? formatTokens(d.api_call_count) : (
                                    <span className="text-text-secondary/50 italic" title="No baseline available; lifetime totals not shown as delta">—</span>
                                  )}
                                </td>
                                <td className="py-2 px-3 text-right text-text-secondary">
                                  {hasDelta && d ? formatTokens(cached) : (
                                    <span className="text-text-secondary/50 italic" title="No baseline available; lifetime totals not shown as delta">—</span>
                                  )}
                                </td>
                                <td className="py-2 px-3 text-right text-text-secondary">
                                  {hasDelta && d ? formatTokens(d.input_tokens) : (
                                    <span className="text-text-secondary/50 italic" title="No baseline available; lifetime totals not shown as delta">—</span>
                                  )}
                                </td>
                                <td className="py-2 px-3 text-right text-text-secondary">
                                  {hasDelta && d ? formatTokens(d.output_tokens) : (
                                    <span className="text-text-secondary/50 italic" title="No baseline available; lifetime totals not shown as delta">—</span>
                                  )}
                                </td>
                                <td className="py-2 px-3 text-right whitespace-nowrap">
                                  {!sd.has_baseline || sd.status === "missing_baseline" ? (
                                    <span
                                      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold uppercase bg-amber-500/10 text-amber-400 border border-amber-500/30"
                                      title={sd.reason || "Missing baseline checkpoint: lifetime counters not displayed as delta"}
                                      data-testid="missing-baseline-badge"
                                    >
                                      Missing Baseline
                                    </span>
                                  ) : sd.is_discontinuity ? (
                                    <span
                                      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold uppercase bg-rose-500/10 text-rose-400 border border-rose-500/30"
                                      title={sd.reason || "Discontinuity in counters"}
                                    >
                                      Discontinuity
                                    </span>
                                  ) : (
                                    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold uppercase bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
                                      <CheckCircle2 className="w-2.5 h-2.5" />
                                      OK
                                    </span>
                                  )}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                        {/* Summary Row for Total Interval Delta */}
                        {chkDelta?.total_delta && (
                          <tfoot>
                            <tr className="bg-midground/15 font-bold border-t border-midground/25 text-[11px]">
                              <td colSpan={3} className="py-2 px-3 text-foreground uppercase">
                                Interval Total Activity (Attributed)
                              </td>
                              <td className="py-2 px-3 text-right text-foreground">
                                {formatTokens(chkDelta.total_delta.api_call_count)}
                              </td>
                              <td className="py-2 px-3 text-right text-foreground">
                                {formatTokens(
                                  chkDelta.total_delta.cache_tokens ??
                                    (chkDelta.total_delta.cache_read_tokens + chkDelta.total_delta.cache_write_tokens)
                                )}
                              </td>
                              <td className="py-2 px-3 text-right text-foreground">
                                {formatTokens(chkDelta.total_delta.input_tokens)}
                              </td>
                              <td className="py-2 px-3 text-right text-foreground">
                                {formatTokens(chkDelta.total_delta.output_tokens)}
                              </td>
                              <td className="py-2 px-3 text-right text-text-secondary">
                                —
                              </td>
                            </tr>
                          </tfoot>
                        )}
                        {!chkDelta?.has_baseline && (
                          <tfoot>
                            <tr className="bg-midground/10 italic text-[11px]">
                              <td colSpan={8} className="py-2 px-3 text-amber-400 text-center">
                                Interval activity delta total is unknown (missing baseline). Lifetime counter totals are not masqueraded as deltas.
                              </td>
                            </tr>
                          </tfoot>
                        )}
                      </table>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
