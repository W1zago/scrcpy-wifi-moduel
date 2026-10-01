---
name: CyberDeck Wireless Interface
colors:
  surface: '#131318'
  surface-dim: '#131318'
  surface-bright: '#39383e'
  surface-container-lowest: '#0e0e13'
  surface-container-low: '#1b1b20'
  surface-container: '#1f1f25'
  surface-container-high: '#2a292f'
  surface-container-highest: '#35343a'
  on-surface: '#e4e1e9'
  on-surface-variant: '#c7c4d7'
  inverse-surface: '#e4e1e9'
  inverse-on-surface: '#303036'
  outline: '#908fa0'
  outline-variant: '#464554'
  surface-tint: '#c0c1ff'
  primary: '#c0c1ff'
  on-primary: '#1000a9'
  primary-container: '#8083ff'
  on-primary-container: '#0d0096'
  inverse-primary: '#494bd6'
  secondary: '#d0bcff'
  on-secondary: '#3c0091'
  secondary-container: '#571bc1'
  on-secondary-container: '#c4abff'
  tertiary: '#4ae176'
  on-tertiary: '#003915'
  tertiary-container: '#00a74b'
  on-tertiary-container: '#003111'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#e1e0ff'
  primary-fixed-dim: '#c0c1ff'
  on-primary-fixed: '#07006c'
  on-primary-fixed-variant: '#2f2ebe'
  secondary-fixed: '#e9ddff'
  secondary-fixed-dim: '#d0bcff'
  on-secondary-fixed: '#23005c'
  on-secondary-fixed-variant: '#5516be'
  tertiary-fixed: '#6bff8f'
  tertiary-fixed-dim: '#4ae176'
  on-tertiary-fixed: '#002109'
  on-tertiary-fixed-variant: '#005321'
  background: '#131318'
  on-background: '#e4e1e9'
  surface-variant: '#35343a'
typography:
  headline-lg:
    fontFamily: Inter
    fontSize: 28px
    fontWeight: '700'
    lineHeight: 36px
    letterSpacing: -0.02em
  headline-md:
    fontFamily: Inter
    fontSize: 22px
    fontWeight: '600'
    lineHeight: 28px
    letterSpacing: -0.015em
  headline-sm:
    fontFamily: Inter
    fontSize: 18px
    fontWeight: '600'
    lineHeight: 24px
    letterSpacing: -0.01em
  body-lg:
    fontFamily: Inter
    fontSize: 15px
    fontWeight: '400'
    lineHeight: 22px
  body-md:
    fontFamily: Inter
    fontSize: 13px
    fontWeight: '400'
    lineHeight: 18px
  body-sm:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '400'
    lineHeight: 16px
  label-lg:
    fontFamily: JetBrains Mono
    fontSize: 13px
    fontWeight: '500'
    lineHeight: 18px
    letterSpacing: 0.02em
  label-md:
    fontFamily: JetBrains Mono
    fontSize: 11px
    fontWeight: '500'
    lineHeight: 16px
    letterSpacing: 0.04em
  label-sm:
    fontFamily: JetBrains Mono
    fontSize: 10px
    fontWeight: '600'
    lineHeight: 14px
    letterSpacing: 0.05em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  gutter: 1rem
  margin: 1.25rem
  space-xs: 0.25rem
  space-sm: 0.5rem
  space-md: 0.75rem
  space-lg: 1.25rem
  space-xl: 2rem
---

## Brand & Style

This design system establishes a high-performance, developer-first desktop aesthetic calibrated for low-latency hardware interaction and remote telemetry. Designed for a custom Electron/desktop context, the interface merges modern minimalism with subtle cyberpunk/futuristic engineering cues. It communicates speed, precision, and low-level control while feeling like a native, specialized hardware companion tool.

### Design Principles
- **Precision Telemetry:** Data density is balanced with breathing room. Metrics, ports, and addresses are presented with clinical clarity.
- **Atmospheric Glow & Depth:** High-contrast dark field with selective indigo-to-purple luminescent accents mimicking reactive terminal LED states.
- **Frameless Fluidity:** Borderless canvas leveraging custom window controls, unified headerbars, and floating micro-panels.
- **Tactile Response:** Every toggle, port switch, and device pairing trigger produces an immediate, visible change in state via glow diffusion and perimeter highlights.

## Colors

The color system relies on a deep void baseline (`#0a0a0f`) to maximize the perceived luminance of functional status indicators and neon-infused interactive elements without inducing eye strain.

### Palette Architecture
- **Surfaces:**
  - `bg-primary` (`#0a0a0f`): Core app canvas and base window background.
  - `bg-secondary` (`#12121a`): Sidebar panels, utility docks, and secondary workspaces.
  - `bg-elevated` (`#1a1a24`): Cards, popovers, context menus, and modal dialogs.
  - `border-subtle` (`rgba(255, 255, 255, 0.08)`): Perimeter containment lines.
  - `border-glow` (`rgba(99, 102, 241, 0.35)`): Highlighted state indicator for focused cards.
- **Brand & Accents:**
  - `accent-primary` (`#6366f1`): Active actions, primary CTAs, active connection rings.
  - `accent-secondary` (`#8b5cf6`): Secondary action states, telemetry spikes, sub-routines.
- **Functional Semantics:**
  - `success` (`#22c55e`): Device connected, ADB online, handshake verified.
  - `warning` (`#f59e0b`): High ping, dropped frames, degraded signal.
  - `error` (`#ef4444`): Device disconnected, port conflict, unauthorized RSA key.

