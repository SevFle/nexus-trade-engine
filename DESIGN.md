# Nexus Trade Engine — Design System

**Visual language:** Nothing-inspired industrial design. Monochrome, typographic, mechanical. A trading terminal that feels like an instrument panel — data as beauty, precision in every pixel.

**Fonts:** Space Grotesk (body/UI), Space Mono (data/labels), Doto (hero moments — dot-matrix display)
**Load via Google Fonts:** `https://fonts.googleapis.com/css2?family=Doto:wght@400;700&family=Space+Grotesk:wght@300;400;500;700&family=Space+Mono:wght@400;700&display=swap`

---

## 1. Design Philosophy

- **Subtract, don't add.** Every pixel earns its place. Default to removal.
- **Data as beauty.** `$2,847,391.44` in Space Mono at 48px IS the visual. No illustrations.
- **Type does the heavy lifting.** Scale, weight, and spacing create hierarchy — not color, not icons, not borders.
- **Structure is ornament.** Expose the grid. Let the data breathe.
- **Mechanical honesty.** Controls look like physical instruments. Toggles are switches. Gauges are dials. Progress bars are segmented hardware.
- **Both modes are first-class.** Dark = OLED instrument panel. Light = printed technical manual. Neither derived — both intentional. System preference respected with manual override.
- **Color is a signal, not decoration.** Status colors encode data state. Red is urgent. Green is confirmed. Gray is everything else.

---

## 2. Users & Roles

The dashboard serves three distinct personas. Navigation adapts; the visual language stays consistent.

| Role | Primary Need | Key Screens |
|------|-------------|-------------|
| **Quant Developer** | Build, test, deploy strategies | Strategy runner, Plugin dev console, Cost analysis |
| **Retail Trader** | Run pre-built strategies, monitor portfolio | Dashboard overview, Positions & orders, Marketplace |
| **Portfolio Manager** | Monitor risk, allocations, performance across strategies | Dashboard overview, Risk monitor, Positions & orders |

Role-based navigation filtering is applied server-side. The UI never hides features client-side — all role logic is backend-enforced.

---

## 3. Color System

### Dark Mode (default)

| Token | Hex | Role |
|-------|-----|------|
| `--black` | `#000000` | Primary background (OLED) |
| `--surface` | `#111111` | Elevated surfaces, cards |
| `--surface-raised` | `#1A1A1A` | Secondary elevation |
| `--border` | `#222222` | Subtle dividers |
| `--border-visible` | `#333333` | Intentional borders |
| `--text-disabled` | `#666666` | Disabled, timestamps |
| `--text-secondary` | `#999999` | Labels, captions, metadata |
| `--text-primary` | `#E8E8E8` | Body text |
| `--text-display` | `#FFFFFF` | Headlines, hero numbers |

### Light Mode

| Token | Hex |
|-------|-----|
| `--black` | `#F5F5F5` |
| `--surface` | `#FFFFFF` |
| `--surface-raised` | `#F0F0F0` |
| `--border` | `#E8E8E8` |
| `--border-visible` | `#CCCCCC` |
| `--text-disabled` | `#999999` |
| `--text-secondary` | `#666666` |
| `--text-primary` | `#1A1A1A` |
| `--text-display` | `#000000` |

### Status Colors (identical in both modes)

| Token | Hex | Meaning |
|-------|-----|---------|
| `--success` | `#4A9E5C` | Profit, confirmed, connected, healthy |
| `--warning` | `#D4A843` | Caution, pending, degraded, approaching limit |
| `--accent` | `#D71921` | Loss, error, urgent, over limit, destructive action |
| `--accent-subtle` | `rgba(215,25,33,0.15)` | Red tint background |
| `--interactive` | `#5B9BF6` (dark) / `#007AFF` (light) | Tappable text, links |

**Trading-specific color rules:**
- P&L values: green for profit, red for loss, `--text-primary` for flat
- Trend arrows inherit the value's status color
- Status color applies to the **value only**, never to labels or backgrounds
- Labels always stay `--text-secondary`

