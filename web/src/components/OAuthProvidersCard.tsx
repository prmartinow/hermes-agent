import { useEffect, useState, useCallback, useRef } from "react";
import { Link } from "react-router";
import {
  ShieldCheck,
  ShieldOff,
  ExternalLink,
  RefreshCw,
  Terminal,
  Clock,
  Activity,
} from "lucide-react";
import {
  api,
  type OAuthProvider,
  type DociRanking,
  type GeminiAccountStatus,
  type GeminiAccountQuota,
} from "@/lib/api";
import { Button } from "@nous-research/ui/ui/components/button";
import { CopyButton } from "@nous-research/ui/ui/components/command-block";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { ConfirmDialog } from "@nous-research/ui/ui/components/confirm-dialog";
import { OAuthLoginModal } from "@/components/OAuthLoginModal";
import { useI18n } from "@/i18n";

interface Props {
  onError?: (msg: string) => void;
  onSuccess?: (msg: string) => void;
}

interface DisconnectTarget {
  id: string;
  name: string;
}

interface LoginTarget {
  provider: OAuthProvider;
  accountId?: number;
}

function formatExpiresAt(
  expiresAt: string | null | undefined,
  expiresInTemplate: string,
): string | null {
  if (!expiresAt) return null;
  try {
    const dt = new Date(expiresAt);
    if (Number.isNaN(dt.getTime())) return null;
    const now = Date.now();
    const diff = dt.getTime() - now;
    if (diff < 0) return "expired";
    const mins = Math.floor(diff / 60_000);
    if (mins < 60) return expiresInTemplate.replace("{time}", `${mins}m`);
    const hours = Math.floor(mins / 60);
    if (hours < 24) return expiresInTemplate.replace("{time}", `${hours}h`);
    const days = Math.floor(hours / 24);
    return expiresInTemplate.replace("{time}", `${days}d`);
  } catch {
    return null;
  }
}

