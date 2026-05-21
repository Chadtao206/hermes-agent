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

function statusClass(status: string): string {
  if (status === "completed") return "border-green-300 text-green-700 dark:border-green-900 dark:text-green-300";
  if (status === "failed") return "border-red-300 text-red-700 dark:border-red-900 dark:text-red-300";
  if (status === "expired") return "border-amber-300 text-amber-700 dark:border-amber-900 dark:text-amber-300";
  if (status === "claimed") return "border-blue-300 text-blue-700 dark:border-blue-900 dark:text-blue-300";
  return "border-border text-muted-foreground";
}

function compactJson(value: unknown): string | null {
  if (!value || typeof value !== "object") return null;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function resultSummary(value: Record<string, unknown> | null | undefined): string | null {
  if (!value) return null;
  const error = value.error;
  if (typeof error === "string" && error) return error;
  const status = value.status;
  if (typeof status === "string" && status) return `status: ${status}`;
  const output = value.output;
  if (typeof output === "string" && output) return output.slice(0, 240);
  return compactJson(value);
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
            {commands.map((command) => {
              const payload = compactJson(command.payload);
              const result = resultSummary(command.result);
              return (
                <li key={command.id} className="py-3 flex flex-col gap-1.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium text-foreground truncate">{command.action}</span>
                    <span className={`rounded border px-2 py-0.5 text-xs ${statusClass(command.status)}`}>
                      {command.status}
                    </span>
                  </div>
                  <div className="text-xs text-muted-foreground truncate">
                    target {command.target_session_id || "global"} • queued {ago(command.created_at)}
                    {command.claimed_at ? ` • claimed ${ago(command.claimed_at)}` : ""}
                    {command.completed_at ? ` • finished ${ago(command.completed_at)}` : ""}
                  </div>
                  {payload ? (
                    <div className="truncate rounded bg-muted/30 px-2 py-1 font-mono text-[11px] text-muted-foreground">
                      payload {payload}
                    </div>
                  ) : null}
                  {result ? (
                    <div className="rounded bg-background px-2 py-1 text-xs text-foreground border">
                      result {result}
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