---

## 4. Typography

### Font Stack

| Role | Font | Fallback | Weights |
|------|------|----------|---------|
| Display | Doto | Space Mono, monospace | 400, 700 |
| Body / UI | Space Grotesk | DM Sans, system-ui, sans-serif | 300, 400, 500, 700 |
| Data / Labels | Space Mono | JetBrains Mono, SF Mono, monospace | 400, 700 |

### Type Scale

| Token | Size | Tracking | Use |
|-------|------|----------|-----|
| `--display-xl` | 72px | -0.03em | Portfolio value, hero P&L |
| `--display-lg` | 48px | -0.02em | Section heroes, allocation % |
| `--display-md` | 36px | -0.02em | Page titles, key metrics |
| `--heading` | 24px | -0.01em | Section headings, card titles |
| `--subheading` | 18px | 0 | Subsections, strategy names |
| `--body` | 16px | 0 | Body text, descriptions |
| `--body-sm` | 14px | 0.01em | Secondary body, compact data |
| `--caption` | 12px | 0.04em | Timestamps, footnotes |
| `--label` | 11px | 0.08em | ALL CAPS monospace labels |

### Rules

- **Doto:** 36px+ only, hero moments only (portfolio value, total P&L). Never for body text.
- **Space Mono:** All numbers, data values, labels. Labels always ALL CAPS with 0.06–0.1em tracking.
- **Space Grotesk:** All prose, descriptions, headings, navigation.
- **Max 2 font families per screen.** Max 3 sizes. Max 2 weights. Budget thinking.
- **Currency formatting:** `$` as `--label` size, amount in display size. `USD` in `--label` size right-aligned.

---

## 5. Spacing

8px base grid. Spacing communicates relationship.

| Token | Value | Meaning |
|-------|-------|---------|
| `--space-2xs` | 2px | Optical adjustments |
| `--space-xs` | 4px | Icon-to-label, number-to-unit |
| `--space-sm` | 8px | Component internals, tight grouping |
| `--space-md` | 16px | Standard padding, list items |
| `--space-lg` | 24px | Group separation |
| `--space-xl` | 32px | Section margins |
| `--space-2xl` | 48px | Major section breaks |
| `--space-3xl` | 64px | Page-level rhythm |
| `--space-4xl` | 96px | Hero breathing room |

---

## 6. Layout

### Grid

- Desktop: 12-column grid, 24px gutters, 32px page margins
- Tablet: 8-column grid, 16px gutters, 24px margins
- Mobile: 4-column grid, 16px gutters, 16px margins

### Page Structure

```
┌─────────────────────────────────────────────────┐
│  NAV BAR                                        │
├──────────┬──────────────────────────────────────┤
│          │                                       │
│  SIDEBAR │  MAIN CONTENT                         │
│  (collaps-│  ┌─────────────────────────────────┐ │
│   ible)   │  │  HERO SECTION (primary metric)  │ │
│          │  └─────────────────────────────────┘ │
│          │  ┌──────┬──────┬──────┬──────┐       │
│          │  │ CARD │ CARD │ CARD │ CARD │       │
│          │  └──────┴──────┴──────┴──────┘       │
│          │  ┌─────────────────────────────────┐ │
│          │  │  DETAIL SECTION                  │ │
│          │  └─────────────────────────────────┘ │
└──────────┴──────────────────────────────────────┘
```

### Sidebar Navigation

- Width: 240px expanded, 64px collapsed (icon-only)
- Items: Space Mono, ALL CAPS, 13px, 0.06em tracking
- Active: `--text-display` + left 2px `--accent` bar
- Inactive: `--text-secondary`
- Hover: text brightens to `--text-primary`
- Collapse toggle at bottom

---

## 7. Core Screens

### 7.1 Dashboard Overview

The home screen. One hero metric, secondary metrics in widget cards, tertiary navigation.

