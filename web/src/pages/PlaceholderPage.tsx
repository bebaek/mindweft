interface PlaceholderPageProps {
  eyebrow: string;
  title: string;
  copy: string;
  features: string[];
}

export function PlaceholderPage({ eyebrow, title, copy, features }: PlaceholderPageProps) {
  return (
    <section className="placeholder-page">
      <p className="eyebrow">{eyebrow}</p>
      <h1>{title}</h1>
      <p>{copy}</p>
      <ul>{features.map((feature) => <li key={feature}>{feature}</li>)}</ul>
      <span className="phase-pill">Next implementation phase</span>
    </section>
  );
}
