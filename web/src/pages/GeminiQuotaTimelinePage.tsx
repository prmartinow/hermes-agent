import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  RefreshCw,
  Sparkles,
  Zap,
} from "lucide-react";
import {
  api,
  type GeminiAccountMeta,
  type GeminiQuotaInterval,
  type GeminiQuotaTimelineResponse,
} from "@/lib/api";

type ModelGroupOption = "gemini" | "claude";

export default function GeminiQuotaTimelinePage() {
  const [timelineData, setTimelineData] = useState<GeminiQuotaTimelineResponse | null>(null);
  const [modelGroup, setModelGroup] = useState<ModelGroupOption>("gemini");
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(
    async (isBackground = false) => {
      if (!isBackground) {
        setLoading(true);
      }
      setError(null);

      try {
        const res = await api.getGeminiQuotaTimeline({
          timespan: "24h",
          model_group: modelGroup,
        });
        setTimelineData(res);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load Gemini quota timeline");
      } finally {
        if (!isBackground) {
          setLoading(false);
        }
      }
    },
    [modelGroup]
  );

  useEffect(() => {
    loadData(false);
  }, [loadData]);

  // Auto-refresh every 30s when tab is visible
  useEffect(() => {
    const interval = setInterval(() => {
      if (document.visibilityState === "visible") {
        loadData(true);
      }
    }, 30000);
    return () => clearInterval(interval);
  }, [loadData]);

  const defaultAccountAliases = useMemo(() => ["account_1", "account_2", "account_3", "account_4", "account_5"], []);

  const accountsMeta: GeminiAccountMeta[] = useMemo(() => {
    if (timelineData?.accounts_meta && timelineData.accounts_meta.length > 0) {
      return timelineData.accounts_meta;
    }
    return defaultAccountAliases.map((alias, i) => ({
      account_id: i + 1,
      alias,
      logged_in: false,
    }));
  }, [timelineData, defaultAccountAliases]);

  const sortedIntervals = useMemo(() => {
    if (!timelineData?.intervals) return [];
    return [...timelineData.intervals].reverse();
  }, [timelineData]);

  const getPercentBadge = (pct: number | null | undefined, loggedIn = true) => {
    if (!loggedIn || pct === null || pct === undefined) {
      return <span className="text-text-secondary/40 font-mono text-[11px]">—</span>;
    }

    return (
      <span
        className="text-[11px] font-mono font-medium text-foreground leading-none"
        title={`${pct.toFixed(1)}% remaining`}
      >
        {Math.round(pct)}%
      </span>
    );
  };

  const getPercentCell = (pct: number | null | undefined, resetStr?: string | null, loggedIn = true) => {
    if (!loggedIn || pct === null || pct === undefined) {
      return <span className="text-text-secondary/40 font-mono text-[11px]">—</span>;
    }

    return (
      <div className="flex items-center justify-center gap-1.5 whitespace-nowrap text-[11px] font-mono">
        <span className="font-medium text-foreground text-right w-9 shrink-0 leading-none">
          {Math.round(pct)}%
        </span>
        <span
          className="text-[10px] text-text-secondary/70 text-left w-16 shrink-0 tracking-tight"
          title={resetStr ? `Resets in ${resetStr}` : undefined}
        >
          {resetStr ? `(${resetStr})` : ""}
        </span>
      </div>
    );
  };

  const getRankBadge = (rank: number | null | undefined, loggedIn = true) => {
    if (!loggedIn || !rank) {
      return <span className="text-text-secondary/40 font-mono text-[11px]">—</span>;
    }

    return (
      <span
        className="text-[11px] font-mono font-medium text-foreground"
        title={`Rank #${rank}`}
      >
        #{rank}
      </span>
    );
  };

  return (
    <div className="flex flex-col gap-5 p-4 sm:p-6 max-w-[1600px] mx-auto w-full font-mono text-foreground">
      {/* Header & Controls */}
      <div className="flex flex-col gap-4 border-b border-midground/20 pb-4">
        <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
          <div>
            <div className="flex items-center gap-2.5">
              <Activity className="w-5 h-5 text-emerald-400" />
              <h1 className="text-xl font-bold tracking-wider uppercase text-foreground">
                Gemini 5-Account Quota & Rank Timeline
              </h1>
            </div>
            <p className="text-xs text-text-secondary mt-1">
              15-minute interval usage rates (5h capacity, 7d weekly) & DOCI opportunity-cost rankings across all 5 accounts.
            </p>
          </div>
        </div>

        {/* Segmented Control: Model Group */}
        <div className="flex items-center gap-1 p-1 bg-black/50 border border-midground/30 rounded-lg w-fit">
          <button
            type="button"
            onClick={() => setModelGroup("gemini")}
            className={`flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-bold uppercase tracking-wider rounded-md transition-all ${
              modelGroup === "gemini"
                ? "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 shadow-sm"
                : "text-text-secondary hover:text-foreground hover:bg-midground/10 border border-transparent"
            }`}
          >
            <Zap className="w-3.5 h-3.5" />
            <span>Gemini Quota</span>
          </button>
          <button
            type="button"
            onClick={() => setModelGroup("claude")}
            className={`flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-bold uppercase tracking-wider rounded-md transition-all ${
              modelGroup === "claude"
                ? "bg-purple-500/20 text-purple-300 border border-purple-500/40 shadow-sm"
                : "text-text-secondary hover:text-foreground hover:bg-midground/10 border border-transparent"
            }`}
          >
            <Sparkles className="w-3.5 h-3.5" />
            <span>Claude & 3P Quota</span>
          </button>
        </div>
      </div>

      {/* Account Overview Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
        {accountsMeta.map((acc) => {
          return (
            <div
              key={acc.account_id}
              className="p-3.5 rounded-lg border border-midground/20 bg-black/40 flex flex-col gap-2 transition-all"
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-1.5">
                  <span className="text-sm font-bold tracking-wide text-foreground uppercase">{acc.alias}</span>
                  <span className="text-[10px] text-text-secondary">#{acc.account_id}</span>
                </div>
                <div>{getRankBadge(acc.current_rank, acc.logged_in)}</div>
              </div>

              <div className="grid grid-cols-[28px_1fr_auto] items-center text-xs pt-1 border-t border-midground/10">
                <span className="text-text-secondary text-[11px] font-semibold text-left">5h</span>
                <span className="text-[10px] text-text-secondary/70 font-mono text-center whitespace-nowrap px-1">
                  {acc.logged_in && acc.current_5h_reset ? acc.current_5h_reset : "—"}
                </span>
                <div className="flex justify-end">{getPercentBadge(acc.current_5h_pct, acc.logged_in)}</div>
              </div>

              <div className="grid grid-cols-[28px_1fr_auto] items-center text-xs">
                <span className="text-text-secondary text-[11px] font-semibold text-left">7d</span>
                <span className="text-[10px] text-text-secondary/70 font-mono text-center whitespace-nowrap px-1">
                  {acc.logged_in && acc.current_7d_reset ? acc.current_7d_reset : "—"}
                </span>
                <div className="flex justify-end">{getPercentBadge(acc.current_7d_pct, acc.logged_in)}</div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Main 15-Minute Timeline Matrix Table */}
      <div className="border border-midground/20 rounded-lg overflow-hidden bg-black/30 shadow-md">
        {error && (
          <div className="p-4 text-xs text-rose-400 bg-rose-500/10 border-b border-rose-500/20 flex items-center gap-2">
            <span>{error}</span>
          </div>
        )}

        <div className="overflow-x-auto max-h-[700px]">
          <table className="w-full text-left text-xs border-collapse font-mono">
            {/* Sticky Two-Tier Header */}
            <thead className="sticky top-0 z-20 bg-[#0f1115] shadow-sm">
              {/* Row 1: Account Column Headers */}
              <tr className="border-b border-midground/20 text-text-secondary text-[11px] uppercase tracking-wider select-none bg-midground/10">
                <th className="py-3 px-3.5 font-bold text-foreground border-r border-midground/20 w-32 min-w-[120px] sticky left-0 z-30 bg-[#0f1115]">
                  Time
                </th>
                {accountsMeta.map((acc) => (
                  <th
                    key={acc.account_id}
                    colSpan={3}
                    className="py-2.5 px-2 text-center font-bold text-foreground border-r border-midground/20 last:border-r-0 bg-midground/5"
                  >
                    <div className="flex items-center justify-center gap-1.5">
                      <span className="text-xs uppercase tracking-wider text-midground">{acc.alias}</span>
                      <span className="text-[10px] text-text-secondary/60">(acc {acc.account_id})</span>
                    </div>
                  </th>
                ))}
              </tr>

              {/* Row 2: Sub-columns for 5h, 7d, Acc Rank */}
              <tr className="border-b border-midground/30 text-[10px] uppercase tracking-wider select-none text-text-secondary bg-black/60">
                <th className="py-2 px-3.5 font-semibold text-text-secondary/80 border-r border-midground/20 sticky left-0 z-30 bg-[#0c0d10]">
                  Slot
                </th>
                {accountsMeta.map((acc) => (
                  <React.Fragment key={acc.account_id}>
                    <th className="py-2 px-2 text-center font-semibold text-text-secondary/90 w-28 min-w-[110px]">
                      5h
                    </th>
                    <th className="py-2 px-2 text-center font-semibold text-text-secondary/90 w-28 min-w-[110px]">
                      7d
                    </th>
                    <th className="py-2 px-2 text-center font-semibold text-text-secondary/90 w-16 min-w-[50px] border-r border-midground/20 last:border-r-0">
                      Rank
                    </th>
                  </React.Fragment>
                ))}
              </tr>
            </thead>

            {/* Table Body */}
            <tbody className="divide-y divide-midground/10 text-xs">
              {loading && sortedIntervals.length === 0 ? (
                <tr>
                  <td colSpan={16} className="py-12 text-center text-text-secondary">
                    <div className="flex flex-col items-center justify-center gap-2">
                      <RefreshCw className="w-5 h-5 animate-spin text-emerald-400" />
                      <span>Loading quota timeline data...</span>
                    </div>
                  </td>
                </tr>
              ) : sortedIntervals.length === 0 ? (
                <tr>
                  <td colSpan={16} className="py-8 text-center text-text-secondary">
                    No timeline intervals found.
                  </td>
                </tr>
              ) : (
                sortedIntervals.map((interval: GeminiQuotaInterval) => {
                  const slotTime = (() => {
                    try {
                      const d = new Date(interval.epoch * 1000);
                      return {
                        time: d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }),
                        date: d.toLocaleDateString([], { month: "short", day: "numeric" }),
                      };
                    } catch {
                      return { time: interval.time_label, date: interval.date_label };
                    }
                  })();

                  return (
                    <tr
                      key={interval.epoch}
                      className={`hover:bg-midground/10 transition-colors ${
                        interval.is_current ? "bg-emerald-500/5 font-semibold" : ""
                      }`}
                    >
                      {/* Time Column */}
                      <td className="py-2.5 px-3.5 border-r border-midground/20 whitespace-nowrap sticky left-0 z-10 bg-[#0c0d10] font-mono">
                        <div className="flex items-center gap-2">
                          <span className="font-bold text-foreground text-xs">{slotTime.time}</span>
                          <span className="text-[10px] text-text-secondary">{slotTime.date}</span>
                          {interval.is_current && (
                            <span className="px-1 py-0.2 rounded bg-emerald-500/20 text-emerald-300 text-[9px] border border-emerald-500/40 uppercase font-bold tracking-tighter">
                              NOW
                            </span>
                          )}
                        </div>
                      </td>

                      {/* Account Cells */}
                      {accountsMeta.map((acc) => {
                        const accData = interval.accounts[acc.alias] || interval.accounts[String(acc.account_id)];

                        return (
                          <React.Fragment key={acc.account_id}>
                            {/* 5h Sub-column */}
                            <td className="py-2 px-2 text-center">
                              {getPercentCell(accData?.cap_5h, accData?.reset_5h, accData?.logged_in ?? acc.logged_in)}
                            </td>

                            {/* 7d Weekly Sub-column */}
                            <td className="py-2 px-2 text-center">
                              {getPercentCell(accData?.cap_7d, accData?.reset_7d, accData?.logged_in ?? acc.logged_in)}
                            </td>

                            {/* Acc Rank Sub-column */}
                            <td className="py-2 px-2 text-center border-r border-midground/20 last:border-r-0">
                              {getRankBadge(accData?.rank, accData?.logged_in ?? acc.logged_in)}
                            </td>
                          </React.Fragment>
                        );
                      })}
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