**Primary (hero):** Portfolio total value in Doto at `--display-xl`. Center-left, vast breathing room. Change indicator (daily P&L $ and %) in status color below, `--heading` size.

**Secondary (widget row):** 4 equal-width surface cards:
- Day P&L (dollar + sparkline)
- Total return (% + segmented progress bar)
- Active positions (count + mini allocation arc)
- Strategy health (status indicator + running count)

**Tertiary:** Timestamp (last updated), market status, connection status — bottom-right, `--caption`, `--text-secondary`.

**Data density:** Balanced. Hero dominates, cards provide at-a-glance context, detail comes on drill-down.

### 7.2 Strategy Runner

Configure and execute a strategy across backtest/paper/live modes.

**Primary:** Strategy name in `--heading` + execution mode segmented control (BACKTEST / PAPER / LIVE).

**Secondary:** Parameter configuration panel:
- Inputs with Space Mono labels, underline style
- Parameter groups separated by spacing (not borders)
- Run button (primary variant) prominent at bottom

**Tertiary:** Execution status, last run timestamp, result summary.

**Mode switching:** Segmented control at top. BACKTEST = date range picker. PAPER = live status. LIVE = broker connection + confirmation gate.

**Cost model preview:** Inline summary after parameters. Shows estimated commission, spread, slippage as stat rows.

### 7.3 Strategy Marketplace

Browse, compare, install community strategy plugins.

**Primary:** Category filter (tag chips) + search bar.

**Secondary:** Strategy cards in a 3-column grid. Each card:
- `--surface` bg, 16px radius
- Strategy name (Space Grotesk `--subheading`)
- Category tags (pill chips)
- Performance summary (annualized return %, Sharpe, max drawdown) as stat rows
- Author + install count (`--caption`, `--text-secondary`)
- Install button (secondary variant)

**Tertiary:** Sort controls, result count, pagination.

### 7.4 Positions & Orders

Active positions, order history, execution details.

**Primary:** Total exposure / position count.

**Secondary:** Data table — the densest screen. Space Mono numeric cells, Space Grotesk text cells. Right-aligned numbers. Columns: Symbol, Side, Qty, Avg Price, Current, P&L ($), P&L (%), Strategy.

- P&L values colored by status
- Active row: `--surface-raised` bg + left 2px accent bar
- No zebra striping. Dividers only.

**Order history:** Same table format below, collapsible. Columns: Time, Symbol, Side, Type, Qty, Price, Status, Strategy.

**Real-time:** Price and P&L columns update via WebSocket. Values flash briefly (opacity pulse 0.6→1.0, 200ms) on change.

### 7.5 Cost Analysis

Commission, slippage, spread, tax breakdown per strategy and per trade.

**Primary:** Total cost drag on portfolio (% of returns) as hero number.

**Secondary:** Cost breakdown by category:
- Segmented bar for each cost type (commission, spread, slippage, taxes)
- Stat rows for per-trade averages
- Comparison: "with cost model" vs "without" as sparkline overlay

**Tertiary:** Date range selector, export controls, methodology notes.

### 7.6 Risk Monitor

Drawdown, VaR, exposure, concentration limits.

**Primary:** Current portfolio risk score as a circular gauge (thin stroke arc). Color mapped: green → yellow → red based on threshold proximity.

**Secondary:** Risk metrics as stat rows:
- Max drawdown (current vs limit — segmented bar)
- VaR (95%, 99%) with confidence indicator
- Sector/asset concentration (horizontal stacked bars)
- Strategy correlation matrix (dot grid, opacity = correlation strength)

**Tertiary:** Risk model parameters, last calculation timestamp, alert thresholds.

**Alerts:** When a metric breaches threshold, the gauge segment fills `--accent` red and the stat row label gains a `[!]` prefix in `--accent`.

### 7.7 Plugin Dev Console

SDK reference, local testing, manifest editing for strategy developers.

**Primary:** Code editor area (takes 60% of viewport). Syntax highlighting with monochrome theme (white text on `--surface` bg, `--text-secondary` for comments, `--interactive` for keywords).

