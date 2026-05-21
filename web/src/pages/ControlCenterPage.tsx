import { useEffect, useRef, useState } from "react";
import { PluginSlot } from "@/plugins";
import { api } from "@/lib/api";
import type {
  ControlCenterOverviewResponse,
  ControlCenterLiveSession,
  ControlCenterPendingRequest,
  ControlCenterProcess,
  ControlCenterSystemProcess,
  ControlCenterDelegationSummary,
  ControlCenterProfileStatus,
  ControlCenterCommand,
  ControlCenterProcessActionResponse,
  ControlCenterRuntimeAction,
  ControlCenterRuntimeCard,
  ControlCenterRuntimeHealthResponse,
} from "@/lib/api";
import { OverviewCards } from "@/components/control-center/OverviewCards";
import { LiveSessionsPane } from "@/components/control-center/LiveSessionsPane";
import { PendingRequestsPane } from "@/components/control-center/PendingRequestsPane";
import { ProcessesPane } from "@/components/control-center/ProcessesPane";
import { DelegationPane } from "@/components/control-center/DelegationPane";
import { ProfileHealthPane } from "@/components/control-center/ProfileHealthPane";
import { CommandQueuePane } from "@/components/control-center/CommandQueuePane";
import { RuntimeHealthPane } from "@/components/control-center/RuntimeHealthPane";

const OVERVIEW_MS = 5_000;
const DETAIL_MS = 4_000;
const CONTROL_CENTER_ACTIONS_ENABLED = false;

