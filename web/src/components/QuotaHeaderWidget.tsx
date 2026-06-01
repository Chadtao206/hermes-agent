import { useEffect, useMemo, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api, type QuotaProviderStatus, type QuotaStatusResponse, type QuotaWindow } from "@/lib/api";
import { cn } from "@/lib/utils";

const POLL_MS = 90_000;

function formatReset(value?: string | null): string {
  if (!value) return "reset unknown";
  const when = new Date(value);
  if (Number.isNaN(when.getTime())) return "reset unknown";
  const now = Date.now();
  const deltaMs = when.getTime() - now;
  if (deltaMs <= 0) return "reset soon";
  const minutes = Math.round(deltaMs / 60_000);
  if (minutes < 60) return `resets in ${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `resets in ${hours}h`;
  const days = Math.round(hours / 24);
  return `resets in ${days}d`;
}

function formatUpdated(value?: string | null): string {
  if (!value) return "no local snapshot";
  const when = new Date(value);
  if (Number.isNaN(when.getTime())) return "snapshot time unknown";
  return `updated ${when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
}

function windowText(window?: QuotaWindow): string {
  if (!window || window.used_percent == null) return "—";
  const numeric = Number(window.used_percent);
  if (!Number.isFinite(numeric)) return "—";
  return `${Math.round(numeric)}%`;
}

function windowTitle(window?: QuotaWindow): string {
  if (!window) return "No quota window exposed by this provider/source";
  return `${window.label}: ${windowText(window)} used, ${formatReset(window.resets_at_iso)}`;
}

function isStaleSnapshot(value?: string | null): boolean {
  if (!value) return false;
  const when = new Date(value);
  if (Number.isNaN(when.getTime())) return false;
  return Date.now() - when.getTime() > 12 * 60 * 60 * 1000;
}

function planText(provider: QuotaProviderStatus): string {
  const plan = provider.plan_type;
  if (provider.provider === "codex" && plan === "self_serve_business_usage_based") {
    const credits = provider.credits;
    if (credits?.overage_limit_reached === true) return "usage-based · overage limit hit";
    if (typeof credits?.balance === "number") return `usage-based · $${credits.balance.toFixed(2)} credits`;
    return "usage-based · no fixed 5h/W";
  }
  if (provider.provider === "claude" && plan) {
    return `${String(plan).toUpperCase()} · usage hidden`;
  }
  if (provider.error === "codex_plan_has_no_fixed_5h_or_weekly_windows") return "no fixed 5h/W";
  if (provider.error?.includes("quota_windows_not_exposed")) return "usage hidden";
  return "quota unavailable";
}

function providerStatusTitle(provider: QuotaProviderStatus): string {
  const base = `${provider.label}: ${planText(provider)} (${provider.source})`;
  if (provider.provider === "codex" && provider.plan_type === "self_serve_business_usage_based") {
    return `${base} — Codex live app-server returned primary/secondary quota windows as empty for this usage-based account${
      provider.credits?.has_credits === true ? "; credits are active" : ""
    }`;
  }
  if (provider.error) return `${base} — ${provider.error}`;
  return base;
}

function ProviderChip({ provider }: { provider: QuotaProviderStatus }) {
  const fiveHour = provider.windows.five_hour;
  const weekly = provider.windows.weekly;
  const stale = isStaleSnapshot(provider.updated_at);
  const windows = Object.values(provider.windows);
  const hasAny = provider.available && windows.length > 0;
  const fallbackWindow = !fiveHour && !weekly ? windows[0] : undefined;
  const title = hasAny
    ? `${provider.label}: ${formatUpdated(provider.updated_at)} (${provider.source})${stale ? " — stale snapshot" : ""}`
    : providerStatusTitle(provider);

  return (
    <div
      className={cn(
        "flex items-center gap-1 rounded-full border px-2 py-1 leading-none",
        hasAny && !stale
          ? "border-current/20 bg-background-base/45 text-midground"
          : "border-current/10 bg-background-base/20 text-muted-foreground",
      )}
      title={title}
    >
      <span className="font-expanded text-[10px] font-bold tracking-[0.08em]">
        {provider.provider === "codex" ? "Codex" : "Claude"}
      </span>
      {fallbackWindow ? (
        <span className="text-[10px]" title={windowTitle(fallbackWindow)}>
          {fallbackWindow.label} {windowText(fallbackWindow)}{stale ? " stale" : ""}
        </span>
      ) : hasAny ? (
        <>
          <span className="text-[10px]" title={windowTitle(fiveHour)}>
            5h {windowText(fiveHour)}
          </span>
          <span className="text-current/30">·</span>
          <span className="text-[10px]" title={windowTitle(weekly)}>
            W {windowText(weekly)}{stale ? " stale" : ""}
          </span>
        </>
      ) : (
        <span className="max-w-[11rem] truncate text-[10px]">{planText(provider)}</span>
      )}
    </div>
  );
}

export function QuotaHeaderWidget() {
  const [data, setData] = useState<QuotaStatusResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      setLoading(true);
      try {
        const result = await api.getQuotaStatus();
        if (!cancelled) {
          setData(result);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();
    const interval = window.setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  const providers = useMemo(() => {
    if (!data) return [];
    return [data.providers.codex, data.providers.claude].filter(Boolean);
  }, [data]);

  if (error && !data) {
    return (
      <div
        className="hidden rounded-full border border-destructive/30 bg-destructive/10 px-2 py-1 text-[10px] text-destructive sm:block"
        title={error}
      >
        quotas unavailable
      </div>
    );
  }

  if (!providers.length) {
    return (
      <div className="hidden items-center gap-1 rounded-full border border-current/10 bg-background-base/20 px-2 py-1 text-[10px] text-muted-foreground sm:flex">
        <RefreshCw className={cn("h-3 w-3", loading && "animate-spin")} />
        quotas
      </div>
    );
  }

  return (
    <div className="hidden min-w-0 items-center gap-1 sm:flex" aria-label="Code assistant quota status">
      {providers.map((provider) => (
        <ProviderChip key={provider.provider} provider={provider} />
      ))}
      {loading ? <RefreshCw className="h-3 w-3 animate-spin text-muted-foreground" /> : null}
    </div>
  );
}
