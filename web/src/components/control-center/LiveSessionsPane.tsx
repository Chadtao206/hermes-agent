import { MessageSquare } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ControlCenterLiveSession } from "@/lib/api";

export interface LiveSessionsPaneProps {
  sessions: ControlCenterLiveSession[] | null;
  onInterrupt?: (session: ControlCenterLiveSession) => void;
  onSteer?: (session: ControlCenterLiveSession) => void;
  onSubmit?: (session: ControlCenterLiveSession) => void;
}

function ago(ts: number | null | undefined): string {
  if (!ts) return "unknown";
  const secs = Math.max(0, Math.floor((Date.now() / 1000) - ts));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours}h ${mins % 60}m ago`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h ago`;
}

function durationSince(ts: number | null | undefined): string {
  if (!ts) return "unknown";
  const secs = Math.max(0, Math.floor((Date.now() / 1000) - ts));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ${secs % 60}s`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours}h ${mins % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

function formatTimestamp(ts: number | null | undefined): string {
  if (!ts) return "unknown";
  return new Date(ts * 1000).toLocaleString();
}

function shortId(id: string): string {
  return id.length > 20 ? `${id.slice(0, 20)}…` : id;
}

function statusPillClass(kind: "running" | "awaiting" | "idle" | "pending"): string {
  if (kind === "running") return "border-green-300 text-green-700 dark:border-green-900 dark:text-green-300";
  if (kind === "awaiting") return "border-amber-300 text-amber-700 dark:border-amber-900 dark:text-amber-300";
  if (kind === "pending") return "border-blue-300 text-blue-700 dark:border-blue-900 dark:text-blue-300";
  return "border-border text-muted-foreground";
}

function StatusPill({ children, kind }: { children: string; kind: "running" | "awaiting" | "idle" | "pending" }) {
  return (
    <span className={`rounded border px-2 py-0.5 text-xs ${statusPillClass(kind)}`}>
      {children}
    </span>
  );
}

function detail(label: string, value: string | number | null | undefined) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="truncate text-xs text-foreground" title={value === null || value === undefined ? undefined : String(value)}>
        {value === null || value === undefined || value === "" ? "—" : value}
      </div>
    </div>
  );
}

export function LiveSessionsPane({ sessions, onInterrupt, onSteer, onSubmit }: LiveSessionsPaneProps) {
  return (
    <Card>
      <CardHeader className="py-3 px-4">
        <CardTitle className="text-sm flex items-center gap-2">
          <MessageSquare className="h-4 w-4" />
          Live Sessions
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4 pb-4">
        {sessions === null ? (
          <p className="text-sm text-muted-foreground text-center py-6">
            Loading…
          </p>
        ) : sessions.length === 0 ? (
          <p className="text-sm text-muted-foreground text-center py-6">
            No active sessions.
          </p>
        ) : (
          <ul className="divide-y divide-border text-sm">
            {sessions.map((s) => {
              const pendingKinds = s.pending_request_kinds || [];
              const sessionStatus = s.awaiting_input ? "awaiting input" : s.running ? "running" : "idle";
              return (
                <li key={s.session_id} className="py-3 flex flex-col gap-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-foreground font-medium">
                        {s.title || shortId(s.session_id)}
                      </div>
                      <div className="mt-1 flex flex-wrap gap-2">
                        <StatusPill kind={s.awaiting_input ? "awaiting" : s.running ? "running" : "idle"}>
                          {sessionStatus}
                        </StatusPill>
                        {pendingKinds.map((kind) => (
                          <StatusPill key={kind} kind="pending">
                            {`pending ${kind}`}
                          </StatusPill>
                        ))}
                      </div>
                    </div>
                    <div className="shrink-0 text-right text-xs text-muted-foreground">
                      <div>last seen {ago(s.last_seen_at)}</div>
                      <div>running {durationSince(s.started_at)}</div>
                    </div>
                  </div>

                  <div className="grid gap-3 rounded border bg-muted/20 p-3 sm:grid-cols-2 lg:grid-cols-4">
                    {detail("started", formatTimestamp(s.started_at))}
                    {detail("elapsed", durationSince(s.started_at))}
                    {detail("last seen", ago(s.last_seen_at))}
                    {detail("owner", s.owner_kind)}
                    {detail("profile", s.profile)}
                    {detail("source", s.source)}
                    {detail("model", s.model)}
                    {detail("session id", shortId(s.session_id))}
                  </div>

                  {s.last_preview ? (
                    <div className="rounded bg-background px-3 py-2 text-xs text-muted-foreground border">
                      <span className="font-medium text-foreground">Last preview:</span> {s.last_preview}
                    </div>
                  ) : null}

                  {onInterrupt || onSteer || onSubmit ? (
                    <div className="flex flex-wrap gap-2">
                      {onInterrupt ? (
                        <button className="rounded border px-2 py-1 text-xs hover:bg-accent" onClick={() => onInterrupt(s)}>
                          Interrupt
                        </button>
                      ) : null}
                      {onSteer ? (
                        <button className="rounded border px-2 py-1 text-xs hover:bg-accent" onClick={() => onSteer(s)}>
                          Steer…
                        </button>
                      ) : null}
                      {onSubmit ? (
                        <button className="rounded border px-2 py-1 text-xs hover:bg-accent" onClick={() => onSubmit(s)}>
                          Submit…
                        </button>
                      ) : null}
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
