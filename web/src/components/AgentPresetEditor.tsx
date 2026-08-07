import { useMemo, useState } from "react";

type JsonObject = Record<string, unknown>;
type AgentPreset = JsonObject & { name: string };

export function AgentPresetEditor({
  agents,
  capabilities,
  llmProfiles,
  onChange,
}: {
  agents: string;
  capabilities: string;
  llmProfiles: string;
  onChange: (value: string) => void;
}) {
  const parsed = useMemo(() => parseState(agents, capabilities, llmProfiles), [agents, capabilities, llmProfiles]);
  const [editing, setEditing] = useState<number | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [draft, setDraft] = useState<AgentDraft>(emptyDraft());
  const [error, setError] = useState<string | null>(null);

  function startNew() {
    setEditing(null);
    setDraft(emptyDraft());
    setError(null);
    setFormOpen(true);
  }

  function startEdit(index: number, agent: AgentPreset) {
    setEditing(index);
    setDraft(toDraft(agent));
    setError(null);
    setFormOpen(true);
  }

  function saveDraft() {
    const name = draft.name.trim();
    if (!name) {
      setError("Agent preset name is required.");
      return;
    }
    const next: JsonObject = {
      name,
      ...(draft.description.trim() ? { description: draft.description.trim() } : {}),
      skill_refs: refs(draft.skillRefs),
      ...(draft.capabilityProfile ? { capability_profile_ref: draft.capabilityProfile } : {}),
      ...(draft.llmProfile ? { llm_profile: draft.llmProfile } : {}),
    };
    const items = parsed.agents.map((item) => ({ ...item }));
    if (editing === null) items.push(next as AgentPreset);
    else items[editing] = next as AgentPreset;
    onChange(JSON.stringify({ ...parsed.agentConfig, items }, null, 2));
    startNew();
  }

  function remove(index: number) {
    const items = parsed.agents.filter((_, itemIndex) => itemIndex !== index);
    onChange(JSON.stringify({ ...parsed.agentConfig, items }, null, 2));
    if (editing === index) {
      setEditing(null);
      setFormOpen(false);
    }
  }

  if (parsed.error) return <p className="execution-config-empty" role="alert">{parsed.error}</p>;

  return (
    <section className="agent-preset-editor" aria-labelledby="agent-preset-editor-title">
      <div className="guided-editor-heading">
        <div><strong id="agent-preset-editor-title">Agent presets</strong><span>Give each preset a clear role and choose its tools, skills, and model.</span></div>
        <button type="button" onClick={startNew}>New preset</button>
      </div>
      <div className="guided-editor-list">
        {parsed.agents.map((agent, index) => (
          <article key={`${agent.name}-${index}`} className="guided-editor-item">
            <div><strong>{agent.name}</strong><small>{summary(agent, parsed.capabilityNames, parsed.llmNames)}</small></div>
            <div className="guided-editor-actions"><button type="button" onClick={() => startEdit(index, agent)}>Edit</button><button type="button" className="button-danger" onClick={() => remove(index)}>Remove</button></div>
          </article>
        ))}
        {parsed.agents.length === 0 && <p className="personalization-empty">No agent presets configured yet.</p>}
      </div>
      {formOpen && (
        <div className="guided-editor-form">
          <div className="execution-field-grid">
            <label>Name<input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} placeholder="Code reviewer" /></label>
            <label className="wide">Description<input value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} placeholder="Reviews changes and reports risks" /></label>
            <label className="wide">Skill references<input value={draft.skillRefs} onChange={(event) => setDraft({ ...draft, skillRefs: event.target.value })} placeholder="shared:coding, user:reviewer" /><small>Comma-separated qualified references.</small></label>
            <label>Capability profile<select value={draft.capabilityProfile} onChange={(event) => setDraft({ ...draft, capabilityProfile: event.target.value })}><option value="">Use default</option>{parsed.capabilityNames.map((name) => <option key={name} value={name}>{name}</option>)}</select></label>
            <label>Model profile<select value={draft.llmProfile} onChange={(event) => setDraft({ ...draft, llmProfile: event.target.value })}><option value="">Use default</option>{parsed.llmNames.map((name) => <option key={name} value={name}>{name}</option>)}</select></label>
          </div>
          {error && <p className="dialog-error" role="alert">{error}</p>}
          <div className="guided-editor-actions"><button type="button" className="button-secondary" onClick={() => { setFormOpen(false); setEditing(null); }}>Cancel</button><button type="button" className="button-primary" onClick={saveDraft}>{editing === null ? "Add preset" : "Save preset"}</button></div>
        </div>
      )}
    </section>
  );
}

interface AgentDraft { name: string; description: string; skillRefs: string; capabilityProfile: string; llmProfile: string }
function emptyDraft(): AgentDraft { return { name: "", description: "", skillRefs: "", capabilityProfile: "", llmProfile: "" }; }
function toDraft(agent: AgentPreset): AgentDraft {
  return {
    name: agent.name,
    description: typeof agent.description === "string" ? agent.description : "",
    skillRefs: Array.isArray(agent.skill_refs) ? agent.skill_refs.filter((item): item is string => typeof item === "string").join(", ") : "",
    capabilityProfile: typeof agent.capability_profile_ref === "string" ? agent.capability_profile_ref : "",
    llmProfile: typeof agent.llm_profile === "string" ? agent.llm_profile : "",
  };
}
function refs(value: string): string[] { return [...new Set(value.split(",").map((item) => item.trim()).filter(Boolean))]; }
interface ParsedState {
  error: string | null;
  agentConfig: JsonObject;
  agents: AgentPreset[];
  capabilityNames: string[];
  llmNames: string[];
}

function parseState(agents: string, capabilities: string, llmProfiles: string): ParsedState {
  try {
    const agentConfig = JSON.parse(agents) as unknown;
    const capabilityConfig = JSON.parse(capabilities) as unknown;
    const llmConfig = JSON.parse(llmProfiles) as unknown;
    if (!isObject(agentConfig) || !Array.isArray(agentConfig.items)) return emptyParsed("Agent presets JSON must contain an items array.");
    const parsedAgents = agentConfig.items.filter(isAgent);
    const capabilityNames = isObject(capabilityConfig) && Array.isArray(capabilityConfig.items) ? capabilityConfig.items.filter(isNamed).map((item) => item.name) : [];
    const llmNames = isObject(llmConfig) ? Object.keys(llmConfig) : [];
    return { error: null, agentConfig, agents: parsedAgents, capabilityNames, llmNames };
  } catch { return emptyParsed("Fix the agent preset and profile JSON before using the guided editor."); }
}

function emptyParsed(error: string): ParsedState {
  return { error, agentConfig: {}, agents: [], capabilityNames: [], llmNames: [] };
}
function summary(agent: AgentPreset, capabilityNames: string[], llmNames: string[]): string {
  const skills = Array.isArray(agent.skill_refs) ? agent.skill_refs.filter((item): item is string => typeof item === "string").join(", ") : "No skills";
  const capability = typeof agent.capability_profile_ref === "string" && capabilityNames.includes(agent.capability_profile_ref) ? agent.capability_profile_ref : "Default tools";
  const model = typeof agent.llm_profile === "string" && llmNames.includes(agent.llm_profile) ? agent.llm_profile : "Default model";
  return `${skills || "No skills"} · ${capability} · ${model}`;
}
function isObject(value: unknown): value is JsonObject { return Boolean(value) && typeof value === "object" && !Array.isArray(value); }
function isNamed(value: unknown): value is { name: string } { return isObject(value) && typeof value.name === "string"; }
function isAgent(value: unknown): value is AgentPreset { return isNamed(value); }
