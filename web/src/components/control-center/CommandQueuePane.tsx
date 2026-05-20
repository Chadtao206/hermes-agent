import { ActivitySquare } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ControlCenterCommand } from "@/lib/api";

export interface CommandQueuePaneProps {
  commands: ControlCenterCommand[] | null;
}

function ago(ts: number | null | undefined): string {
  if (!ts) return "—";
  const secs = Math.floor((Date.now() / 1000) - ts);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  return `${Math.floor(mins / 60)}h ago`;
}

export function CommandQueuePane({ commands }: CommandQueuePaneProps) {
  return (
    <Card>
      <CardHeader className="py-3 px-4">
        <CardTitle className="text-sm flex items-center gap-2">
          <ActivitySquare className="h-4 w-4" />
          Command Bus
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4 pb-4">
        {commands === null ? (
          <p className="text-sm text-muted-foreground text-center py-6">Loading…</p>
        ) : commands.length === 0 ? (
          <p className="text-sm text-muted-foreground text-center py-6">No recent commands.</p>
        ) : (
          <ul className="divide-y divide-border text-sm">
            {commands.map((command) => (
              <li key={command.id} className="py-3 flex flex-col gap-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium text-foreground truncate">{command.action}</span>
                  <span className="text-xs text-muted-foreground">{command.status}</span>
                </div>
                <div className="text-xs text-muted-foreground truncate">
                  {command.target_session_id || "global"} • queued {ago(command.created_at)}
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
