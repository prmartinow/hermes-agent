import React, { useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Cpu,
  Info,
  RefreshCw,
  Shield,
  ShieldAlert,
} from "lucide-react";
import {
  api,
  type CodexPlanPeriod,
  type CodexUsageHistoryResponse,
} from "../lib/api";

export interface CodexUsageHistoryPanelProps {
  mode: "history" | "timeline";
}

function formatTokens(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString();
}

function formatBasisPoints(bps: number | null | undefined): string {
  if (bps === null || bps === undefined) return "—";
  const pct = bps / 100;
  return pct % 1 === 0 ? `${pct}%` : `${pct.toFixed(1)}%`;
}

function formatTimestamp(ts: string | number | null | undefined): string {
  if (!ts) return "—";
  try {
    const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
    if (isNaN(d.getTime())) return String(ts);
    return (
      d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) +
      " · " +
      d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" })
    );
  } catch {
    return String(ts);
  }
}

function formatDuration(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined) return "—";
  if (minutes >= 1440 && minutes % 1440 === 0) {
    const days = minutes / 1440;
    return `${days}d (${minutes}m)`;
  }
  if (minutes >= 60 && minutes % 60 === 0) {
    const hours = minutes / 60;
    return `${hours}h (${minutes}m)`;
  }
  return `${minutes}m`;
}