export default function ControlCenterPage() {
  const [overview, setOverview] = useState<ControlCenterOverviewResponse | null>(null);
  const [sessions, setSessions] = useState<ControlCenterLiveSession[] | null>(null);
  const [pending, setPending] = useState<ControlCenterPendingRequest[] | null>(null);
  const [commands, setCommands] = useState<ControlCenterCommand[] | null>(null);
  const [processes, setProcesses] = useState<ControlCenterProcess[] | null>(null);
  const [systemProcesses, setSystemProcesses] = useState<ControlCenterSystemProcess[] | null>(null);
  const [subagents, setSubagents] = useState<ControlCenterDelegationSummary[] | null>(null);
  const [profiles, setProfiles] = useState<ControlCenterProfileStatus[] | null>(null);
  const [runtimes, setRuntimes] = useState<ControlCenterRuntimeHealthResponse | null>(null);
  const [runtimeActionResult, setRuntimeActionResult] = useState<string | null>(null);
  const [selectedProcessId, setSelectedProcessId] = useState<string | null>(null);
  const [processDetail, setProcessDetail] = useState<ControlCenterProcessActionResponse["result"] | null>(null);
  const [processDetailLoading, setProcessDetailLoading] = useState(false);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const refreshOverview = () => {
    api.getControlCenterOverview()
      .then((d) => { if (mountedRef.current) setOverview(d); })
      .catch(() => {});
  };

  const refreshSessions = () => {
    api.getControlCenterSessions()
      .then((d) => { if (mountedRef.current) setSessions(d.sessions); })
      .catch(() => {});
  };

  const refreshPending = () => {
    api.getControlCenterPending()
      .then((d) => { if (mountedRef.current) setPending(d.requests); })
      .catch(() => {});
  };

  const refreshCommands = () => {
    api.getControlCenterCommands()
      .then((d) => { if (mountedRef.current) setCommands(d.commands); })
      .catch(() => {});
  };

  const refreshProcesses = () => {
    api.getControlCenterProcesses()
      .then((d) => { if (mountedRef.current) setProcesses(d.processes); })
      .catch(() => {});
  };

  const refreshSystemProcesses = () => {
    api.getControlCenterSystemProcesses()
      .then((d) => { if (mountedRef.current) setSystemProcesses(d.processes); })
      .catch(() => {});
  };

  const refreshSubagents = () => {
    api.getControlCenterDelegation()
      .then((d) => { if (mountedRef.current) setSubagents(d.subagents); })
      .catch(() => {});
  };

  const refreshProfiles = () => {
    api.getControlCenterProfiles()
      .then((d) => { if (mountedRef.current) setProfiles(d.profiles); })
      .catch(() => {});
  };

  const refreshRuntimes = () => {
    api.getControlCenterRuntimes()
      .then((d) => { if (mountedRef.current) setRuntimes(d); })
      .catch(() => {});
  };

  const refreshControlState = () => {
    refreshOverview();
    refreshSessions();
    refreshPending();
    refreshCommands();
    refreshProcesses();
    refreshSystemProcesses();
    refreshRuntimes();
  };

  useEffect(() => {
    refreshOverview();
    const id = setInterval(refreshOverview, OVERVIEW_MS);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    refreshSessions();
    const id = setInterval(refreshSessions, DETAIL_MS);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    refreshPending();
    const id = setInterval(refreshPending, DETAIL_MS);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    refreshCommands();
    const id = setInterval(refreshCommands, DETAIL_MS);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    refreshProcesses();
    const id = setInterval(refreshProcesses, DETAIL_MS);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    refreshSystemProcesses();
    const id = setInterval(refreshSystemProcesses, DETAIL_MS);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    refreshSubagents();
    const id = setInterval(refreshSubagents, DETAIL_MS);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    refreshProfiles();
    const id = setInterval(refreshProfiles, DETAIL_MS);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    refreshRuntimes();
    const id = setInterval(refreshRuntimes, DETAIL_MS);
    return () => clearInterval(id);
  }, []);

  const reportError = (error: unknown) => {
    const message = error instanceof Error ? error.message : String(error);
    window.alert(message);
  };

  const handleInterrupt = async (session: ControlCenterLiveSession) => {
    try {
      await api.interruptControlCenterSession(session.session_id);
      refreshControlState();
    } catch (error) {
      reportError(error);
    }
  };

  const handleSteer = async (session: ControlCenterLiveSession) => {
    const text = window.prompt(`Steer ${session.title || session.session_id}`, "");
    if (!text || !text.trim()) return;
    try {
      await api.steerControlCenterSession(session.session_id, text.trim());
      refreshControlState();
    } catch (error) {
      reportError(error);
    }
  };

  const handleSubmit = async (session: ControlCenterLiveSession) => {
    const text = window.prompt(`Submit to ${session.title || session.session_id}`, "");
    if (!text || !text.trim()) return;
    try {
      await api.submitControlCenterSession(session.session_id, text.trim());
      refreshControlState();
    } catch (error) {
      reportError(error);
    }
  };

  const updateProcessDetail = async (
    proc: ControlCenterProcess,
    loader: () => Promise<ControlCenterProcessActionResponse>,
  ) => {
    setSelectedProcessId(proc.session_id);
    setProcessDetailLoading(true);
    try {
      const response = await loader();
      setProcessDetail(response.result);
      refreshControlState();
    } catch (error) {
      reportError(error);
    } finally {
      if (mountedRef.current) setProcessDetailLoading(false);
    }
  };

  const handleProcessSelect = (proc: ControlCenterProcess) => {
    setSelectedProcessId(proc.session_id);
    setProcessDetail({
      session_id: proc.session_id,
      command: proc.command,
      status: proc.status || (proc.exited ? "exited" : "running"),
      pid: proc.pid,
      uptime_seconds: proc.uptime_seconds || undefined,
      output_preview: proc.output_preview || undefined,
      exit_code: proc.exit_code,
    });
  };

  const handleProcessPoll = async (proc: ControlCenterProcess) => {
    await updateProcessDetail(proc, () => api.pollControlCenterProcess(proc.session_id));
  };

  const handleProcessReadLog = async (proc: ControlCenterProcess) => {
    await updateProcessDetail(proc, () => api.getControlCenterProcessLog(proc.session_id, 200));
  };

  const handleProcessWait = async (proc: ControlCenterProcess) => {
    await updateProcessDetail(proc, () => api.waitControlCenterProcess(proc.session_id, 3));
  };

  const handleProcessKill = async (proc: ControlCenterProcess) => {
    const label = proc.command || proc.session_id;
    if (!window.confirm(`Kill managed process ${label}?`)) return;
    try {
      const response = await api.killControlCenterProcess(proc.session_id);
      setSelectedProcessId(proc.session_id);
      setProcessDetail(response.result);
      refreshControlState();
    } catch (error) {
      reportError(error);
    }
  };

  const handleRuntimeAction = async (runtime: ControlCenterRuntimeCard, action: ControlCenterRuntimeAction) => {
    if (!action.available) return;
    if (action.destructive && !window.confirm(`${action.label} ${runtime.name}?`)) return;
    try {
      const response = await api.runControlCenterRuntimeAction(runtime.id, action.id);
      const result = response.result;
      const output = typeof result.output === "string" ? result.output : "";
      setRuntimeActionResult(`${runtime.name} / ${action.label}: ${result.status}${output ? `
${output}` : ""}`);
      refreshControlState();
    } catch (error) {
      reportError(error);
    }
  };

  const handlePendingRespond = async (request: ControlCenterPendingRequest) => {
    try {
      if (request.kind === "approval") {
        const approve = window.confirm(request.prompt_preview || "Approve this request?");
        await api.respondToControlCenterPending(request.request_id, { choice: approve ? "once" : "deny" });
      } else if (request.choices && request.choices.length > 0) {
        const text = window.prompt(
          `${request.prompt_preview}\nChoices: ${request.choices.join(", ")}`,
          request.choices[0] || "",
        );
        if (!text || !text.trim()) return;
        await api.respondToControlCenterPending(request.request_id, { text: text.trim() });
      } else {
        const text = window.prompt(request.prompt_preview || `Respond to ${request.kind}`, "");
        if (!text || !text.trim()) return;
        await api.respondToControlCenterPending(request.request_id, { text: text.trim() });
      }
      refreshControlState();
    } catch (error) {
      reportError(error);
    }
  };

  return (
    <div className="flex flex-col gap-6">
      <PluginSlot name="control-center:top" />

      <OverviewCards data={overview} />

      <div className="flex min-w-0 gap-6">
        <div className="flex min-w-0 flex-1 flex-col gap-6">
          <LiveSessionsPane
            sessions={sessions}
            onInterrupt={CONTROL_CENTER_ACTIONS_ENABLED ? handleInterrupt : undefined}
            onSteer={CONTROL_CENTER_ACTIONS_ENABLED ? handleSteer : undefined}
            onSubmit={CONTROL_CENTER_ACTIONS_ENABLED ? handleSubmit : undefined}
          />
          <PendingRequestsPane
            requests={pending}
            onRespond={CONTROL_CENTER_ACTIONS_ENABLED ? handlePendingRespond : undefined}
          />
          <RuntimeHealthPane
            data={runtimes}
            actionResult={CONTROL_CENTER_ACTIONS_ENABLED ? runtimeActionResult : null}
            onAction={CONTROL_CENTER_ACTIONS_ENABLED ? handleRuntimeAction : undefined}
          />
          <ProcessesPane
            processes={processes}
            systemProcesses={systemProcesses}
            selectedProcessId={selectedProcessId}
            processDetail={processDetail}
            processDetailLoading={processDetailLoading}
            onSelect={handleProcessSelect}
            onPoll={CONTROL_CENTER_ACTIONS_ENABLED ? handleProcessPoll : undefined}
            onReadLog={CONTROL_CENTER_ACTIONS_ENABLED ? handleProcessReadLog : undefined}
            onWait={CONTROL_CENTER_ACTIONS_ENABLED ? handleProcessWait : undefined}
            onKill={CONTROL_CENTER_ACTIONS_ENABLED ? handleProcessKill : undefined}
          />
          <DelegationPane subagents={subagents} />
          <ProfileHealthPane profiles={profiles} />
        </div>

        <aside className="w-72 shrink-0 flex flex-col gap-4">
          <CommandQueuePane commands={commands} />
          <PluginSlot name="control-center:right-rail" />
        </aside>
      </div>

      <PluginSlot name="control-center:bottom" />
    </div>
  );
}