**Secondary:** Split panels:
- Left: file tree / manifest editor
- Right: test output console (Space Mono, `--body-sm`, scrollable)

**Tertiary:** SDK version, test runner status, validation state.

**Layout:** IDE-like. No decorative elements. Pure function. The dot-grid motif appears subtly in the console background at very low opacity.

---

## 8. Real-Time Data Strategy

**Hybrid approach:**

| Data Type | Update Method | Frequency | Visual Feedback |
|-----------|--------------|-----------|-----------------|
| Portfolio value | WebSocket | Tick-level | Opacity pulse 0.6→1.0, 200ms |
| Position P&L | WebSocket | Tick-level | Status color update, flash |
| Prices | WebSocket | Tick-level | Value swap, no animation |
| Strategy status | WebSocket | Event-driven | Inline `[RUNNING]` / `[STOPPED]` text |
| Market status | Poll | 5s | Timestamp update |
| Cost analysis | Poll | 30s | Full re-render |
| Risk metrics | Poll | 10s | Gauge/bar re-render |

**Connection state:** Bottom-right corner. `[CONNECTED]` / `[RECONNECTING...]` in Space Mono `--caption`. Green dot or pulsing amber dot.

**Offline fallback:** Stale data shown with dimmed values (`--text-disabled`) and `[LAST UPDATE: HH:MM:SS]` timestamp.

---

## 9. Components

### Buttons

| Variant | Background | Border | Text | Use |
|---------|-----------|--------|------|-----|
| Primary | `--text-display` | none | `--black` | "RUN STRATEGY", "INSTALL" |
| Secondary | transparent | `1px --border-visible` | `--text-primary` | "EXPORT", "FILTER" |
| Ghost | transparent | none | `--text-secondary` | Navigation, dismiss |
| Destructive | transparent | `1px --accent` | `--accent` | "STOP STRATEGY", "CLOSE ALL" |

All: Space Mono, 13px, ALL CAPS, 0.06em tracking, 12px 24px padding, min-height 44px, pill radius (999px).

### Cards / Widgets

- Background: `--surface`, border: `1px solid --border` or none
- Radius: 16px cards, 8px compact, 4px technical
- Padding: 16–24px. No shadows. Flat surfaces, border separation.
- Hero metric: large Doto/Space Mono, left-aligned
- Unit: `--label` size, adjacent to value
- Category label: ALL CAPS Space Mono, top-left, `--text-secondary`

### Data Tables

- Header: `--label` style (Space Mono, ALL CAPS, `--text-secondary`), bottom border `--border-visible`
- Cells: Space Mono for numbers, Space Grotesk for text. Padding: 12px 16px.
- Numbers right-aligned, text left-aligned.
- No zebra striping, no cell backgrounds.
- Active row: `--surface-raised` bg, left 2px `--accent` indicator.
- Sortable columns: `▼` / `▲` in `--text-disabled`, active sort in `--text-primary`.

### Segmented Progress Bars

The signature visualization. Discrete rectangular segments with 2px gaps.

- Filled = status color. Empty = `--border` (dark) / `#E0E0E0` (light).
- Square ends, no border-radius.
- Always paired with numeric readout.
- Sizes: Hero 16–20px, Standard 8–12px, Compact 4–6px.
- Use for: allocation %, drawdown vs limit, strategy progress, cost breakdown.

### Gauges

- Thin stroke arc (2–3px), `--border-visible` track, status-colored fill.
- Numeric readout centered in `--display-md` Space Mono.
- Tick marks at 25/50/75% thresholds, `--border-visible` color.
- Use for: risk score, strategy health, portfolio concentration.

### Sparklines

- Line 1.5px, `--text-display` color.
- Zero line: 1px dashed `--border-visible`.
- No area fill, no dot markers, no axis labels.
- Paired with stat value to the left.
- Use for: P&L trends, equity curves, cost over time.

