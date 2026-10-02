# UI Micro-Animations Plan — UpScale Scouting Tool

A phased plan to take the dashboard UI to top-notch with micro-animations.
Everything here respects the brand rules in `BRAND.md` (dark-first, Ink/Panel/
Hairline, UpScale Orange as the only accent, "Tool, not toy" voice) and the
technical reality of the app (`app.py`, Streamlit + `st.html` blocks with
`unsafe_allow_javascript=True` for the copy/gate scripts).

**Ground rule:** motion must communicate state, not decorate. Every animation
below answers "what just changed?" — a row arrived, a copy succeeded, a sync
finished. No bouncing, no confetti, no idle loops except the two explicitly
listed live indicators.

---

## 0. Motion design system (do this first)

Create one shared style block (`_MOTION_CSS`) injected once at app start, so
every animation uses the same vocabulary instead of ad-hoc CSS per surface.

### Tokens

```css
:root {
  /* Durations — never longer than 320ms for feedback */
  --motion-fast: 120ms;   /* hover, press, toggle          */
  --motion-base: 200ms;   /* enter/exit, color transitions */
  --motion-slow: 320ms;   /* panels, page transitions      */
  /* Easing — a single outgoing curve for everything small */
  --ease-out: cubic-bezier(0.22, 1, 0.36, 1);
}
```

### Reduced motion

Every animated rule sits behind one media query so a single flag disables all
of it for users who ask the OS for less motion (and for `@test` snapshots):

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
```

### Rules of the road

1. **Animate only `transform` and `opacity`** — they run on the compositor.
   Never animate `width`/`height`/`top` (jank on the 20-row table).
2. **150–320ms** for everything; nothing loops forever except the sync pulse
   and the stale-data shimmer.
3. Orange `#FF6E01` flashes only for positive/active signal (a fresh blow-up,
   a completed sync) — same semantics as the Momentum column.
4. Every new animation must survive `st.rerun()` (Streamlit remounts HTML
   blocks) — keyframe entrances must be re-triggerable, or gated behind a
   `data-just-changed` attribute set server-side.

---

## Phase 1 — CSS-only quick wins (no JS, ~half a day, biggest visible lift)

### 1.1 Table rows: entrance stagger
When a page renders, rows fade-and-rise in with an 18ms per-row delay. This is
the single most "premium" feel-per-line-of-CSS in the plan.

```css
.ss-table tbody tr {
  opacity: 0;
  transform: translateY(6px);
  animation: ss-row-in var(--motion-base) var(--ease-out) forwards;
}
.ss-table tbody tr:nth-child(1) { animation-delay: 18ms; }
/* ...increment 18ms per row, cap at ~200ms (11 rows) — or generate inline
   style="animation-delay" per row in render_table() for arbitrary page sizes */
@keyframes ss-row-in { to { opacity: 1; transform: none; } }
```
Re-triggers naturally on every page change since Streamlit remounts the block.

### 1.2 Row hover: quiet lift, not just background
Upgrade the existing `tr:hover` background tint to a subtle scale + accent
hairline on the left edge:

```css
.ss-table tbody tr { transition: background var(--motion-fast) linear; }
.ss-table tbody tr:hover { background: rgba(255,255,255,0.035); }
```

### 1.3 Buttons: press depth + hover glow
Streamlit `[data-testid="stButton"] button` and the `.ss-copy` cell buttons:

```css
button, .ss-copy { transition: transform var(--motion-fast) var(--ease-out),
                                background var(--motion-fast) linear,
                                box-shadow var(--motion-fast) linear; }
button:hover:not(:disabled) { box-shadow: 0 0 0 1px rgba(255,110,1,0.35); }
button:active:not(:disabled) { transform: scale(0.97); }
```

### 1.4 Game thumbnails: zoom-on-hover
The 44px `.ss-thumb` eases to a slight scale with a ring in Hairline color —
signals "this is a link" before the underline appears:

```css
.ss-thumb { transition: transform var(--motion-base) var(--ease-out); }
.ss-game:hover .ss-thumb { transform: scale(1.08); }
```

### 1.5 Genre pills: color-in on hover
Pills currently sit flat; a background-wash transition (Panel → slight orange
tint border) makes them feel alive without adding color noise.

### 1.6 Copy button success morph
The copy button already flips its label after copy — add a 160ms scale pulse
in orange so "copied" registers peripherally (see 2.2 for the JS side).

### 1.7 Sidebar: focus-ring polish
Custom thin orange focus ring (`box-shadow: 0 0 0 2px rgba(255,110,1,0.5)`)
on all inputs with a `--motion-fast` transition — currently browser-default.

### 1.8 Skeleton shimmer for the first load
Replace the `st.spinner("Loading games...")` for the table area with a
shimmering skeleton panel (3 gray rows, Hairline gradient sweep) rendered as
the table's loading state. Same skeleton treatment for icon-less games:
the gray `.ss-fallback` square gets a gentle one-time shimmer when its real
thumbnail is missing.

---

## Phase 2 — JS-assisted feedback (uses the existing `st.html` script blocks)

### 2.1 Number count-up on the live catalog tracker
The four tracker numbers (total cataloged, meeting target, discovered today,
last sync) count from the previous value to the new one over ~500ms when a
sync changes them. Requires persisting the previous values (hidden DOM node
or `localStorage`), animating with `requestAnimationFrame`, tabular-nums to
prevent width jitter.

