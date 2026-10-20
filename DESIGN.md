---
name: Element MCP
description: Neutral light and dark settings UI in the approved XBSL.IO base-sera direction.
colors:
  background: "light-dark(#fff, #171717)"
  foreground: "light-dark(#171717, #fafafa)"
  muted: "light-dark(#666, #aaa)"
  border: "light-dark(#e5e5e5, #363636)"
  hover: "light-dark(#f5f5f5, #262626)"
  primary: "#4ba3f7"
  primary-foreground: "#101a24"
  primary-hover: "#70b7fa"
  success: "light-dark(#14805b, #69cda1)"
  error: "light-dark(#b42318, #ff8b82)"
  toast-surface: "light-dark(#fff, #222)"
  shadow: "light-dark(#00000014, #00000045)"
typography:
  body: {fontFamily: '"JetBrains Mono", ui-monospace, SFMono-Regular, Consolas, monospace', fontSize: "13px", fontWeight: 400, lineHeight: 1.6, letterSpacing: "0"}
  brand: {fontFamily: '"JetBrains Mono", ui-monospace, SFMono-Regular, Consolas, monospace', fontSize: "18px", fontWeight: 600, lineHeight: 1.4, letterSpacing: "0"}
  headline: {fontFamily: '"JetBrains Mono", ui-monospace, SFMono-Regular, Consolas, monospace', fontSize: "21px", fontWeight: 600, lineHeight: 1.4, letterSpacing: "0"}
  title: {fontFamily: '"JetBrains Mono", ui-monospace, SFMono-Regular, Consolas, monospace', fontSize: "14px", fontWeight: 500, lineHeight: 1.5, letterSpacing: "0"}
  label: {fontFamily: '"JetBrains Mono", ui-monospace, SFMono-Regular, Consolas, monospace', fontSize: "12px", fontWeight: 500, lineHeight: 1.6, letterSpacing: "0"}
  caption: {fontFamily: '"JetBrains Mono", ui-monospace, SFMono-Regular, Consolas, monospace', fontSize: "12px", fontWeight: 400, lineHeight: 1.6, letterSpacing: "0"}
  fine: {fontFamily: '"JetBrains Mono", ui-monospace, SFMono-Regular, Consolas, monospace', fontSize: "11px", fontWeight: 400, lineHeight: 1.6, letterSpacing: "0"}
rounded:
  control: "0"
  switch: "12px"
  round: "50%"
spacing:
  control-gap: "8px"
  compact-gap: "10px"
  item-gap: "12px"
  action-gap: "16px"
  header-gap: "20px"
  detail-gap: "24px"
  field-offset: "26px"
  action-offset: "28px"
  section-offset: "32px"
components:
  button-primary: {backgroundColor: "{colors.primary}", textColor: "{colors.primary-foreground}", typography: "{typography.label}", rounded: "{rounded.control}", padding: "8px 18px"}
  button-primary-hover: {backgroundColor: "{colors.primary-hover}"}
  button-secondary: {backgroundColor: "transparent", textColor: "{colors.foreground}", typography: "{typography.label}", rounded: "{rounded.control}", padding: "8px 18px"}
  icon-button: {backgroundColor: "transparent", textColor: "{colors.muted}", rounded: "{rounded.control}", width: "36px", height: "36px"}
  input: {backgroundColor: "transparent", textColor: "{colors.foreground}", typography: "{typography.body}", rounded: "{rounded.control}", padding: "8px 0", width: "100%"}
  navigation: {backgroundColor: "transparent", textColor: "{colors.muted}", typography: "{typography.caption}", rounded: "{rounded.control}", padding: "9px 12px"}
  navigation-current: {backgroundColor: "{colors.hover}", textColor: "{colors.foreground}"}
  switch: {backgroundColor: "{colors.hover}", rounded: "{rounded.switch}", width: "34px", height: "20px"}
  switch-checked: {backgroundColor: "{colors.primary}"}
  toast: {backgroundColor: "{colors.toast-surface}", textColor: "{colors.foreground}", typography: "{typography.caption}", rounded: "{rounded.control}", padding: "14px 12px 14px 16px"}