### Stat Rows

```
LABEL (Space Mono, ALL CAPS, --text-secondary)     VALUE (--text-primary, status-colored)
```

The fundamental building block for all secondary data. Compact, scannable, repeatable.

### Inputs

- Underline style: `1px solid --border-visible` bottom
- Label above: Space Mono, ALL CAPS, `--text-secondary`
- Focus: border → `--text-primary`
- Error: border → `--accent`, message below in `--accent`
- Data entry: Space Mono input text

### Navigation

- Sidebar: 240px expanded / 64px collapsed
- Items: Space Mono, ALL CAPS, 13px
- Active: `--text-display` + left 2px accent bar
- Inactive: `--text-secondary`
- Mobile: bottom bar with same ALL CAPS treatment

### Tags / Chips

- Border: `1px solid --border-visible`, no fill
- Text: Space Mono, `--caption`, ALL CAPS
- Radius: 999px (pill) or 4px (technical)
- Use for: strategy categories, filter tags, status indicators

### Segmented Control

- Container: `1px solid --border-visible`, pill or 8px rounded
- Active: `--text-display` bg, `--black` text (inverted)
- Inactive: transparent, `--text-secondary`
- Max 4 segments. Use for: BACKTEST/PAPER/LIVE, time periods.

---

## 10. State Patterns

### Loading
- `[LOADING...]` bracket text in Space Mono, `--caption`
- Segmented spinner (rotating hardware-style) for full-page loads
- No skeleton screens. Ever.

### Error
- Inline: `[ERROR]` prefix in `--accent`, message in `--text-primary`
- Form: input border → `--accent` + message below
- API: full-page centered: status code in `--display-md`, message in `--body`, retry button (secondary variant)
- Never red backgrounds, alert banners, or full-page error illustrations

### Empty
- Centered, 96px+ top padding
- Headline in `--text-secondary`, 1 sentence
- Optional dot-matrix illustration (low opacity)
- No mascots, no sad faces, no multi-paragraph empty states

### Real-Time Disconnected
- Values dim to `--text-disabled`
- `[OFFLINE]` badge in bottom-right
- Last known timestamp shown

---

## 11. Motion

- **Duration:** 150–250ms micro, 300–400ms transitions
- **Easing:** `cubic-bezier(0.25, 0.1, 0.25, 1)` — subtle ease-out
- Prefer opacity over position. Elements fade, never slide.
- Hover: border/text brightens. No scale, no shadows.
- Real-time flash: opacity 0.6 → 1.0 over 200ms on value change
- No spring/bounce, no parallax, no scroll-jacking.

---

## 12. Anti-Patterns

- No gradients in UI chrome
- No shadows or blur. Flat surfaces, border separation.
- No skeleton loading screens
- No toast popups — use inline `[SAVED]`, `[ERROR: ...]`
- No zebra striping in tables
- No filled or multi-color icons
- No parallax, scroll-jacking, or gratuitous animation
- No border-radius > 16px on cards
- No emoji as UI elements
- No decorative use of `--accent` red
- Data viz: differentiate with opacity or pattern before introducing color

---

## 13. Iconography

- Library: Lucide (thin variant) — already in `package.json`
- Style: Monoline, 1.5px stroke, no fill, 24x24 base
- Color: inherits text color
- Max 5–6 strokes per icon
- Never filled, multi-color, or emoji

---

## 14. Dot-Matrix Motif

Used sparingly as the "one moment of surprise" per screen:

- Hero typography via Doto font (portfolio value, total P&L)
- Subtle background grids at very low opacity (0.05–0.1)
- Loading indicators
- Empty state illustrations
- Correlation matrix heat maps

CSS:
```css
.dot-grid {
  background-image: radial-gradient(circle, var(--border-visible) 1px, transparent 1px);
  background-size: 16px 16px;
}
```

---

## 15. Platform Implementation

### Tech Stack

