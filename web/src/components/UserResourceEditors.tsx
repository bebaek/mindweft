import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "../auth/auth-context";

const DEFAULT_AGENT_ID = "user:personal-assistant";
type Resource = Record<string, unknown>;


export function UserResourceEditors() {
  const { api, authentication } = useAuth();
  const queryClient = useQueryClient();
  const skillsKey = ["user-resources", "skills", authentication];
  const agentsKey = ["user-resources", "agents", authentication];
  const skills = useQuery({
    queryKey: skillsKey,
    queryFn: ({ signal }) => api.listUserResources("skills", signal),
    retry: false,
  });
  const agents = useQuery({
    queryKey: agentsKey,
    queryFn: ({ signal }) => api.listUserResources("agents", signal),
    retry: false,
  });
  const executionOptions = useQuery({
    queryKey: ["execution-options", authentication],
    queryFn: ({ signal }) => api.getExecutionOptions(signal),
    retry: false,
    staleTime: 60_000,
  });
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [editingVersion, setEditingVersion] = useState<number>(0);
  const [editingAgent, setEditingAgent] = useState<Resource | null>(null);
  const [agentName, setAgentName] = useState("");
  const [agentSkills, setAgentSkills] = useState("");
  const [agentProfile, setAgentProfile] = useState("");
  const [agentLlmProfile, setAgentLlmProfile] = useState("");
  const [agentFormError, setAgentFormError] = useState<string | null>(null);
  const createSkill = useMutation({
    mutationFn: () => {
      const slug = slugify(name);
      if (!slug) throw new Error("Enter a skill name");
      return api.updateUserResource("skills", `user:${slug}`, {
        id: `user:${slug}`,
        name: name.trim(),
        system_prompt: prompt.trim(),
      }, skills.data?.version ?? 0);
    },
    onSuccess: async () => {
      setName("");
      setPrompt("");
      setFormError(null);
      await queryClient.invalidateQueries({ queryKey: skillsKey });
      await queryClient.invalidateQueries({ queryKey: ["user-execution-config", authentication] });
    },
  });
  const removeSkill = useMutation({
    mutationFn: (skill: Resource) => api.deleteUserResource("skills", String(skill.id), skills.data?.version ?? undefined),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: skillsKey });
      await queryClient.invalidateQueries({ queryKey: ["user-execution-config", authentication] });
    },
  });
  const createAgent = useMutation({
    mutationFn: () => {
      const slug = slugify(agentName);
      if (!slug) throw new Error("Enter an agent name");
      const skillRefs = agentSkills.split(",").map((item) => item.trim()).filter(Boolean);
      if (skillRefs.some((item) => !item.includes(":"))) {
        throw new Error("Skill references must use user: or shared: prefixes");
      }
      const id = editingAgent ? String(editingAgent.id) : `user:${slug}`;
      if (!editingAgent && agents.data?.items.some((agent) => agent.id === id)) {
        throw new Error("An agent with this ID already exists. Use Edit to change it.");
      }
      if (agentLlmProfile && !executionOptions.data?.llm_profiles.items.some((profile) => profile.name === agentLlmProfile)) {
        throw new Error("Choose an available model profile or inherit the default.");
      }
      return api.updateUserResource("agents", id, {
        ...editingAgent,
        id,
        name: agentName.trim(),
        skill_refs: skillRefs,
        capability_profile_ref: agentProfile.trim() || null,
        llm_profile: agentLlmProfile || null,
      }, editingAgent ? editingVersion : agents.data?.version ?? 0);
    },
    onSuccess: async () => {
      setEditingAgent(null);
      setAgentName("");
      setAgentSkills("");
      setAgentProfile("");
      setAgentLlmProfile("");
      setAgentFormError(null);
      await queryClient.invalidateQueries({ queryKey: ["execution-options", authentication] });
      await queryClient.invalidateQueries({ queryKey: agentsKey });
      await queryClient.invalidateQueries({ queryKey: ["user-execution-config", authentication] });
    },
  });
  const removeAgent = useMutation({
    mutationFn: (agent: Resource) => api.deleteUserResource("agents", String(agent.id), agents.data?.version ?? undefined),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["execution-options", authentication] });
      await queryClient.invalidateQueries({ queryKey: agentsKey });
      await queryClient.invalidateQueries({ queryKey: ["user-execution-config", authentication] });
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    if (!name.trim() || !prompt.trim()) {
      setFormError("Name and instructions are required.");
      return;
    }
    try {
      createSkill.mutate();
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "Could not save skill");
    }
  }

  function startAgentFrom(agent: Resource, edit = false) {
    createAgent.reset();
    setEditingVersion(agents.data?.version ?? 0);
    setEditingAgent(edit ? agent : null);
    const agentNameValue = typeof agent.name === "string" ? agent.name : "Personal assistant";
    setAgentName(edit ? agentNameValue : `${agentNameValue} copy`);
    setAgentSkills(Array.isArray(agent.skill_refs) ? agent.skill_refs.filter((item): item is string => typeof item === "string").join(", ") : "");
    setAgentProfile(typeof agent.capability_profile_ref === "string" ? agent.capability_profile_ref : "");
    setAgentLlmProfile(typeof agent.llm_profile === "string" ? agent.llm_profile : "");
    setAgentFormError(null);
  }

  function submitAgent(event: FormEvent) {
    event.preventDefault();
    setAgentFormError(null);
    if (!agentName.trim()) {
      setAgentFormError("Agent name is required.");
      return;
    }
    try {
      createAgent.mutate();
    } catch (error) {
      setAgentFormError(error instanceof Error ? error.message : "Could not save agent");
    }
  }

  return (
    <section className="personalization-panel resource-editors" aria-labelledby="resource-editors-title">
      <div className="personalization-panel-heading">
        <div>
          <p className="eyebrow">Guided resources</p>
          <h2 id="resource-editors-title">Skills and agents</h2>
          <p>Create reusable personal instructions without editing the full execution JSON.</p>
        </div>
        <span>{skills.data?.items.length ?? 0} skills · {agents.data?.items.length ?? 0} agents</span>
      </div>
      <form className="resource-skill-form" onSubmit={submit}>
        <label>Skill name<input value={name} onChange={(event) => setName(event.target.value)} placeholder="Code reviewer" /></label>
        <label>Instructions<textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Review changes for correctness and tests." rows={3} /></label>
        {(formError || createSkill.error) && <p className="inline-error" role="alert">{formError ?? errorMessage(createSkill.error)}</p>}
        <button type="submit" className="button button-primary" disabled={createSkill.isPending}>{createSkill.isPending ? "Saving…" : "Add skill"}</button>
      </form>
      {skills.error && <p className="inline-error" role="alert">{errorMessage(skills.error)}</p>}
      <ul className="resource-list">
        {skills.data?.items.map((skill) => (
          <li key={String(skill.id)}>
            <div><strong>{String(skill.name ?? skill.id)}</strong><small>{resourceSummary(skill)}</small></div>
            <button type="button" className="button button-danger" disabled={removeSkill.isPending} onClick={() => removeSkill.mutate(skill)}>Remove</button>
          </li>
        ))}
        {!skills.isPending && skills.data?.items.length === 0 && <li className="personalization-empty">No personal skills yet.</li>}
      </ul>
      <form className="resource-skill-form resource-agent-form" onSubmit={submitAgent}>
        <h3>{editingAgent ? "Edit agent" : "New agent"}</h3>
        {executionOptions.data?.agents.items.filter((agent) => agent.name === "coding" && agent.source !== "user").map((agent) => (
          <button key={agent.id ?? agent.name} type="button" onClick={() => {
            const qualify = (ref: string) => ref.includes(":") ? ref : `shared:${ref}`;
            setEditingAgent(null);
            setAgentName("My coding");
            setAgentSkills((agent.skills ?? (agent.skill_name ? [agent.skill_name] : [])).map(qualify).join(", "));
            setAgentProfile(agent.capability_profile ? qualify(agent.capability_profile) : "");
            setAgentLlmProfile(agent.llm_profile ?? "");
            setAgentFormError(null);
          }}>Customize coding</button>
        ))}

        <label>Agent name<input value={agentName} onChange={(event) => setAgentName(event.target.value)} placeholder="Release assistant" /></label>
        <label>Skill references<input value={agentSkills} onChange={(event) => setAgentSkills(event.target.value)} placeholder="user:reviewer, shared:coding-workspace" /><small>Comma-separated qualified references.</small></label>
        <label>Capability profile reference<input value={agentProfile} onChange={(event) => setAgentProfile(event.target.value)} placeholder="user:personal-tools or shared:workspace" /></label>
        <label>Model profile<select aria-label="Agent model profile" value={agentLlmProfile} onChange={(event) => setAgentLlmProfile(event.target.value)} disabled={executionOptions.isPending}><option value="">Inherit default</option>{agentLlmProfile && !executionOptions.data?.llm_profiles.items.some((profile) => profile.name === agentLlmProfile) && <option value={agentLlmProfile}>{agentLlmProfile} (unavailable)</option>}{executionOptions.data?.llm_profiles.items.map((profile) => <option key={profile.name} value={profile.name}>{[profile.display_name ?? profile.name, profile.provider, profile.model].filter(Boolean).join(" · ")}</option>)}</select><small>Optional preference for new conversations. An explicit conversation override takes precedence; existing conversations are unchanged.</small></label>
        {(agentFormError || createAgent.error) && <p className="inline-error" role="alert">{agentFormError ?? errorMessage(createAgent.error)}</p>}
        <button type="submit" className="button button-primary" disabled={createAgent.isPending || agents.isPending || executionOptions.isPending || Boolean(agents.error || executionOptions.error)}>{createAgent.isPending ? "Saving…" : editingAgent ? "Save agent" : "Add agent"}</button>
      </form>
      {editingAgent && <button type="button" onClick={() => { setEditingAgent(null); setAgentName(""); setAgentSkills(""); setAgentProfile(""); setAgentLlmProfile(""); setAgentFormError(null); }}>Cancel editing</button>}
      {executionOptions.error && <p className="inline-error" role="alert">Could not load available model profiles: {errorMessage(executionOptions.error)}</p>}
      {agents.error && <p className="inline-error" role="alert">{errorMessage(agents.error)}</p>}
      <ul className="resource-list">
        {agents.data?.items.map((agent) => (
          <li key={String(agent.id)}>
            <div><strong>{String(agent.name ?? agent.id)} {agent.id === DEFAULT_AGENT_ID && <em className="resource-default-badge">Default</em>}</strong><small>{agentSummary(agent, executionOptions.data?.llm_profiles.items ?? [])}</small></div>
            <div className="resource-row-actions"><button type="button" className="button button-secondary" disabled={createAgent.isPending} onClick={() => startAgentFrom(agent, true)}>Edit</button><button type="button" className="button button-secondary" onClick={() => startAgentFrom(agent)}>Use as template</button><button type="button" className="button button-danger" disabled={removeAgent.isPending || agent.id === DEFAULT_AGENT_ID} onClick={() => removeAgent.mutate(agent)}>{agent.id === DEFAULT_AGENT_ID ? "Protected" : "Remove"}</button></div>
          </li>
        ))}
        {!agents.isPending && agents.data?.items.length === 0 && <li className="personalization-empty">No personal agents yet.</li>}
      </ul>
    </section>
  );
}

function slugify(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function resourceSummary(resource: Resource): string {
  const summary = resource.description ?? resource.system_prompt;
  return typeof summary === "string" && summary.length > 0 ? summary : "Personal skill";
}

function agentSummary(agent: Resource, llmProfiles: Array<{ name: string; display_name?: string | null }>): string {
  const refs = agent.skill_refs;
  const profile = agent.capability_profile_ref;
  const llmProfile = agent.llm_profile;
  const skillText = Array.isArray(refs) ? refs.filter((item): item is string => typeof item === "string").join(", ") : "No skills";
  const profileText = typeof profile === "string" ? profile : "";
  const llmText = typeof llmProfile === "string"
    ? `Model: ${llmProfiles.find((profile) => profile.name === llmProfile)?.display_name ?? llmProfile}`
    : "Model: inherit default";
  return [profileText ? `${skillText || "No skills"} · ${profileText}` : skillText, llmText].filter(Boolean).join(" · ");
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Request failed";
}
