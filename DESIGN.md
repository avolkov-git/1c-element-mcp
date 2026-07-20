# Design System

## Direction

A quiet Windows Server utility viewed in ordinary office light: neutral surfaces, strong text, one warm action color, and a small green operational indicator. The visual system is restrained and functional. It intentionally rejects the dark monospace developer-tool template suggested by the generic category search.

## Color

Use semantic OKLCH tokens. Light mode is the default physical scene; dark mode follows the operating-system preference.

- Background: neutral near-white, chroma 0.
- Surface: white in light mode and a lifted neutral in dark mode.
- Ink: near-black/near-white with at least WCAG AA contrast.
- Primary: restrained burnt coral around hue 36, used only for the main action.
- Success: green plus explicit status text.
- Warning/unavailable: warm neutral plus explicit recovery text, never color alone.

## Typography

Use the system UI stack. Headings, labels, and controls share one family. Data such as versions and filesystem paths may use the system monospace stack with tabular figures.

## Layout

One responsive column capped near 560px. A single bordered surface groups the status and update action; it has no decorative shadow. Spacing follows a 4/8px rhythm. The page remains usable at 320px and does not introduce navigation until a second destination actually exists.

## Components

- Operational status: static dot, heading, and explanatory sentence.
- Metadata rows: current version and update source, with wrapping for long paths.
- Update source editor: a progressively disclosed, explicitly labelled path field with inline validation and a return-to-origin action.
- Update state: an `aria-live` message with current, available, checking, applying, or unavailable copy.
- Primary button: one 48px control whose label changes with context. It has hover, focus-visible, active, disabled, loading, success, and error states.

## Motion

Only state feedback moves. Button press responds immediately with a subtle scale change; content replacement cross-fades quickly. There are no entrance sequences or pulsing status indicators. Reduced-motion removes transforms and nonessential transitions.