export default function CodexUsageHistoryPanel({ mode }: CodexUsageHistoryPanelProps) {
  const [data, setData] = useState<CodexUsageHistoryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;

    setData(null);
    setLoading(true);
    setError(null);

    api.getCodexUsageHistory({ days: 7, signal: controller.signal })
      .then((res) => {
        if (!cancelled) {
          setData(res);
          setLoading(false);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          if (err instanceof DOMException && err.name === "AbortError") {
            return;
          }
          if (err && typeof err === "object" && "name" in err && err.name === "AbortError") {
            return;
          }
          setError(err instanceof Error ? err.message : "Failed to load OpenAI Codex usage history");
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [mode]);

  const tokenBreakdown = data?.daily_token_usage_breakdown;
  const planHistory = data?.plan_limit_history;

  return (
    <div className="flex flex-col gap-5 w-full font-mono text-foreground" data-testid="codex-usage-panel">
      {/* Account Scope & Metadata Card */}
      {data && (
        <div className="border border-midground/20 rounded-lg p-4 bg-black/40 flex flex-col gap-3">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-midground/15 pb-3">
          <div className="flex items-center gap-2">
            <Cpu className="w-5 h-5 text-purple-400" />
            <h2 className="text-base font-bold tracking-wide text-foreground uppercase">
              {data?.provider === "openai-codex" ? "OpenAI Codex" : (data?.provider || "OpenAI Codex")}
            </h2>
            <span className="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-purple-500/10 text-purple-300 border border-purple-500/30">
              {mode === "history" ? "Daily Token Usage" : "Plan Quota Timeline"}
            </span>
          </div>

          {/* Account Scope Notice */}
          <div className="flex items-center gap-1.5 px-2.5 py-1 rounded bg-midground/10 border border-midground/20 text-xs text-text-secondary">
            <Shield className="w-3.5 h-3.5 text-midground shrink-0" />
            <span className="font-semibold text-foreground">Scope:</span>
            <span>Account-wide (not Hermes-session-specific)</span>
          </div>
        </div>

        {/* Freshness & Metadata Details */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 text-xs">
          <div className="flex flex-col">
            <span className="text-[10px] text-text-secondary/70 uppercase">Account ID</span>
            <span className="font-mono text-foreground truncate" title={data?.account_id ?? "unknown"}>
              {data?.account_id || "unknown"}
            </span>
          </div>

          <div className="flex flex-col">
            <span className="text-[10px] text-text-secondary/70 uppercase">Fetched At</span>
            <span className="font-mono text-foreground">
              {formatTimestamp(data?.fetched_at)}
            </span>
          </div>

          {mode === "history" && tokenBreakdown?.data && (
            <>
              <div className="flex flex-col">
                <span className="text-[10px] text-text-secondary/70 uppercase">Data Freshness</span>
                <span className="font-mono text-foreground">
                  {formatTimestamp(tokenBreakdown.data.data_freshness_ts)}
                </span>
              </div>
              <div className="flex flex-col">
                <span className="text-[10px] text-text-secondary/70 uppercase">Units</span>
                <span className="font-mono text-foreground">
                  {tokenBreakdown.data.units || "unknown"}
                </span>
              </div>
            </>
          )}

          {mode === "timeline" && planHistory?.data && (
            <>
              <div className="flex flex-col">
                <span className="text-[10px] text-text-secondary/70 uppercase">Data As Of</span>
                <span className="font-mono text-foreground">
                  {formatTimestamp(planHistory.data.data_as_of)}
                </span>
              </div>
              <div className="flex flex-col">
                <span className="text-[10px] text-text-secondary/70 uppercase">Coverage Start</span>
                <span className="font-mono text-foreground">
                  {formatTimestamp(planHistory.data.coverage_start)}
                </span>
              </div>
            </>
          )}
        </div>

        {/* Timeline Coverage Indicators */}
        {mode === "timeline" && planHistory?.data && (
          <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-midground/10">
            {planHistory.data.coverage_complete === false && (
              <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] font-bold bg-amber-500/10 text-amber-400 border border-amber-500/30">
                <AlertTriangle className="w-3.5 h-3.5" />
                Coverage Incomplete
              </span>
            )}
            {planHistory.data.coverage_complete === true && (
              <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
                <CheckCircle2 className="w-3.5 h-3.5" />
                Coverage Complete
              </span>
            )}
            {planHistory.data.approximate === true && (
              <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] font-bold bg-blue-500/10 text-blue-400 border border-blue-500/30">
                <Info className="w-3.5 h-3.5" />
                Approximate Data
              </span>
            )}
          </div>
        )}
      </div>
      )}

      {/* Loading & Top-Level Error */}
      {loading && !data && (
        <div className="border border-midground/20 rounded-lg p-12 bg-black/30 flex flex-col items-center justify-center gap-3 text-text-secondary">
          <RefreshCw className="w-6 h-6 animate-spin text-purple-400" />
          <span className="text-xs">Loading OpenAI Codex usage & quota data…</span>
        </div>
      )}

      {error && (
        <div className="p-4 text-xs text-rose-400 bg-rose-500/10 border border-rose-500/20 rounded-lg flex items-center gap-2">
          <ShieldAlert className="w-4 h-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* History Mode: Daily Token Usage Breakdown */}
      {mode === "history" && data && tokenBreakdown && (
        <div className="border border-midground/20 rounded-lg overflow-hidden bg-black/30">
          {/* Status Banners: Unsupported vs Error vs Empty */}
          {tokenBreakdown.status === "unavailable" && (
            <div className="p-4 text-xs text-amber-400 bg-amber-500/10 border-b border-amber-500/20 flex items-center gap-2">
              <AlertTriangle className="w-4 h-4 shrink-0" />
              <div>
                <span className="font-bold">Feature Unavailable: </span>
                <span>Daily token usage breakdown is unavailable or unsupported for this account.</span>
                {tokenBreakdown.error && <p className="mt-1 text-text-secondary">{tokenBreakdown.error}</p>}
              </div>
            </div>
          )}

          {(tokenBreakdown.status === "error" || tokenBreakdown.status === "invalid_response") && (
            <div className="p-4 text-xs text-rose-400 bg-rose-500/10 border-b border-rose-500/20 flex items-center gap-2">
              <ShieldAlert className="w-4 h-4 shrink-0" />
              <div>
                <span className="font-bold">Usage Error ({tokenBreakdown.status}): </span>
                <span>{tokenBreakdown.error || "Failed to retrieve token usage data."}</span>
              </div>
            </div>
          )}

          {tokenBreakdown.status === "ok" && (!tokenBreakdown.data?.days || tokenBreakdown.data.days.length === 0) && (
            <div className="py-12 text-center text-xs text-text-secondary">
              No token usage recorded for the past 7 days.
            </div>
          )}

          {/* History Table */}
          {tokenBreakdown.status === "ok" && tokenBreakdown.data?.days && tokenBreakdown.data.days.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs border-collapse">
                <thead>
                  <tr className="bg-midground/10 border-b border-midground/20 text-text-secondary text-[11px] uppercase tracking-wider select-none">
                    <th className="py-2.5 px-3">Date</th>
                    <th className="py-2.5 px-3">Model</th>
                    <th className="py-2.5 px-3 text-right">Cached Input</th>
                    <th className="py-2.5 px-3 text-right">Uncached Input</th>
                    <th className="py-2.5 px-3 text-right">Output</th>
                    <th className="py-2.5 px-4 text-right">Provider Total</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-midground/10">
                  {tokenBreakdown.data.days.flatMap((day) => {
                    if (!day.models || day.models.length === 0) {
                      return [
                        <tr key={day.date} className="hover:bg-midground/5">
                          <td className="py-2.5 px-3 whitespace-nowrap font-medium text-foreground">
                            {day.date}
                          </td>
                          <td className="py-2.5 px-3 text-text-secondary/50 italic">—</td>
                          <td className="py-2.5 px-3 text-right text-text-secondary/50 font-mono">—</td>
                          <td className="py-2.5 px-3 text-right text-text-secondary/50 font-mono">—</td>
                          <td className="py-2.5 px-3 text-right text-text-secondary/50 font-mono">—</td>
                          <td className="py-2.5 px-4 text-right text-text-secondary/50 font-mono">—</td>
                        </tr>
                      ];
                    }

                    return day.models.map((m, mIdx) => (
                      <tr key={`${day.date}-${m.model}-${mIdx}`} className="hover:bg-midground/5">
                        <td className="py-2.5 px-3 whitespace-nowrap font-medium text-foreground">
                          {mIdx === 0 ? day.date : ""}
                        </td>
                        <td className="py-2.5 px-3 whitespace-nowrap">
                          <span className="px-2 py-0.5 bg-midground/10 text-foreground rounded font-mono text-[11px] border border-midground/20">
                            {m.model || "—"}
                          </span>
                        </td>
                        <td className="py-2.5 px-3 text-right font-mono text-text-secondary">
                          {formatTokens(m.cached_text_input_tokens)}
                        </td>
                        <td className="py-2.5 px-3 text-right font-mono text-text-secondary">
                          {formatTokens(m.uncached_text_input_tokens)}
                        </td>
                        <td className="py-2.5 px-3 text-right font-mono text-text-secondary">
                          {formatTokens(m.text_output_tokens)}
                        </td>
                        <td className="py-2.5 px-4 text-right font-mono font-bold text-foreground">
                          {formatTokens(m.total_tokens)}
                        </td>
                      </tr>
                    ));
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Timeline Mode: Plan Limit Windows */}
      {mode === "timeline" && data && planHistory && (
        <div className="border border-midground/20 rounded-lg overflow-hidden bg-black/30">
          {/* Status Banners */}
          {planHistory.status === "unavailable" && (
            <div className="p-4 text-xs text-amber-400 bg-amber-500/10 border-b border-amber-500/20 flex items-center gap-2">
              <AlertTriangle className="w-4 h-4 shrink-0" />
              <div>
                <span className="font-bold">Feature Unavailable: </span>
                <span>Plan limit history is unavailable or unsupported for this account.</span>
                {planHistory.error && <p className="mt-1 text-text-secondary">{planHistory.error}</p>}
              </div>
            </div>
          )}

          {(planHistory.status === "error" || planHistory.status === "invalid_response") && (
            <div className="p-4 text-xs text-rose-400 bg-rose-500/10 border-b border-rose-500/20 flex items-center gap-2">
              <ShieldAlert className="w-4 h-4 shrink-0" />
              <div>
                <span className="font-bold">Plan Limit Error ({planHistory.status}): </span>
                <span>{planHistory.error || "Failed to retrieve plan limit history."}</span>
              </div>
            </div>
          )}

          {planHistory.status === "ok" && (!planHistory.data?.periods || planHistory.data.periods.length === 0) && (
            <div className="py-12 text-center text-xs text-text-secondary">
              No plan limit periods recorded.
            </div>
          )}

          {/* Plan Windows Table */}
          {planHistory.status === "ok" && planHistory.data?.periods && planHistory.data.periods.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs border-collapse font-mono">
                <thead>
                  <tr className="bg-midground/10 border-b border-midground/20 text-text-secondary text-[11px] uppercase tracking-wider select-none">
                    <th className="py-2.5 px-3">Window Range</th>
                    <th className="py-2.5 px-3">Duration</th>
                    <th className="py-2.5 px-3">Plan Type</th>
                    <th className="py-2.5 px-3 text-center">Quota Used</th>
                    <th className="py-2.5 px-3">Reset / End</th>
                    <th className="py-2.5 px-4 text-right">Accounting</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-midground/10">
                  {planHistory.data.periods.map((period: CodexPlanPeriod, idx: number) => {
                    const pct = period.used_basis_points !== null && period.used_basis_points !== undefined
                      ? period.used_basis_points / 100
                      : null;

                    return (
                      <tr key={`${period.starts_at}-${period.ends_at}-${idx}`} className="hover:bg-midground/5">
                        <td className="py-3 px-3 whitespace-nowrap">
                          <div className="flex flex-col">
                            <span className="font-semibold text-foreground">
                              {formatTimestamp(period.starts_at)}
                            </span>
                            <span className="text-[10px] text-text-secondary">
                              to {formatTimestamp(period.ends_at)}
                            </span>
                          </div>
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap">
                          <span className="inline-flex items-center gap-1 text-text-secondary font-mono">
                            <Clock className="w-3 h-3 text-midground" />
                            {formatDuration(period.window_minutes)}
                          </span>
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap">
                          {period.plan_type ? (
                            <span className="px-2 py-0.5 rounded text-[11px] font-bold uppercase tracking-wider bg-purple-500/10 text-purple-300 border border-purple-500/30">
                              {period.plan_type}
                            </span>
                          ) : (
                            <span className="text-text-secondary/50 font-mono">—</span>
                          )}
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap">
                          <div className="flex flex-col items-center gap-1">
                            <span className={`font-bold font-mono ${
                              pct !== null && pct > 90 ? "text-rose-400" : pct !== null && pct > 75 ? "text-amber-400" : "text-foreground"
                            }`}>
                              {formatBasisPoints(period.used_basis_points)}
                            </span>
                            {pct !== null && (
                              <div className="w-24 bg-midground/20 rounded-full h-1.5 overflow-hidden">
                                <div
                                  className={`h-full ${pct > 90 ? "bg-rose-500" : pct > 75 ? "bg-amber-500" : "bg-purple-500"}`}
                                  style={{ width: `${Math.min(100, Math.max(0, pct))}%` }}
                                />
                              </div>
                            )}
                          </div>
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap text-text-secondary">
                          <span>{formatTimestamp(period.ends_at)}</span>
                        </td>

                        <td className="py-3 px-4 whitespace-nowrap text-right">
                          {period.accounting_complete === true && (
                            <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] uppercase font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
                              <CheckCircle2 className="w-2.5 h-2.5" />
                              Complete
                            </span>
                          )}
                          {period.accounting_complete === false && (
                            <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] uppercase font-bold bg-amber-500/10 text-amber-400 border border-amber-500/30">
                              <AlertTriangle className="w-2.5 h-2.5" />
                              Pending
                            </span>
                          )}
                          {period.accounting_complete === null && (
                            <span className="text-text-secondary/50 font-mono">—</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
