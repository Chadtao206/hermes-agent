import { Activity, AlertTriangle } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ControlCenterRuntimeAction, ControlCenterRuntimeCard, ControlCenterRuntimeHealthResponse } from "@/lib/api";

export interface RuntimeHealthPaneProps {
  data: ControlCenterRuntimeHealthResponse | null;
  actionResult?: string | null;
  onAction?: (runtime: ControlCenterRuntimeCard, action: ControlCenterRuntimeAction) => void;
}

function formatChecked(ts?: number | null): string {
  if (!ts) return "unknown";
  return new Date(ts * 1000).toLocaleTimeString();
}

function detailValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") {
    const keys = Object.keys(value as Record<string, unknown>);
    if (keys.length === 0) return "—";
    return keys.slice(0, 4).join(", ") + (keys.length > 4 ? "…" : "");
  }
  return String(value);
}

function statusClass(runtime: ControlCenterRuntimeCard): string {
  if (runtime.warnings?.length) return "text-warning";
  if (runtime.running || runtime.status === "active") return "text-success";
  return "text-muted-foreground";
}

export function RuntimeHealthPane({ data, actionResult = null, onAction }: RuntimeHealthPaneProps) {
  return (
    <Card>
      <CardHeader className="py-3 px-4">
        <CardTitle className="text-sm flex items-center gap-2">
          <Activity className="h-4 w-4" />
          Runtime Health & Controls
          {data ? <span className="ml-auto text-xs font-normal text-muted-foreground">checked {formatChecked(data.last_checked)}</span> : null}
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4 pb-4">
        {data === null ? (
          <p className="text-sm text-muted-foreground text-center py-6">Loading…</p>
        ) : data.runtimes.length === 0 ? (
          <p className="text-sm text-muted-foreground text-center py-6">No runtime health data available.</p>
        ) : (
          <div className="grid gap-3 md:grid-cols-3">
            {data.runtimes.map((runtime) => (
              <div key={runtime.id} className="rounded border p-3 text-sm">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <div className="font-medium text-foreground">{runtime.name}</div>
                    <div className="mt-1 text-xs text-muted-foreground">{runtime.source}</div>
                  </div>
                  <span className={`text-xs font-medium ${statusClass(runtime)}`}>{runtime.status}</span>
                </div>
                <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-xs text-muted-foreground">
                  <span>state</span><span className="truncate text-foreground">{runtime.state || "—"}</span>
                  <span>primary pid</span><span className="text-foreground">{runtime.primary_pid ?? "—"}</span>
                  <span>pids</span><span className="truncate text-foreground">{runtime.pids?.length ? runtime.pids.join(", ") : "—"}</span>
                  {Object.entries(runtime.details || {}).slice(0, 4).map(([key, value]) => (
                    <span key={key} className="contents">
                      <span>{key.replace(/_/g, " ")}</span><span className="truncate text-foreground">{detailValue(value)}</span>
                    </span>
                  ))}
                </div>
                {runtime.warnings?.length ? (
                  <div className="mt-3 flex flex-col gap-1 text-xs text-warning">
                    {runtime.warnings.map((warning, idx) => (
                      <div key={idx} className="flex gap-1"><AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" /> <span>{warning}</span></div>
                    ))}
                  </div>
                ) : null}
                {onAction && runtime.actions?.length ? (
                  <div className="mt-3 flex flex-wrap gap-2">
                    {runtime.actions.map((action) => (
                      <button
                        key={action.id}
                        title={action.reason || undefined}
                        disabled={!action.available}
                        className={`rounded border px-2 py-1 text-xs ${action.available ? "hover:bg-accent" : "cursor-not-allowed opacity-50"} ${action.destructive && action.available ? "border-destructive/30 text-destructive hover:bg-destructive/10" : ""}`}
                        onClick={() => onAction?.(runtime, action)}
                      >
                        {action.label}
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        )}
        {actionResult ? (
          <pre className="mt-3 max-h-40 overflow-auto rounded bg-muted/40 p-2 text-xs whitespace-pre-wrap">{actionResult}</pre>
        ) : null}
      </CardContent>
    </Card>
  );
}
