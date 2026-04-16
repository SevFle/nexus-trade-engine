# Nothing Design System — Platform Mapping

## 1. HTML / CSS / WEB

Load fonts via Google Fonts `<link>` or `@import`. Use CSS custom properties, `rem` for type, `px` for spacing/borders. Dark/light via `prefers-color-scheme` or class toggle.

```css
:root {
  --black: #000000;
  --surface: #111111;
  --surface-raised: #1A1A1A;
  --border: #222222;
  --border-visible: #333333;
  --text-disabled: #666666;
  --text-secondary: #999999;
  --text-primary: #E8E8E8;
  --text-display: #FFFFFF;
  --accent: #D71921;
  --accent-subtle: rgba(215,25,33,0.15);
  --success: #4A9E5C;
  --warning: #D4A843;
  --interactive: #5B9BF6;
  --space-xs: 4px;
  --space-sm: 8px;
  --space-md: 16px;
  --space-lg: 24px;
  --space-xl: 32px;
  --space-2xl: 48px;
  --space-3xl: 64px;
  --space-4xl: 96px;
}
```

---

## 2. REACT / TAILWIND CSS

Extend `tailwind.config.js` with design tokens as custom theme values. Use CSS custom properties for mode switching.

```js
// tailwind.config.js
module.exports = {
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        nx: {
          black: 'var(--black)',
          surface: 'var(--surface)',
          'surface-raised': 'var(--surface-raised)',
          border: 'var(--border)',
          'border-visible': 'var(--border-visible)',
          'text-disabled': 'var(--text-disabled)',
          'text-secondary': 'var(--text-secondary)',
          'text-primary': 'var(--text-primary)',
          'text-display': 'var(--text-display)',
          accent: 'var(--accent)',
          'accent-subtle': 'var(--accent-subtle)',
          success: 'var(--success)',
          warning: 'var(--warning)',
          interactive: 'var(--interactive)',
        }
      },
      fontFamily: {
        display: ['"Doto"', '"Space Mono"', 'monospace'],
        body: ['"Space Grotesk"', '"DM Sans"', 'system-ui', 'sans-serif'],
        mono: ['"Space Mono"', '"JetBrains Mono"', '"SF Mono"', 'monospace'],
      },
      fontSize: {
        'display-xl': ['72px', { lineHeight: '1.0', letterSpacing: '-0.03em' }],
        'display-lg': ['48px', { lineHeight: '1.05', letterSpacing: '-0.02em' }],
        'display-md': ['36px', { lineHeight: '1.1', letterSpacing: '-0.02em' }],
        heading: ['24px', { lineHeight: '1.2', letterSpacing: '-0.01em' }],
        subheading: ['18px', { lineHeight: '1.3', letterSpacing: '0' }],
        body: ['16px', { lineHeight: '1.5', letterSpacing: '0' }],
        'body-sm': ['14px', { lineHeight: '1.5', letterSpacing: '0.01em' }],
        caption: ['12px', { lineHeight: '1.4', letterSpacing: '0.04em' }],
        label: ['11px', { lineHeight: '1.2', letterSpacing: '0.08em' }],
      },
      spacing: {
        '2xs': '2px',
        'xs': '4px',
        'sm': '8px',
        'md': '16px',
        'lg': '24px',
        'xl': '32px',
        '2xl': '48px',
        '3xl': '64px',
        '4xl': '96px',
      }
    }
  }
}
```

### React Component Conventions

- Use `nx-` prefix for custom utility classes to avoid conflicts
- Mode toggle: `useTheme()` hook toggles `dark` class on `<html>`
- Data components: `StatRow`, `HeroMetric`, `SegmentedBar`, `Sparkline` as shared primitives
- Typography: `<Text variant="display-lg|heading|body|label">` component wrapping font+size+color tokens
- All status colors applied via `data-status="success|warning|error|neutral"` attribute + CSS selector

---

## 3. SWIFTUI / iOS

Register fonts in Info.plist, bundle `.ttf` files. Use `@Environment(\.colorScheme)` for mode switching.

```swift
extension Color {
    static let ndBlack = Color(hex: "000000")
    static let ndSurface = Color(hex: "111111")
    static let ndSurfaceRaised = Color(hex: "1A1A1A")
    static let ndBorder = Color(hex: "222222")
    static let ndBorderVisible = Color(hex: "333333")
    static let ndTextDisabled = Color(hex: "666666")
    static let ndTextSecondary = Color(hex: "999999")
    static let ndTextPrimary = Color(hex: "E8E8E8")
    static let ndTextDisplay = Color.white
    static let ndAccent = Color(hex: "D71921")
    static let ndSuccess = Color(hex: "4A9E5C")
    static let ndWarning = Color(hex: "D4A843")
    static let ndInteractive = Color(hex: "5B9BF6")
}
```

Light mode values in tokens.md Dark/Light table. Derive Font extension from font stack table.

---

## 4. PAPER (DESIGN TOOL)

Use `get_font_family_info` to verify fonts before writing styles. Direct hex values (no CSS variables). Dark mode as default canvas, light mode as separate artboard.
