import { Cpu, Server } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ControlCenterProcess, ControlCenterProcessActionResponse, ControlCenterSystemProcess } from "@/lib/api";

export interface ProcessesPaneProps {
  processes: ControlCenterProcess[] | null;
  systemProcesses?: ControlCenterSystemProcess[] | null;
  selectedProcessId?: string | null;
  processDetail?: ControlCenterProcessActionResponse["result"] | null;
  processDetailLoading?: boolean;
  onSelect?: (process: ControlCenterProcess) => void;
  onPoll?: (process: ControlCenterProcess) => void;
  onReadLog?: (process: ControlCenterProcess) => void;
  onWait?: (process: ControlCenterProcess) => void;
  onKill?: (process: ControlCenterProcess) => void;
}

function formatAge(seconds?: number | null): string {
  if (seconds === null || seconds === undefined) return "unknown";
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours}h ${mins % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

function statusClass(exited: boolean): string {
  return !exited ? "text-green-600 dark:text-green-400" : "text-muted-foreground";
}

function shortId(id: string): string {
  return id.length > 18 ? `${id.slice(0, 18)}…` : id;
}

function renderOutput(detail: ControlCenterProcessActionResponse["result"]): string {
  const output = typeof detail.output === "string" ? detail.output : "";
  const preview = typeof detail.output_preview === "string" ? detail.output_preview : "";
  return output || preview;
}

export function ProcessesPane({
  processes,
  systemProcesses = null,
  selectedProcessId = null,
  processDetail = null,
  processDetailLoading = false,
  onSelect,
  onPoll,
  onReadLog,
  onWait,
  onKill,
}: ProcessesPaneProps) {
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="py-3 px-4">
          <CardTitle className="text-sm flex items-center gap-2">
            <Cpu className="h-4 w-4" />
            Managed Background Processes
          </CardTitle>
        </CardHeader>
        <CardContent className="px-4 pb-4">
          {processes === null ? (
            <p className="text-sm text-muted-foreground text-center py-6">Loading…</p>
          ) : processes.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-6">No managed background processes.</p>
          ) : (
            <ul className="divide-y divide-border text-sm">
              {processes.map((p, i) => {
                const selected = selectedProcessId === p.session_id;
                const controllable = p.controllable !== false;
                return (
                  <li key={p.session_id || p.pid || i} className="py-3 flex flex-col gap-2">
                    <div className="flex items-start justify-between gap-3">
                      <button
                        type="button"
                        className="min-w-0 flex-1 text-left"
                        onClick={() => onSelect?.(p)}
                      >
                        <div className="truncate text-foreground font-mono text-xs">
                          {p.command || "(unknown command)"}
                        </div>
                        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                          <span>session {shortId(p.session_id || "unknown")}</span>
                          <span>pid {p.pid ?? "—"}</span>
                          <span>age {formatAge(p.uptime_seconds)}</span>
                          {p.cwd ? <span className="truncate max-w-72">cwd {p.cwd}</span> : null}
                          {p.session_key ? <span>key {shortId(p.session_key)}</span> : null}
                        </div>
                      </button>
                      <span className={`shrink-0 text-xs font-medium ${statusClass(p.exited)}`}>
                        {p.status || (p.exited ? "exited" : "running")}
                      </span>
                    </div>
                    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                      {p.notify_on_complete ? <span className="rounded border px-2 py-0.5">notify on complete</span> : null}
                      {p.detached ? <span className="rounded border px-2 py-0.5">detached</span> : null}
                      {p.exit_code !== null && p.exit_code !== undefined ? (
                        <span className="rounded border px-2 py-0.5">exit {p.exit_code}</span>
                      ) : null}
                      {!controllable ? <span className="rounded border px-2 py-0.5">metadata only</span> : null}
                      {controllable ? (
                        <button className="rounded border px-2 py-1 hover:bg-accent" onClick={() => onPoll?.(p)}>
                          Poll
                        </button>
                      ) : null}
                      {controllable ? (
                        <button className="rounded border px-2 py-1 hover:bg-accent" onClick={() => onReadLog?.(p)}>
                          Log
                        </button>
                      ) : null}
                      {!p.exited && controllable ? (
                        <button className="rounded border px-2 py-1 hover:bg-accent" onClick={() => onWait?.(p)}>
                          Wait 3s
                        </button>
                      ) : null}
                      {!p.exited && controllable && onKill ? (
                        <button
                          className="rounded border border-red-300 px-2 py-1 text-red-600 hover:bg-red-50 dark:border-red-900 dark:text-red-400 dark:hover:bg-red-950/40"
                          onClick={() => onKill(p)}
                        >
                          Kill
                        </button>
                      ) : null}
                    </div>
                    {selected && processDetail ? (
                      <div className="rounded border bg-muted/20 p-3 text-xs">
                        <div className="mb-2 flex flex-wrap gap-x-3 gap-y-1 text-muted-foreground">
                          <span>detail {processDetailLoading ? "loading…" : processDetail.status || "loaded"}</span>
                          {processDetail.pid !== undefined ? <span>pid {String(processDetail.pid)}</span> : null}
                          {processDetail.uptime_seconds !== undefined ? <span>age {formatAge(processDetail.uptime_seconds as number)}</span> : null}
                          {processDetail.exit_code !== undefined ? <span>exit {String(processDetail.exit_code)}</span> : null}
                          {processDetail.total_lines !== undefined ? <span>{String(processDetail.total_lines)} log lines</span> : null}
                          {processDetail.showing ? <span>{String(processDetail.showing)}</span> : null}
                        </div>
                        {processDetail.timeout_note ? (
                          <div className="mb-2 text-muted-foreground">{String(processDetail.timeout_note)}</div>
                        ) : null}
                        {processDetail.note ? (
                          <div className="mb-2 text-muted-foreground">{String(processDetail.note)}</div>
                        ) : null}
                        {renderOutput(processDetail) ? (
                          <pre className="max-h-64 overflow-auto rounded bg-background p-2 text-xs text-foreground whitespace-pre-wrap">
                            {renderOutput(processDetail)}
                          </pre>
                        ) : (
                          <div className="text-muted-foreground">No output captured.</div>
                        )}
                      </div>
                    ) : p.output_preview ? (
                      <pre className="max-h-20 overflow-hidden rounded bg-muted/40 p-2 text-xs text-muted-foreground whitespace-pre-wrap">
                        {p.output_preview}
                      </pre>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="py-3 px-4">
          <CardTitle className="text-sm flex items-center gap-2">
            <Server className="h-4 w-4" />
            Hermes System Processes
          </CardTitle>
        </CardHeader>
        <CardContent className="px-4 pb-4">
          {systemProcesses === null ? (
            <p className="text-sm text-muted-foreground text-center py-6">Loading…</p>
          ) : systemProcesses.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-6">No Hermes-related OS processes found.</p>
          ) : (
            <ul className="divide-y divide-border text-sm">
              {systemProcesses.map((p) => (
                <li key={p.pid} className="py-3 flex flex-col gap-1">
                  <div className="flex items-center justify-between gap-2">
                    <div className="min-w-0 flex items-center gap-2">
                      <span className="rounded border px-2 py-0.5 text-xs uppercase text-muted-foreground">{p.kind}</span>
                      {p.managed ? <span className="rounded border px-2 py-0.5 text-xs text-green-600 dark:text-green-400">managed</span> : null}
                      <span className="truncate font-mono text-xs text-foreground">{p.command_preview || p.command}</span>
                    </div>
                    <span className="shrink-0 text-xs text-muted-foreground">pid {p.pid}</span>
                  </div>
                  <div className="text-xs text-muted-foreground">ppid {p.ppid} • elapsed {p.elapsed}</div>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
