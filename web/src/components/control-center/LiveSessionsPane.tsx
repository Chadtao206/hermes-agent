import { MessageSquare } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ControlCenterLiveSession } from "@/lib/api";

export interface LiveSessionsPaneProps {
  sessions: ControlCenterLiveSession[] | null;
  onInterrupt?: (session: ControlCenterLiveSession) => void;
  onSteer?: (session: ControlCenterLiveSession) => void;
  onSubmit?: (session: ControlCenterLiveSession) => void;
}

function ago(ts: number): string {
  const secs = Math.floor((Date.now() / 1000) - ts);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  return `${Math.floor(mins / 60)}h ago`;
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
            {sessions.map((s) => (
              <li key={s.session_id} className="py-3 flex flex-col gap-2">
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate text-foreground font-medium">
                      {s.title || s.session_id.slice(0, 12)}
                    </div>
                    <div className="text-xs text-muted-foreground truncate">
                      {s.owner_kind} • {s.profile || s.model || s.source || s.session_id}
                    </div>
                  </div>
                  <span className="shrink-0 text-muted-foreground text-xs">
                    {ago(s.last_seen_at)}
                  </span>
                </div>
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
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
