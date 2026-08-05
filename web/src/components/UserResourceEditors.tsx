import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "../auth/auth-context";

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
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
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

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Request failed";
}