| Component | Technology |
|-----------|-----------|
| Framework | React 18 |
| Build | Vite 5 |
| Styling | Tailwind CSS 3.4 |
| Charts | Recharts 2.12 |
| Icons | Lucide React 0.383 |
| Data fetching | TanStack React Query 5.40 |
| Routing | React Router DOM 6.23 |
| Utility | clsx 2.1 |

### Tailwind Integration

Extend `tailwind.config.js` with all tokens as custom theme values. Use CSS custom properties for dark/light mode switching via `class` strategy on `<html>`.

### Font Loading

Add to `index.html`:
```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Doto:wght@400;700&family=Space+Grotesk:wght@300;400;500;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
```

### Component Architecture

```
src/
├── components/
│   ├── primitives/       # Text, StatRow, HeroMetric, StatusBadge
│   ├── layout/           # Shell, Sidebar, Navbar, PageHeader
│   ├── data/             # DataTable, SegmentedBar, Gauge, Sparkline
│   ├── forms/            # Input, SegmentedControl, DatePicker, Toggle
│   ├── feedback/         # InlineStatus, LoadingSpinner, EmptyState
│   └── trading/          # PositionRow, OrderRow, PnLDisplay, CostBreakdown
├── screens/
│   ├── Dashboard/
│   ├── StrategyRunner/
│   ├── Marketplace/
│   ├── Positions/
│   ├── CostAnalysis/
│   ├── RiskMonitor/
│   └── DevConsole/
├── hooks/
│   ├── useTheme.js
│   ├── useWebSocket.js
│   └── useMarketStatus.js
└── styles/
    └── tokens.css        # CSS custom properties for both modes
```

### Theme Switching

```js
// useTheme.js — respects system preference, manual override persists to localStorage
const useTheme = () => {
  const [mode, setMode] = useState(() => {
    const stored = localStorage.getItem('nexus-theme');
    if (stored) return stored;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  });

  useEffect(() => {
    document.documentElement.classList.toggle('dark', mode === 'dark');
    localStorage.setItem('nexus-theme', mode);
  }, [mode]);

  return { mode, setMode, toggle: () => setMode(m => m === 'dark' ? 'light' : 'dark') };
};
```

---

## 16. Visual Hierarchy Per Screen

Every screen follows the three-layer rule. Here's the primary/secondary/tertiary mapping:

| Screen | Primary (hero) | Secondary (supporting) | Tertiary (metadata) |
|--------|---------------|----------------------|-------------------|
| Dashboard | Portfolio value (Doto 72px) | 4 widget cards | Timestamp, connection |
| Strategy Runner | Strategy name + mode | Config params | Run status, last result |
| Marketplace | Search + filters | Strategy cards grid | Sort, count, pagination |
| Positions | Total exposure | Data table | Market status, timestamps |
| Cost Analysis | Cost drag % (Doto 48px) | Segmented bars + stat rows | Date range, methodology |
| Risk Monitor | Risk gauge (arc) | Risk stat rows + matrix | Model params, thresholds |
| Dev Console | Code editor | Split panels | SDK version, test status |

---

## 17. Accessibility

- All text meets WCAG AA contrast ratios (verified per token pair)
- Focus indicators: `2px solid --interactive` outline, 2px offset
- Keyboard navigation: full sidebar and tab order
- Screen reader: `aria-live="polite"` for real-time value updates
- Reduced motion: respect `prefers-reduced-motion` — disable all transitions
- Data tables: proper `<th>` scope, `<caption>` elements
- Status colors never the only indicator — always paired with text/icon

---

## 18. Responsive Breakpoints

| Breakpoint | Width | Columns | Sidebar |
|-----------|-------|---------|---------|
| Desktop | ≥1024px | 12 | Visible, collapsible |
| Tablet | 768–1023px | 8 | Overlay, auto-collapsed |
| Mobile | <768px | 4 | Hidden, bottom nav bar |

---

*This design system is the source of truth for all Nexus Trade Engine frontend work. When in doubt, subtract. When adding, justify every pixel. The data is the design.*
