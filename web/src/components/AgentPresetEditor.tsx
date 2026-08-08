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
    const existing = editing === null ? null : parsed.agents[editing];
    const next = presetFromDraft(draft, existing);
    const items = parsed.agents.map((item) => ({ ...item }));
    if (editing === null) items.push(next);
    else items[editing] = next;
    const agentConfig = syncDefaultAgent(parsed.agentConfig, existing?.name, next.name);
    onChange(JSON.stringify({ ...agentConfig, items }, null, 2));
    startNew();
  }

  function remove(index: number) {
    const removed = parsed.agents[index];
    const items = parsed.agents.filter((_, itemIndex) => itemIndex !== index);
    const agentConfig = syncDefaultAgent(parsed.agentConfig, removed?.name, null);
    onChange(JSON.stringify({ ...agentConfig, items }, null, 2));
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
            <label className="wide">Skills<input value={draft.skillNames} onChange={(event) => setDraft({ ...draft, skillNames: event.target.value })} placeholder="coding, reviewer" /><small>Comma-separated configured skill names.</small></label>
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

interface AgentDraft { name: string; description: string; skillNames: string; capabilityProfile: string; llmProfile: string }
function emptyDraft(): AgentDraft { return { name: "", description: "", skillNames: "", capabilityProfile: "", llmProfile: "" }; }
function toDraft(agent: AgentPreset): AgentDraft {
  const configuredSkills = agent.skill_names ?? agent.skillNames ?? agent.skills;
  const singularSkill = agent.skill_name ?? agent.skillName;
  return {
    name: agent.name,
    description: typeof agent.description === "string" ? agent.description : "",
    skillNames: Array.isArray(configuredSkills)
      ? configuredSkills.filter((item): item is string => typeof item === "string").join(", ")
      : typeof singularSkill === "string" ? singularSkill : "",
    capabilityProfile: stringProperty(agent, "capability_profile", "capabilityProfile"),
    llmProfile: stringProperty(agent, "llm_profile", "llmProfile"),
  };
}
function presetFromDraft(draft: AgentDraft, existing: AgentPreset | null): AgentPreset {
  const preset: JsonObject = existing ? { ...existing } : {};
  removeKeys(preset, [
    "name", "description",
    "skill_name", "skillName", "skill_names", "skillNames", "skills",
    "capability_profile", "capabilityProfile",
    "llm_profile", "llmProfile",
  ]);
  preset.name = draft.name.trim();
  if (draft.description.trim()) preset.description = draft.description.trim();
  preset.skill_names = skills(draft.skillNames);
  if (draft.capabilityProfile) preset.capability_profile = draft.capabilityProfile;
  if (draft.llmProfile) preset.llm_profile = draft.llmProfile;
  return preset as AgentPreset;
}
function syncDefaultAgent(config: JsonObject, previousName: string | undefined, nextName: string | null): JsonObject {
  const defaultAgent = config.default_agent ?? config.defaultAgent;
  if (typeof defaultAgent !== "string" || defaultAgent !== previousName) return config;
  const updated = { ...config };
  removeKeys(updated, ["default_agent", "defaultAgent"]);
  if (nextName) updated.default_agent = nextName;
  return updated;
}
function removeKeys(object: JsonObject, keys: string[]) { for (const key of keys) delete object[key]; }
function skills(value: string): string[] { return [...new Set(value.split(",").map((item) => item.trim()).filter(Boolean))]; }
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
    if (!agentConfig.items.every(isAgent)) return emptyParsed("Agent presets JSON must contain only named preset objects.");
    const parsedAgents = agentConfig.items;
    const capabilityNames = isObject(capabilityConfig) && Array.isArray(capabilityConfig.items) ? capabilityConfig.items.filter(isNamed).map((item) => item.name) : [];
    const llmNames = isObject(llmConfig) ? Object.keys(llmConfig) : [];
    return { error: null, agentConfig, agents: parsedAgents, capabilityNames, llmNames };
  } catch { return emptyParsed("Fix the agent preset and profile JSON before using the guided editor."); }
}

function emptyParsed(error: string): ParsedState {
  return { error, agentConfig: {}, agents: [], capabilityNames: [], llmNames: [] };
}
function summary(agent: AgentPreset, capabilityNames: string[], llmNames: string[]): string {
  const configuredSkills = agent.skill_names ?? agent.skillNames ?? agent.skills;
  const singularSkill = agent.skill_name ?? agent.skillName;
  const skillList = Array.isArray(configuredSkills)
    ? configuredSkills.filter((item): item is string => typeof item === "string")
    : typeof singularSkill === "string" ? [singularSkill] : [];
  const capabilityName = stringProperty(agent, "capability_profile", "capabilityProfile");
  const llmName = stringProperty(agent, "llm_profile", "llmProfile");
  const capability = capabilityNames.includes(capabilityName) ? capabilityName : "Default tools";
  const model = llmNames.includes(llmName) ? llmName : "Default model";
  return `${skillList.join(", ") || "No skills"} · ${capability} · ${model}`;
}
function stringProperty(value: JsonObject, snakeCase: string, camelCase: string): string {
  const property = value[snakeCase] ?? value[camelCase];
  return typeof property === "string" ? property : "";
}
function isObject(value: unknown): value is JsonObject { return Boolean(value) && typeof value === "object" && !Array.isArray(value); }
function isNamed(value: unknown): value is { name: string } { return isObject(value) && typeof value.name === "string"; }
function isAgent(value: unknown): value is AgentPreset { return isNamed(value); }
