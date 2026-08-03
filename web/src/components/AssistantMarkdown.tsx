import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

export function AssistantMarkdown({ children }: { children: string }) {
  return (
    <div className="message-content markdown-content">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        urlTransform={safeUrlTransform}
        components={{
          a: ({ href, children: linkChildren, title }) => {
            if (!href) return <span>{linkChildren}</span>;
            const external = /^https?:\/\//i.test(href);
            return <a href={href} title={title} target={external ? "_blank" : undefined} rel={external ? "noopener noreferrer" : undefined}>{linkChildren}</a>;
          },
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}

function safeUrlTransform(url: string): string {
  return defaultUrlTransform(url);
}
