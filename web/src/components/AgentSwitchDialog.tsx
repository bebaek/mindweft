import { useEffect, useRef, useState } from "react";
import type { ExecutionAgentOptionItem } from "../api/client";

function matchingAgents(agents: ExecutionAgentOptionItem[], query: string) {
  const exact = agents.filter((agent) => (agent.id ?? agent.name) === query);
  if (exact.length) return exact;
  return agents.filter((agent) => agent.name === query || agent.display_name === query);
}

interface Props {
  agents: ExecutionAgentOptionItem[];
  current: string;
  initialQuery: string;
  branching: boolean;
  busy: boolean;
  error: string | null;
  onConfirm: (agent: ExecutionAgentOptionItem) => void;
  onClose: () => void;
}

export function AgentSwitchDialog({ agents, current, initialQuery, branching, busy, error, onConfirm, onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [query, setQuery] = useState(initialQuery);
  const matches = matchingAgents(agents, query);
  const target = matches.length === 1 ? matches[0] : undefined;
  const candidates = agents.filter((agent) => `${agent.id ?? agent.name} ${agent.display_name ?? agent.name}`.toLowerCase().includes(query.toLowerCase()));

  useEffect(() => { dialog.current?.showModal(); }, []);

  return (
    <dialog ref={dialog} className="context-dialog" aria-labelledby="agent-switch-title"
      onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }}>
      <div className="context-dialog-header"><h2 id="agent-switch-title">Continue with another agent</h2></div>
      <div className="context-dialog-body">
        <p>Current agent: {current || "Unspecified (legacy thread)"}</p>
        <label>Agent name or reference
          <input autoFocus value={query} disabled={busy} onChange={(event) => setQuery(event.target.value)} />
        </label>
        <ul aria-label="Available agents">
          {candidates.map((agent) => <li key={agent.id ?? agent.name}>
            <button type="button" disabled={busy} onClick={() => setQuery(agent.id ?? agent.name)}>
              {agent.display_name ?? agent.name} ({agent.id ?? agent.name})
            </button>
          </li>)}
        </ul>
        {query && !target && <p role="status">{matches.length > 1 ? "Choose a scoped reference to disambiguate this name." : "Choose an available agent above, or enter its exact reference."}</p>}
        {target && <p>Continue with <strong>{target.display_name ?? target.name}</strong>?</p>}
        {branching ? <p>Conversation history will be copied to a new branch. The original thread will remain unchanged. The target agent's model and tool permissions will apply; continuing may send this history to a different provider.</p> : <p>Select an agent for your next message. No run will start automatically.</p>}
        {error && <p role="alert">{error}</p>}
        <div className="context-actions">
          <button type="button" disabled={busy} onClick={onClose}>Cancel</button>
          <button type="button" disabled={busy || !target} onClick={() => target && onConfirm(target)}>{busy ? "Switching…" : "Continue"}</button>
        </div>
      </div>
    </dialog>
  );
}
