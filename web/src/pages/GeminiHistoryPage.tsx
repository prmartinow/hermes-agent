import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import {
  ArrowRight,
  ArrowUpDown,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  ExternalLink,
  History,
  MessageSquare,
  Shield,
} from "lucide-react";
import { api, type GeminiSessionAccountHistory } from "../lib/api";

type SortField =
  | "title"
  | "is_subagent"
  | "current_alias"
  | "turns_count"
  | "changes_count"
  | "started_at"
  | "last_activity_at";

type SortDirection = "asc" | "desc" | null;

export default function GeminiHistoryPage() {
  const [sessions, setSessions] = useState<GeminiSessionAccountHistory[]>([]);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [includeSubagents, setIncludeSubagents] = useState(false);
  const [sortField, setSortField] = useState<SortField | null>("turns_count");
  const [sortDir, setSortDir] = useState<SortDirection>("desc");

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    setError(null);
    try {
      const res = await api.getGeminiSessionHistories();
      setSessions(res.sessions || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load account histories");
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    const interval = setInterval(() => {
      if (document.visibilityState === "visible") {
        load(true);
      }
    }, 15000);
    return () => clearInterval(interval);
  }, [load]);

  const toggleExpand = (sid: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(sid)) {
        next.delete(sid);
      } else {
        next.add(sid);
      }
      return next;
    });
  };

  const handleSort = (field: SortField) => {
    if (sortField !== field) {
      setSortField(field);
      setSortDir("asc");
    } else if (sortDir === "asc") {
      setSortDir("desc");
    } else {
      setSortField(null);
      setSortDir(null);
    }
  };

  const parseEpoch = (ts: string | number) => {
    if (typeof ts === "number") return ts;
    const parsed = Date.parse(ts);
    return isNaN(parsed) ? 0 : parsed / 1000;
  };

  const sortedSessions = useMemo(() => {
    let list = sessions;
    if (!includeSubagents) {
      list = list.filter((s) => !s.is_subagent);
    }
    if (!sortField || !sortDir) return list;
    return [...list].sort((a, b) => {
      const valA: string | number = sortField === "is_subagent" ? (a.is_subagent ? 1 : 0) : (a[sortField] ?? "");
      const valB: string | number = sortField === "is_subagent" ? (b.is_subagent ? 1 : 0) : (b[sortField] ?? "");

      if (typeof valA === "string" && typeof valB === "string") {
        const cmp = valA.localeCompare(valB, undefined, { sensitivity: "base" });
        return sortDir === "asc" ? cmp : -cmp;
      }

      if (Number(valA) < Number(valB)) return sortDir === "asc" ? -1 : 1;
      if (Number(valA) > Number(valB)) return sortDir === "asc" ? 1 : -1;
      return 0;
    });
  }, [sessions, sortField, sortDir, includeSubagents]);

  const formatTimestamp = (ts: string | number) => {
    if (!ts) return "—";
    try {
      const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
      return (
        d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) +
        " · " +
        d.toLocaleDateString([], { month: "short", day: "numeric" })
      );
    } catch {
      return String(ts);
    }
  };

  const getBadgeColor = (type: string) => {
    switch (type) {
      case "session_pin":
      case "initial_pin":
        return "bg-teal-500/10 text-teal-400 border-teal-500/30";
      case "quota_failover":
        return "bg-rose-500/10 text-rose-400 border-rose-500/30";
      case "switch":
      case "account_switch":
        return "bg-amber-500/10 text-amber-400 border-amber-500/30";
      case "turn":
        return "bg-cyan-500/10 text-cyan-400 border-cyan-500/30";
      default:
        return "bg-blue-500/10 text-blue-400 border-blue-500/30";
    }
  };

  const renderSortIcon = (field: SortField) => {
    if (sortField !== field) {
      return <ArrowUpDown className="w-3 h-3 text-text-secondary/30 inline ml-1" />;
    }
    if (sortDir === "asc") {
      return <ChevronUp className="w-3 h-3 text-midground inline ml-1" />;
    }
    return <ChevronDown className="w-3 h-3 text-midground inline ml-1" />;
  };

  return (
    <div className="flex flex-col gap-5 p-6 max-w-7xl mx-auto w-full font-mono">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-midground/20 pb-4">
        <div>
          <div className="flex items-center gap-2">
            <Shield className="w-5 h-5 text-midground" />
            <h1 className="text-xl font-bold tracking-wider text-foreground">
              GEMINI ACCOUNT ROTATION HISTORY
            </h1>
          </div>
          <p className="text-xs text-text-secondary mt-1">
            Click on any chat to expand its log timeline showing every Gemini model turn, account rotation, and failover event.
          </p>
        </div>

        <div className="flex items-center gap-2.5">
          <button
            type="button"
            onClick={() => setIncludeSubagents((v) => !v)}
            className={`px-2.5 py-1 text-xs border rounded font-bold uppercase transition-colors ${
              includeSubagents
                ? "bg-purple-500/20 text-purple-300 border-purple-500/40"
                : "bg-transparent text-text-secondary border-midground/20 hover:text-foreground hover:bg-midground/10"
            }`}
            title={includeSubagents ? "Hide Subagents" : "Include Subagents"}
          >
            SUB
          </button>
        </div>
      </div>

      {/* Main Chats & Account History Table */}
      <div className="border border-midground/20 rounded overflow-hidden bg-black/30">
        {error && (
          <div className="p-4 text-xs text-rose-400 bg-rose-500/10 border-b border-rose-500/20">
            {error}
          </div>
        )}

        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs border-collapse">
            <thead>
              <tr className="bg-midground/10 border-b border-midground/20 text-text-secondary text-[11px] uppercase tracking-wider select-none">
                <th className="py-2.5 px-3 w-8"></th>
                <th
                  onClick={() => handleSort("title")}
                  className="py-2.5 px-3 font-semibold cursor-pointer hover:text-foreground transition-colors max-w-[220px]"
                >
                  Chat {renderSortIcon("title")}
                </th>
                <th
                  onClick={() => handleSort("is_subagent")}
                  className="py-2.5 px-3 font-semibold cursor-pointer hover:text-foreground transition-colors"
                >
                  Type {renderSortIcon("is_subagent")}
                </th>
                <th
                  onClick={() => handleSort("current_alias")}
                  className="py-2.5 px-3 font-semibold cursor-pointer hover:text-foreground transition-colors"
                >
                  Acc {renderSortIcon("current_alias")}
                </th>
                <th
                  onClick={() => handleSort("turns_count")}
                  className="py-2.5 px-3 font-semibold cursor-pointer hover:text-foreground transition-colors"
                >
                  Turns {renderSortIcon("turns_count")}
                </th>
                <th
                  onClick={() => handleSort("changes_count")}
                  className="py-2.5 px-3 font-semibold cursor-pointer hover:text-foreground transition-colors"
                >
                  Rotations {renderSortIcon("changes_count")}
                </th>
                <th
                  onClick={() => handleSort("started_at")}
                  className="py-2.5 px-3 font-semibold cursor-pointer hover:text-foreground transition-colors"
                >
                  Started {renderSortIcon("started_at")}
                </th>
                <th
                  onClick={() => handleSort("last_activity_at")}
                  className="py-2.5 px-3 font-semibold cursor-pointer hover:text-foreground transition-colors"
                >
                  Last Activity {renderSortIcon("last_activity_at")}
                </th>
                <th className="py-2.5 px-4 font-semibold text-right">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-midground/10">
              {sortedSessions.length === 0 ? (
                <tr>
                  <td colSpan={9} className="py-12 text-center text-text-secondary">
                    {loading ? "Loading chat histories…" : "No Gemini chat sessions found."}
                  </td>
                </tr>
              ) : (
                sortedSessions.map((sess) => {
                  const isExpanded = expandedIds.has(sess.session_id);
                  const eventsDesc = [...sess.events].sort(
                    (a, b) => parseEpoch(b.timestamp) - parseEpoch(a.timestamp)
                  );

                  return (
                    <React.Fragment key={sess.session_id}>
                      {/* Main Chat Row */}
                      <tr
                        onClick={() => toggleExpand(sess.session_id)}
                        className={`cursor-pointer transition-colors ${
                          isExpanded ? "bg-midground/10" : "hover:bg-midground/5"
                        }`}
                      >
                        <td className="py-3 px-3 text-text-secondary text-center">
                          {isExpanded ? (
                            <ChevronDown className="w-4 h-4 text-midground inline" />
                          ) : (
                            <ChevronRight className="w-4 h-4 text-text-secondary/60 inline" />
                          )}
                        </td>

                        <td className="py-3 px-3 max-w-[220px]">
                          <div className="font-bold text-foreground truncate" title={sess.title}>
                            {sess.title}
                          </div>
                          <span className="text-[10px] text-text-secondary/70 font-mono truncate block">
                            {sess.session_id}
                          </span>
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap">
                          {sess.is_subagent ? (
                            <span className="inline-block px-2 py-0.5 text-[10px] uppercase font-bold border rounded bg-purple-500/10 text-purple-300 border-purple-500/30">
                              Sub
                            </span>
                          ) : (
                            <span className="inline-block px-2 py-0.5 text-[10px] uppercase font-bold border rounded bg-teal-500/10 text-teal-300 border-teal-500/30">
                              User
                            </span>
                          )}
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap">
                          <span className="px-2 py-0.5 bg-teal-500/20 text-teal-300 rounded font-bold border border-teal-500/40">
                            {sess.current_alias}
                          </span>
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap">
                          <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-midground/15 rounded text-[11px] font-bold border border-midground/30">
                            <MessageSquare className="w-3 h-3 text-midground" />
                            {sess.turns_count ?? sess.message_count ?? 0}
                          </span>
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap">
                          <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-midground/15 rounded text-[11px] font-bold border border-midground/30">
                            <History className="w-3 h-3 text-midground" />
                            {sess.changes_count}
                          </span>
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap text-text-secondary">
                          <span title={String(sess.started_at)}>
                            {formatTimestamp(sess.started_at)}
                          </span>
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap text-text-secondary">
                          <span title={String(sess.last_activity_at || sess.started_at)}>
                            {formatTimestamp(sess.last_activity_at || sess.started_at)}
                          </span>
                        </td>

                        <td className="py-3 px-4 text-right whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
                          <Link
                            to={`/chat?resume=${encodeURIComponent(sess.session_id)}`}
                            className="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-bold bg-midground/10 hover:bg-midground/20 text-midground border border-midground/30 rounded transition-colors"
                          >
                            Open Chat
                            <ExternalLink className="w-2.5 h-2.5" />
                          </Link>
                        </td>
                      </tr>

                      {/* Expanded Sub-Timeline for this specific chat */}
                      {isExpanded && (
                        <tr className="bg-black/60 border-y border-midground/20">
                          <td colSpan={9} className="p-0">
                            {eventsDesc.length === 0 ? (
                              <div className="py-4 pl-12 pr-4 text-xs text-text-secondary">
                                No account events recorded for this session.
                              </div>
                            ) : (
                              <table className="w-full text-left text-xs border-collapse bg-black/40">
                                <thead>
                                  <tr className="bg-midground/15 border-b border-midground/20 text-text-secondary text-[10px] uppercase">
                                    <th className="py-2.5 pl-12 pr-3 w-44">Timestamp</th>
                                    <th className="py-2.5 px-3 w-28">Event / Turn</th>
                                    <th className="py-2.5 px-2 w-20">Account</th>
                                    <th className="py-2.5 px-3">Turn Prompt / Details</th>
                                  </tr>
                                </thead>
                                <tbody className="divide-y divide-midground/10">
                                  {eventsDesc.map((evt, idx) => (
                                    <tr key={evt.id || idx} className="hover:bg-midground/5">
                                      <td className="py-2 pl-12 pr-3 whitespace-nowrap text-text-secondary font-mono">
                                        {formatTimestamp(evt.timestamp)}
                                      </td>

                                      <td className="py-2 px-3 whitespace-nowrap">
                                        <span
                                          className={`inline-block px-1.5 py-0.5 text-[9px] uppercase font-bold border rounded ${getBadgeColor(
                                            evt.event_type,
                                          )}`}
                                        >
                                          {evt.event_type === "turn"
                                            ? `TURN ${evt.turn_number ? `#${evt.turn_number}` : ""}`
                                            : evt.event_type.replace(/_/g, " ")}
                                        </span>
                                      </td>

                                      <td className="py-2 px-2 whitespace-nowrap w-20">
                                        {evt.to_alias ? (
                                          <div className="flex items-center gap-1 font-mono">
                                            {evt.from_alias ? (
                                              <>
                                                <span className="px-1.5 py-0.5 bg-midground/15 rounded text-foreground font-bold border border-midground/30">
                                                  {evt.from_alias}
                                                </span>
                                                <ArrowRight className="w-2.5 h-2.5 text-midground/70" />
                                              </>
                                            ) : null}
                                            <span className="px-1.5 py-0.5 bg-teal-500/20 text-teal-300 rounded font-bold border border-teal-500/40">
                                              {evt.to_alias}
                                            </span>
                                          </div>
                                        ) : (
                                          <span className="text-text-secondary/40 font-mono text-[11px]">—</span>
                                        )}
                                      </td>

                                      <td className="py-2 px-3 text-text-secondary">
                                        <div className="flex items-center gap-2">
                                          {evt.event_type === "turn" ? (
                                            <MessageSquare className="w-3.5 h-3.5 text-cyan-400 shrink-0" />
                                          ) : (
                                            <History className="w-3.5 h-3.5 text-amber-400 shrink-0" />
                                          )}
                                          <span className="text-foreground font-medium truncate max-w-xl" title={evt.details}>
                                            {evt.details}
                                          </span>
                                          {evt.api_calls ? (
                                            <span className="text-[10px] text-text-secondary/60 font-mono whitespace-nowrap">
                                              ({evt.api_calls} {evt.api_calls === 1 ? "API call" : "API calls"})
                                            </span>
                                          ) : null}
                                        </div>
                                      </td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            )}
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
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