export function OAuthProvidersCard({ onError, onSuccess }: Props) {
  const [providers, setProviders] = useState<OAuthProvider[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [loginFor, setLoginFor] = useState<LoginTarget | null>(null);
  const [disconnectTarget, setDisconnectTarget] =
    useState<DisconnectTarget | null>(null);
  const { t } = useI18n();

  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  const isMountedRef = useRef(true);

  const refresh = useCallback((showSpinner = true) => {
    if (showSpinner) setLoading(true);
    api
      .getOAuthProviders()
      .then((resp) => {
        if (isMountedRef.current) setProviders(resp.providers);
      })
      .catch((e) => {
        if (isMountedRef.current && showSpinner) onErrorRef.current?.(`Failed to load providers: ${e}`);
      })
      .finally(() => {
        if (isMountedRef.current && showSpinner) setLoading(false);
      });
  }, []);

  useEffect(() => {
    isMountedRef.current = true;
    refresh(true);

    // Continuous active polling (every 15s) regardless of window focus
    const interval = setInterval(() => {
      refresh(false);
    }, 15000);

    // Instant refresh when user focuses or returns to the tab
    const handleFocus = () => {
      if (document.visibilityState === "visible") {
        refresh(false);
      }
    };
    window.addEventListener("focus", handleFocus);
    document.addEventListener("visibilitychange", handleFocus);

    return () => {
      isMountedRef.current = false;
      clearInterval(interval);
      window.removeEventListener("focus", handleFocus);
      document.removeEventListener("visibilitychange", handleFocus);
    };
  }, [refresh]);

  const handleDisconnect = async (target: DisconnectTarget) => {
    setBusyId(target.id);
    setDisconnectTarget(null);
    try {
      await api.disconnectOAuthProvider(target.id);
      onSuccess?.(`${target.name} disconnected`);
      refresh();
    } catch (e) {
      onError?.(`${t.oauth.disconnect} failed: ${e}`);
    } finally {
      setBusyId(null);
    }
  };

  const connectedCount =
    providers?.filter((p) => p.status.logged_in).length ?? 0;
  const totalCount = providers?.length ?? 0;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">
              {t.oauth.providerLogins}
            </CardTitle>
          </div>
          <Button
            ghost
            size="icon"
            className="text-muted-foreground hover:text-foreground"
            onClick={() => refresh(true)}
            disabled={loading}
            aria-label={t.common.refresh}
          >
            {loading ? <Spinner /> : <RefreshCw />}
          </Button>
        </div>
        <CardDescription>
          {t.oauth.description
            .replace("{connected}", String(connectedCount))
            .replace("{total}", String(totalCount))}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {loading && providers === null && (
          <div className="flex items-center justify-center py-8">
            <Spinner className="text-xl text-primary" />
          </div>
        )}
        {providers && providers.length === 0 && (
          <p className="text-sm text-muted-foreground text-center py-8">
            {t.oauth.noProviders}
          </p>
        )}
        <div className="flex flex-col divide-y divide-border">
          {providers?.map((p) => {
            const isGemini =
              p.id === "gemini-oauth" ||
              p.id === "gemini_oauth" ||
              p.id.startsWith("gemini");
            const expiresLabel = formatExpiresAt(
              p.status.expires_at,
              t.oauth.expiresIn,
            );
            const isBusy = busyId === p.id;

            if (isGemini) {
              const accounts: GeminiAccountStatus[] =
                p.status.accounts && p.status.accounts.length > 0
                  ? p.status.accounts
                  : Array.from({ length: 5 }, (_, i) => ({
                      account_id: i + 1,
                      logged_in: false,
                      email: null,
                      name: null,
                      source: `gemini_account_${i + 1}`,
                      source_label: `Google Gemini Account ${i + 1}`,
                      token_preview: null,
                      expires_at: null,
                      has_refresh_token: false,
                      quota: {} as GeminiAccountQuota,
                    }));

              const loggedCount = accounts.filter((a) => a.logged_in).length;
              const dociMap = new Map<number, DociRanking>();
              p.status.doci_rankings?.forEach((dr) => {
                dociMap.set(dr.account_id, dr);
              });

              return (
                <div key={p.id} className="flex flex-col gap-3 py-4">
                  {/* Master Slot Header */}
                  <div className="flex items-center justify-between gap-4">
                    <div className="flex items-start gap-3 min-w-0 flex-1">
                      {p.status.logged_in ? (
                        <ShieldCheck className="h-5 w-5 text-success shrink-0 mt-0.5" />
                      ) : (
                        <ShieldOff className="h-5 w-5 text-muted-foreground shrink-0 mt-0.5" />
                      )}
                      <div className="flex flex-col min-w-0 gap-1">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-medium text-sm">{p.name}</span>
                          <Badge tone="outline" className="text-xs tracking-wide">
                            {t.oauth.flowLabels[p.flow]}
                          </Badge>
                          {loggedCount > 0 ? (
                            <Badge tone="success" className="text-xs">
                              {loggedCount}/5 Accounts Active
                            </Badge>
                          ) : (
                            <Badge tone="outline" className="text-xs">
                              {t.oauth.notConnected.split("{command}")[0].trim()}
                            </Badge>
                          )}
                          <Badge tone="outline" className="text-xs text-text-tertiary">
                            DOCI Dynamic Rotation
                          </Badge>
                        </div>
                        <span className="text-xs text-text-secondary">
                          Single unified pool for all 5 accounts. Automatically rotates for maximum rate limit utilization and KV cache stickiness.
                        </span>
                      </div>
                    </div>

                    <div className="flex items-center gap-2 shrink-0">
                      <Link
                        to="/gemini/history"
                        className="inline-flex items-center gap-1 text-xs font-mono font-semibold text-midground hover:text-foreground bg-midground/10 hover:bg-midground/20 border border-midground/30 rounded px-2.5 py-1 transition-colors"
                      >
                        <Clock className="w-3.5 h-3.5" />
                        Account History
                      </Link>
                      <Link
                        to="/gemini/quota-timeline"
                        className="inline-flex items-center gap-1 text-xs font-mono font-semibold text-midground hover:text-foreground bg-midground/10 hover:bg-midground/20 border border-midground/30 rounded px-2.5 py-1 transition-colors"
                      >
                        <Activity className="w-3.5 h-3.5" />
                        Quota Timeline
                      </Link>
                      {p.docs_url && (
                        <a
                          href={p.docs_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex"
                          title={`Open ${p.name} docs`}
                        >
                          <Button ghost size="icon">
                            <ExternalLink />
                          </Button>
                        </a>
                      )}
                    </div>
                  </div>

                  {/* 5 Accounts Detailed Breakdown */}
                  <div className="grid grid-cols-1 gap-2.5 mt-1 pl-8">
                    {accounts.map((acc) => {
                      const doci = dociMap.get(acc.account_id);
                      const accBusy =
                        busyId === `gemini-oauth-${acc.account_id}` ||
                        busyId === `gemini-${acc.account_id}` ||
                        busyId === p.id;
                      const accExpires = formatExpiresAt(
                        acc.expires_at,
                        t.oauth.expiresIn,
                      );

                      return (
                        <div
                          key={acc.account_id}
                          className="border border-border/70 rounded-md p-3 bg-secondary/15 flex flex-col gap-2 transition-colors hover:border-border"
                        >
                          <div className="flex items-center justify-between gap-3">
                            <div className="flex items-center gap-2 flex-wrap min-w-0">
                              <span className="font-medium text-xs text-foreground flex items-center gap-1.5">
                                <span className={`inline-block w-2 h-2 rounded-full ${acc.logged_in ? "bg-success" : "bg-muted-foreground"}`} />
                                Account #{acc.account_id}
                              </span>

                              {acc.logged_in ? (
                                <>
                                  {acc.email && (
                                    <span
                                      className="text-xs font-mono-ui text-text-secondary cursor-help underline decoration-dotted decoration-border underline-offset-2"
                                      title={acc.email}
                                    >
                                      {acc.alias || acc.email}
                                    </span>
                                  )}
                                  <Badge tone="success" className="text-[10px] py-0 px-1.5">
                                    Connected
                                  </Badge>
                                  {doci && (
                                    <Badge
                                      tone={doci.rank === 1 ? "success" : "outline"}
                                      className="text-[10px] py-0 px-1.5"
                                      title={`DOCI Score: ${doci.doci_score ?? doci.score}`}
                                    >
                                      Rank #{doci.rank}
                                      {doci.status_note ? ` · ${doci.status_note}` : ""}
                                    </Badge>
                                  )}
                                  {accExpires && accExpires !== "expired" && (
                                    <Badge tone="outline" className="text-[10px] py-0 px-1.5">
                                      {accExpires}
                                    </Badge>
                                  )}
                                </>
                              ) : (
                                <Badge tone="outline" className="text-[10px] py-0 px-1.5 text-text-tertiary">
                                  Not Connected
                                </Badge>
                              )}
                            </div>

                            <div className="flex items-center gap-1.5 shrink-0">
                              {acc.logged_in ? (
                                <Button
                                  size="sm"
                                  outlined
                                  className="uppercase text-[11px] h-7 px-2.5"
                                  onClick={() =>
                                    setDisconnectTarget({
                                      id: `gemini-oauth-${acc.account_id}`,
                                      name: `Google Gemini Account #${acc.account_id} (${acc.alias || "Active"})`,
                                    })
                                  }
                                  disabled={accBusy}
                                  prefix={accBusy ? <Spinner /> : undefined}
                                >
                                  {t.oauth.disconnect}
                                </Button>
                              ) : (
                                <Button
                                  size="sm"
                                  className="uppercase text-[11px] h-7 px-2.5"
                                  onClick={() =>
                                    setLoginFor({
                                      provider: p,
                                      accountId: acc.account_id,
                                    })
                                  }
                                >
                                  Connect #{acc.account_id}
                                </Button>
                              )}
                            </div>
                          </div>

                          {/* Quota Telemetry Matrix */}
                          {acc.logged_in && acc.quota && (
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs font-mono-ui bg-background/60 rounded p-2 border border-border/40">
                              {/* Gemini Models */}
                              <div className="flex flex-col gap-0.5 min-w-0">
                                <span className="text-text-tertiary text-[11px] font-sans font-medium">
                                  Gemini Models (Flash / Pro)
                                </span>
                                <div className="text-text-secondary">
                                  <span className="text-text-tertiary">5h: </span>
                                  <span className="text-foreground font-medium">
                                    {typeof acc.quota.gemini_5h_percent === "number"
                                      ? `${acc.quota.gemini_5h_percent}%`
                                      : "—"}
                                  </span>
                                  {acc.quota.gemini_5h_countdown && (
                                    <span className="text-text-tertiary text-[11px]">
                                      {" "}
                                      (resets in {acc.quota.gemini_5h_countdown})
                                    </span>
                                  )}
                                </div>
                                <div className="text-text-secondary">
                                  <span className="text-text-tertiary">Weekly: </span>
                                  <span className="text-foreground font-medium">
                                    {typeof acc.quota.gemini_weekly_percent === "number"
                                      ? `${acc.quota.gemini_weekly_percent}%`
                                      : "—"}
                                  </span>
                                  {acc.quota.gemini_weekly_countdown && (
                                    <span className="text-text-tertiary text-[11px]">
                                      {" "}
                                      (resets in {acc.quota.gemini_weekly_countdown})
                                    </span>
                                  )}
                                </div>
                              </div>

                              {/* Claude & GPT Models */}
                              <div className="flex flex-col gap-0.5 min-w-0">
                                <span className="text-text-tertiary text-[11px] font-sans font-medium">
                                  Claude & GPT Models (Opus / Sonnet / GPT-OSS)
                                </span>
                                <div className="text-text-secondary">
                                  <span className="text-text-tertiary">5h: </span>
                                  <span className="text-foreground font-medium">
                                    {typeof acc.quota.claude_5h_percent === "number"
                                      ? `${acc.quota.claude_5h_percent}%`
                                      : "—"}
                                  </span>
                                  {acc.quota.claude_5h_countdown && (
                                    <span className="text-text-tertiary text-[11px]">
                                      {" "}
                                      (resets in {acc.quota.claude_5h_countdown})
                                    </span>
                                  )}
                                </div>
                                <div className="text-text-secondary">
                                  <span className="text-text-tertiary">Weekly: </span>
                                  <span className="text-foreground font-medium">
                                    {typeof acc.quota.claude_weekly_percent === "number"
                                      ? `${acc.quota.claude_weekly_percent}%`
                                      : "—"}
                                  </span>
                                  {acc.quota.claude_weekly_countdown && (
                                    <span className="text-text-tertiary text-[11px]">
                                      {" "}
                                      (resets in {acc.quota.claude_weekly_countdown})
                                    </span>
                                  )}
                                </div>
                              </div>
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            }

            // Standard rendering for other providers
            return (
              <div
                key={p.id}
                className="flex items-center justify-between gap-4 py-3"
              >
                <div className="flex items-start gap-3 min-w-0 flex-1">
                  {p.status.logged_in ? (
                    <ShieldCheck className="h-5 w-5 text-success shrink-0 mt-0.5" />
                  ) : (
                    <ShieldOff className="h-5 w-5 text-muted-foreground shrink-0 mt-0.5" />
                  )}
                  <div className="flex flex-col min-w-0 gap-0.5">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-medium text-sm">{p.name}</span>
                      <Badge tone="outline" className="text-xs tracking-wide">
                        {t.oauth.flowLabels[p.flow]}
                      </Badge>
                      {p.status.logged_in && (
                        <Badge tone="success" className="text-xs">
                          {t.oauth.connected}
                        </Badge>
                      )}
                      {expiresLabel === "expired" && (
                        <Badge tone="destructive" className="text-xs">
                          {t.oauth.expired}
                        </Badge>
                      )}
                      {expiresLabel && expiresLabel !== "expired" && (
                        <Badge tone="outline" className="text-xs">
                          {expiresLabel}
                        </Badge>
                      )}
                    </div>
                    {p.status.logged_in && p.status.token_preview && (
                      <span className="truncate text-xs font-mono-ui text-text-secondary">
                        <span className="text-text-tertiary">token </span>
                        {p.status.token_preview}
                        {p.status.source_label && (
                          <span className="text-text-tertiary">
                            {" "}
                            · {p.status.source_label}
                          </span>
                        )}
                      </span>
                    )}
                    {!p.status.logged_in && (
                      <>
                        <span className="text-xs text-text-secondary">
                          {t.oauth.notConnected.split("{command}")[0].trimEnd()}
                          {t.oauth.notConnected.split("{command}")[1] ?? ""}
                        </span>

                        <div className="flex min-w-0 flex-wrap items-center gap-2">
                          <code className="font-courier truncate text-xs opacity-60">
                            {p.cli_command}
                          </code>

                          <CopyButton
                            text={p.cli_command}
                            label={t.oauth.cli}
                            copiedLabel={t.oauth.copied}
                          />
                        </div>
                      </>
                    )}
                    {p.status.error && (
                      <span className="text-xs text-destructive">
                        {p.status.error}
                      </span>
                    )}
                  </div>
                </div>

                <div className="flex items-center gap-1.5 shrink-0">
                  {p.docs_url && (
                    <a
                      href={p.docs_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex"
                      title={`Open ${p.name} docs`}
                    >
                      <Button ghost size="icon">
                        <ExternalLink />
                      </Button>
                    </a>
                  )}
                  {!p.status.logged_in && p.flow !== "external" && (
                    <Button
                      size="sm"
                      className="uppercase"
                      onClick={() => setLoginFor({ provider: p })}
                    >
                      {t.oauth.login}
                    </Button>
                  )}
                  {p.status.logged_in && p.flow !== "external" && (
                    <Button
                      size="sm"
                      outlined
                      className="uppercase"
                      onClick={() =>
                        setDisconnectTarget({
                          id: p.id,
                          name: p.name,
                        })
                      }
                      disabled={isBusy}
                      prefix={isBusy ? <Spinner /> : undefined}
                    >
                      {t.oauth.disconnect}
                    </Button>
                  )}
                  {p.status.logged_in && p.flow === "external" && (
                    <span className="text-xs text-text-tertiary italic px-2">
                      <Terminal className="h-3 w-3 inline mr-0.5" />
                      {t.oauth.managedExternally}
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </CardContent>
      {loginFor && (
        <OAuthLoginModal
          provider={loginFor.provider}
          accountId={loginFor.accountId}
          onClose={() => {
            setLoginFor(null);
            refresh();
          }}
          onSuccess={(msg) => onSuccess?.(msg)}
          onError={(msg) => onError?.(msg)}
        />
      )}
      <ConfirmDialog
        open={disconnectTarget !== null}
        onCancel={() => setDisconnectTarget(null)}
        onConfirm={() => {
          if (disconnectTarget) void handleDisconnect(disconnectTarget);
        }}
        title={`${t.oauth.disconnect} ${disconnectTarget?.name ?? ""}?`}
        description={`This will remove stored OAuth credentials for ${disconnectTarget?.name ?? "this account/provider"}. You will need to re-authenticate to use it again.`}
        destructive
        confirmLabel={t.oauth.disconnect}
      />
    </Card>
  );
}
