import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AssistantMarkdown } from "./AssistantMarkdown";

afterEach(cleanup);

describe("AssistantMarkdown", () => {
  it("renders GFM structure and code", () => {
    render(<AssistantMarkdown>{`## Result

- [x] Done

| Name | Value |
| --- | --- |
| status | ok |

\`inline\`

\`\`\`ts
const ready = true;
\`\`\``}</AssistantMarkdown>);

    expect(screen.getByRole("heading", { name: "Result" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox")).toBeChecked();
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText("const ready = true;")).toBeInTheDocument();
  });

  it("does not render raw HTML or unsafe link protocols", () => {
    const { container } = render(<AssistantMarkdown>{`<script>alert("xss")</script>

[unsafe](javascript:alert('xss'))

[documentation](https://example.com/docs)`}</AssistantMarkdown>);

    expect(container.querySelector("script")).not.toBeInTheDocument();
    expect(screen.getByText("unsafe").closest("a")).toBeNull();
    expect(screen.getByRole("link", { name: "documentation" })).toHaveAttribute("href", "https://example.com/docs");
    expect(screen.getByRole("link", { name: "documentation" })).toHaveAttribute("target", "_blank");
    expect(screen.getByRole("link", { name: "documentation" })).toHaveAttribute("rel", "noopener noreferrer");
  });
});