---

# Design System: Element MCP

## Overview

**Creative North Star: "XBSL.IO base-sera settings utility"**

The approved Element MCP mockup follows XBSL.IO's shadcn base-sera direction:
neutral surfaces, square controls, underlined inputs, JetBrains Mono, restrained
blue actions. This replaces the former coral, single-card interface. The page is
an operational settings tool, not a dashboard or a marketing surface.

Visual authority: the approved Element MCP HTML mockup and the requested header
theme toggle. Actual values come from
`src/element_mcp/ui_assets/{styles.css,index.html,app.js,theme.js}`.

**Key Characteristics:**
- Neutral light/dark surfaces and one blue action accent.
- Locally bundled JetBrains Mono and Phosphor assets.
- Four task-based destinations and flat, rule-separated groups.
- Square controls, underlined fields, and brief toast feedback.

Frontmatter owns primitives. The schema-v2 `.impeccable/design.json` extends them
with shadow/motion tokens, breakpoints, metadata, and component previews; it does
not duplicate primitives in a legacy `tokens` object. Product truth remains in `PRODUCT.md`.

## Colors

`primary` is the single blue accent for actions, checked controls, selection,
carets, focus, and the current-navigation stripe. `primary-foreground` is its dark
text; `primary-hover` is the literal hover fill, not a root custom property.
`background`, `foreground`, `muted`, `border`, and `hover` define neutral surfaces,
text, dividers, placeholders, and navigation states. `toast-surface` and `shadow`
serve transient overlays. `success` and `error` are semantic green/red, always
paired with a readable label or icon. Theme pairs preserve the source's
`light-dark(light, dark)` notation; exact values are normative in frontmatter.

## Typography

Use the frontmatter roles: `brand` for h1, `headline` for page h2, `title` for group
h3, `body` for ordinary text/fields, `label` for field labels and command buttons,
`caption` for navigation/status/tooltips/toasts, and `fine` for the footer.
Page headings become 18px at 700px; navigation becomes 11px at 420px. Coarse-pointer
text fields use 16px; the body remains 13px. Textareas use line-height 1.8 and detail
values use tabular numerals. Letter spacing is zero. Font weights 400/500/600 and
Phosphor icons are bundled locally with licenses; there is no CDN dependency.

## Layout

The desktop shell is capped at 1200px. The 210px navigation rail contains four
destinations: connections, documentation, agent actions and updates. Main content
has 34px 38px 40px padding and a 640px measure; sections use rules, not cards.
The header uses 20px/28px padding. Theme and restart icon buttons sit at the far
right, with restart last. The status indicator stays beside the product name.

The desktop grid minimum height is `calc(100svh - 124px)`; navigation is sticky
24px from the top. At 700px and below, navigation becomes a static two-column grid
above content; content padding is 26px 24px 40px. At 420px and below, content padding
is 24px 16px 36px and header/footer side padding is 16px. Forms and long messages
wrap; the no-horizontal-overflow target is 320px.

Coarse-pointer command buttons, icon buttons, navigation buttons, and switch labels
have a 44px minimum height; icon buttons also have a 44px minimum width. Source-mode
buttons retain their 40px minimum height, so this is not a universal touch-size rule.

## Elevation & Depth

Persistent surfaces are flat. Navigation uses `inset 2px 0 var(--primary)` as a
selection stripe. Toasts use `0 6px 24px var(--shadow)` at z-index 30; tooltips use
`0 4px 16px var(--shadow)` at z-index 15. No other drop-shadow vocabulary is present.

## Shapes

Buttons, fields, tooltips, and toasts are square. Fields have a one-pixel bottom
border; command buttons and toasts have full borders. Switches are the explicit
exception: a 34px by 20px track with 12px radius, a circular 12px thumb, and 14px
checked travel. Status dots are circular (6px); checkboxes retain native geometry
(16px) in ruled rows at least 49px high.