### 2.2 Copy confirmation: checkmark draw
On copy success, swap the button content to a small inline SVG checkmark whose
stroke is drawn via `stroke-dashoffset` animation (~250ms), then fade back to
"Copy" after 1.2s. Orange stroke. This lives inside the existing
`_COPY_SCRIPT` — one `onclick` handler change.

### 2.3 Toast on sync completion
Replace/augment the sidebar error line: after a successful "Sync live data",
slide a small toast up from the bottom-right corner ("Catalog synced · N
games refreshed") that auto-dismisses after 3s. Pure DOM in the existing
script block; `translateY` + opacity, `--motion-slow`.

### 2.4 Momentum deltas: tick-in animation
When a Momentum (1d) value is positive/negative and *new* since the last
render, tick it in: value fades up 4px with the orange/red color fading in
200ms after. Implemented with a `data-momentum` attribute diff against the
last rendered value stored in a hidden element.

### 2.5 Blow-up flag pulse (watchlist only)
Rows in the New & Upcoming table get a **one-time** two-pulse orange left
border on entrance (`box-shadow` keyframes, 700ms total, then settles). This
is the only place a repeated pulse is acceptable — it IS the product signal.

### 2.6 View switch transition
When toggling Main scout ↔ New & Upcoming, the results panel fades/slides in
(8px up, 240ms). Cheap to do: a class on the `.ss-panel` that the remount
animation in 1.1 mostly provides already — just extend the stagger to the
panel container.

---

## Phase 3 — Delight layer (small, only after Phase 1–2 land clean)

### 3.1 Pagination: page-number slide
The "Page N of M" caption crossfades when N changes; Previous/Next press
triggers a directional 12px slide of the table (right on next, left on
previous). Directional context removes the disorientation of a full re-stagger.

### 3.2 CSV export button: success state
After clicking "Export visible results (CSV)", the button label morphs to
"Exported ✓" with the same checkmark-draw treatment as 2.2, reverting after
2s.

### 3.3 Gate screen entrance
The password card fades up (12px, 280ms) and the logo mark scales in from
0.96 → 1.0. On wrong password, the card does a 4px horizontal shake
(240ms, single cycle) — instant, physical feedback that needs no text.

### 3.4 Discord invite cells: resolve-in
When a Discord verdict resolves (logo appears where "—" was), the cell
crossfades from the placeholder to the Discord logo + invite text over
200ms. Uses a `data-resolved` attribute diff like 2.4.

### 3.5 Scroll-linked header (subtle)
The results panel's top border hairline brightens to orange-tinted as the
table scrolls horizontally (a scroll listener toggles one class). Signals
"there is more content to the right" on narrow screens — functional, not
decorative.

---

## Phase 4 — Deliberately NOT doing (and why)

| Idea | Why not |
|---|---|
| Cursor-following spotlight / parallax | Toy, not tool; brand voice says no |
| Animated gradient backgrounds | Fights the quiet Ink background the mark is designed against |
| Infinite spinners on every button | Skeletons (1.8) + toasts (2.3) communicate better |
| Column reorder drag physics | Streamlit's DOM fights it; cost >> value |
| Spring/confetti on copy | Breaks "short imperative" voice; checkmark is enough |
| Any animation > 500ms | Feels laggy at table density (20 rows) |

---

## Implementation order & effort

| Phase | Scope | Effort | Risk |
|---|---|---|---|
| 0. Motion tokens + reduced-motion guard | `_MOTION_CSS` block | 1 h | none |
| 1. CSS quick wins (1.1–1.8) | `TABLE_STYLE`, sidebar CSS, gate CSS | half day | none — pure CSS |
| 2. JS feedback (2.1–2.6) | `_COPY_SCRIPT`, tracker block, new toast block | 1–2 days | low — must survive `st.rerun()` |
| 3. Delight layer (3.1–3.5) | spread across table + gate | 1 day | low |

### Acceptance checklist per change

- [ ] Runs at 60fps on the 40-games-per-page table (check with DevTools
      Performance panel — compositor-only properties only)
- [ ] `prefers-reduced-motion: reduce` disables it
- [ ] Survives `st.rerun()` and a full page reload without stuck mid-states
- [ ] No new color outside BRAND.md tokens (Orange positive, `#EF4444`
      negative, Hairline structure)
- [ ] No emoji introduced anywhere (voice rule)
- [ ] Streamlit Cloud still fast: no new fonts, no JS libraries — vanilla
      CSS/JS only, total added weight < 6 KB

### Testing

- Add a pytest in `tests/` asserting `TABLE_STYLE` contains the reduced-motion
  guard and the `ss-row-in` keyframes (string-presence test, same pattern as
  existing table tests) so a CSS refactor can't silently drop them.
- Manual pass matrix: Chrome + Safari, 40-row page, watchlist view, gate
  screen wrong-password path, mobile-width (streamlit on a phone browser).

---

## One-line summary

**Phase 0+1 alone (one day of work) delivers ~80% of the perceived polish:**
staggered row entrances, hover lift, press depth, skeleton shimmer, and a
motion token system with reduced-motion respect. Phase 2 adds the honest
feedback layer (count-up tracker, copy checkmark, sync toast), Phase 3 the
final flourishes.