## Typography

The type system pairs **Inter** for clean, legible structural UI navigation with **JetBrains Mono** for machine-level readouts, IP:Port addresses, buffer latency, and ADB console commands.

- Use **JetBrains Mono** exclusively for data that requires alignment: IP addresses, MAC addresses, bitrates, frame rates, memory footprints, and status pill counters.
- Use **Inter** for titles, instructions, setting descriptions, and general button copy.
- Enforce uppercase tracking on `label-sm` and `label-md` when used in micro-badges, hardware status pills, and column section headers.

## Layout & Spacing

This design system uses a flexible grid optimized for desktop viewports spanning 960px to 1920px, with standard dual-pane configurations (fixed left navigation/device-drawer + fluid right telemetry/mirror stage).

- **Grid & Gutters:** Base layout flows on an 8pt structural rhythm with sub-4pt positioning for micro-indicators and icon-label alignments. Gutters are locked to `1rem` (16px) to maximize screen real estate within an Electron window frame.
- **Frameless Window Bar:** The top bar requires an 8-column layout anchor with drag regions (`-webkit-app-region: drag`) across the empty margins, providing clean window control placements on the top right (or top left for macOS runtime).
- **Reflow Behavior:** When resized below 1100px width, the secondary device log and telemetry panels collapse into tabbed segments beneath the primary device display card.

## Elevation & Depth

Visual hierarchy is maintained through physical tonal tiering accented with luminescent glows rather than standard blurry black shadows.

1. **Base Layer (Level 0):** `#0a0a0f` — Flat, absorbing canvas.
2. **Structural Panels (Level 1):** `#12121a` with a 1px border of `rgba(255, 255, 255, 0.05)`.
3. **Floating Cards & Control Surfaces (Level 2):** `#1a1a24` with a 1px border of `rgba(255, 255, 255, 0.10)`.
4. **Active/Connected State (Glow Elevation):** Elevated surfaces housing an active session exhibit a dual-layered perimeter glow:
   - Outer glow: `box-shadow: 0 0 20px -3px rgba(99, 102, 241, 0.25), 0 0 8px -1px rgba(139, 92, 246, 0.3)`.
   - Border state: 1px solid `rgba(99, 102, 241, 0.6)`.
5. **Backdrop Blur:** Modal panels and floating drop-down drawers utilize `backdrop-filter: blur(16px)` over `rgba(18, 18, 26, 0.75)`.

## Shapes

The geometric framework balances soft humanized ergonomics with sleek, technical silhouettes.

- **Base Radius (0.5rem / 8px):** Applied to primary cards, action buttons, command inputs, and module cards.
- **Large Radius (1rem / 16px):** Outer window corners (if running framed mode) and large contextual modal panels.
- **Pill Radius (9999px):** Status badges, connection indicator rings, ADB trigger chips, and latency tags.

## Components

### Buttons
- **Primary Action (Start Stream / Pair):** Solid gradient fill from `#6366f1` to `#8b5cf6`, white text (`Inter` medium), subtle outer bloom on hover (`box-shadow: 0 0 14px rgba(99, 102, 241, 0.5)`). Transitions scale by 0.98 on active click.
- **Secondary (Disconnect / Config):** Translucent background `rgba(255, 255, 255, 0.04)`, 1px border `rgba(255, 255, 255, 0.1)`, hover turns border to `rgba(99, 102, 241, 0.4)` with text brightening to pure white.
- **Ghost/Icon Button:** Transparent background, icon in `rgba(255, 255, 255, 0.6)`, active state turns icon into accent color with a subtle circular halo.

### Futuristic Badge Pills
- Compact badges constructed with a full pill radius.
- Background uses semantic color at 10% opacity, bordered by semantic color at 30% opacity.
- Includes a 6px circular glowing LED dot before the label (e.g., green dot with pulse animation for `ONLINE`).
- Text is always rendered in `JetBrains Mono` (`label-sm`).

### Cards & Device Tiles
- Standard surface `#1a1a24` with 1px border `rgba(255, 255, 255, 0.08)`.
- Cards feature top-right quick-telemetry indicators (battery %, signal dBm, connection mode: `TCP/IP` vs `USB`).
- On mouse hover, the top border shifts to a dynamic gradient accent line (`linear-gradient(90deg, #6366f1, #8b5cf6)`).

### Input Fields & Address Bars
- Background `#12121a`, border `1px solid rgba(255, 255, 255, 0.1)`.
- Fixed height of 36px for rapid data entry.
- Text displays in `JetBrains Mono` with soft indigo selection color.
- Focus state activates an inner glow and border color `#6366f1`.

### Sliders & Micro-Toggles
- **Toggles:** Pill-shaped track (`#12121a`) with a 1px border. The thumb is an elevated circle with an active color shift to `#6366f1` and an accompanying ambient glow.
- **Sliders (Bitrate/Resolution scaling):** 4px line track with active progress colored in `#6366f1`, thumb features a 12px pill handle with monospace numerical hover tooltips.

### Telemetry Streamers / Terminal Logs
- Monolithic panel embedded at `#0a0a0f` with inset border `rgba(255, 255, 255, 0.04)`.
- Log entries color-code standard stdout (`rgba(255, 255, 255, 0.7)`), warnings (`#f59e0b`), and critical protocol errors (`#ef4444`).