## Components

- Commands use 40px minimum height, 8px icon gaps, and the frontmatter padding.
  Secondary and icon-button hover use `hover`; primary hover uses `primary-hover`.
  Disabled buttons use opacity 0.45 and suppress hover fill.
- Header tools are 36px square with 17px Phosphor glyphs. Theme precedes restart.
  Accessible names and hover/focus tooltips identify both icons.
- Fields are transparent, underlined, and at least 40px high. Placeholders use
  `muted` at full opacity; invalid fields use an error-colored bottom border.
- Navigation items are at least 42px high; `aria-current="page"` adds the neutral
  hover fill, foreground text, and blue stripe. Session storage restores the chosen
  section when available; otherwise the initial section is connections.
- Source-mode selection uses `aria-pressed` and a two-pixel foreground underline,
  not a filled blue segment. Detail rows use muted terms and right-aligned values.

- Theme defaults to the OS preference; the header toggle saves a local browser
  preference. Unavailable storage must not prevent the UI from starting.
- Switches represent Console connection and managed actions. Existing API behavior
  is retained: disabling is immediate; action enablement requires explicit save.
- Protected Console secrets never return from the server. An empty password input
  with a saved-secret placeholder reuses the stored value. Visibility affects only
  the new text typed into this field.
- Runtime setup asks only for the instance directory. Application Manager state
  stays read-only. Startup-locked documentation paths stay disabled.
- Operation success and failure appear as fixed bottom-right toasts for 3000ms,
  with a close button and accessible announcements. Up to three are visible;
  additional messages are queued and each receives its own full lifetime.
  The stack is `min(420px, calc(100% - 32px))` wide, with a 10px gap and 24px viewport
  offsets (16px at 420px). Success announces politely; errors assertively. Close
  buttons and Escape while focused inside a toast dismiss it.
- Field errors also remain available to assistive technology through
  aria-describedby and aria-invalid, without adding inline notification blocks.
- Persistent configuration status is compact text. No verbose feature explanations.
- Restart preserves the existing Windows service API and reloads the page after
  recovery to obtain a fresh CSRF token. Unsupported hosts keep the icon disabled.
- Update source selection preserves immediate origin switching and the established
  save/check/apply flow for a trusted local Git source.

### States And Motion

Keyboard focus uses a two-pixel blue outline with four-pixel offset. Only the busy
icon (1s linear infinite rotation) and switch thumb (120ms ease) animate;
reduced-motion disables both, not the toast timer. Only outer disabled Console and
action fieldsets receive group opacity (0.5), avoiding nested fieldset attenuation.
Toast dismissal restores focus when needed. There are no custom press transforms.

### Reference Differences

The implementation uses bundled Phosphor instead of the mockup's Lucide icons,
local fonts instead of Google Fonts, the requested header theme toggle, a
viewport-height shell instead of a fixed 590px minimum, and fixed queued toasts
instead of one absolute-positioned toast. Documentation shows live document and
fragment counts rather than example catalog categories; remote updates say origin
rather than assuming GitHub. The mockup's unused blue-text and quiet-button tokens
are not implemented. This document records source, not a new runtime verification.

## Do's and Don'ts

### Do:
- Do preserve the approved neutral themes, single blue accent, and local assets.
- Do keep four destinations and theme then restart at the header's far right.
- Do use square controls, underlined fields, and rule-separated groups.
- Do retain readable status, visible focus, accessible errors, and reduced motion.
- Do give each displayed toast its full three-second lifetime.

### Don't:
- Don't restore coral cards or add dashboard grids, gradients, or glass effects.
- Don't add decorative imagery, entrance animations, or verbose feature explanations.
- Don't force square geometry onto switches, status dots, or native checkboxes.
- Don't treat mockup sample data as product facts or documentation as runtime proof.
