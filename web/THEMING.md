# Web color and theme system

The browser UI uses [Radix Colors](https://www.radix-ui.com/colors) as its curated palette and a
small semantic token layer in `src/styles.css`. Components should express visual intent through the
semantic tokens rather than selecting hex values or Radix scale steps directly.

## Palette

The application loads matching light and dark variants of these Radix scales:

- **Sage** for page, panel, border, and text neutrals
- **Green** for brand, interactive, positive, and focus states
- **Lime** for the existing highlighted brand accent
- **Amber** for warnings and validation feedback
- **Red** for destructive and error states

`App.tsx` keeps `data-theme` for existing selectors and also applies Radix's `light` or `dark` class
to the document element. The class activates the corresponding Radix scale values.

## Semantic tokens

Global semantic tokens use the `--color-*` prefix. Examples include:

```css
--color-page
--color-panel
--color-border
--color-text
--color-muted-text
--color-primary-surface
--color-primary-text
--color-positive-surface
--color-positive-text
--color-focus-ring
--color-danger-surface
--color-danger-text
--color-warning-surface
--color-warning-text
```

Legacy variables such as `--ink`, `--muted`, and `--line` remain as transitional aliases so the UI
can migrate incrementally without a large visual rewrite.

## Component rules

1. Use an existing semantic token whenever one describes the intended role.
2. Add a reusable semantic token when the role is shared across components.
3. Do not add component-specific light and dark color pairs.
4. Do not use raw Radix scale tokens inside a component unless the style is part of the global theme
   foundation; map the scale step to a semantic token first.
5. Reserve literal colors for non-theme assets or exceptional data visualizations, and document the
   reason.
6. Validate interactive states in both themes. Hover, focus, active, and disabled states must remain
   distinguishable without relying on color alone.

Radix's scale conventions should guide new semantic mappings: steps 1–2 for app backgrounds, 3–5
for component surfaces, 6–8 for borders and focus rings, 9–10 for solid interactive fills, and
11–12 for text.
