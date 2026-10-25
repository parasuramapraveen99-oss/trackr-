/**
 * `@trackr/ui` -- the design-system primitives BOTH applications draw portfolio screens
 * with. One import surface.
 *
 * Everything here is styled with inline CSS-VARIABLE values rather than Tailwind classes,
 * which is what makes it shareable: a component's appearance is fully described where it
 * is defined, and it re-themes by swapping the variables. See ./tokens.ts for why the
 * exact values live in objects instead of on Tailwind's scale.
 *
 * THE TOKEN CONTRACT IS THE INTERFACE. These components read semantic names -- `--surface`,
 * `--border`, `--text-primary`, `--ledger-ink`, `--positive`, `--negative`, the `--chart-*`
 * and `--alloc-*` ramps, `--type-*`, `--font-*`, `--radius-*`, `--gap-*` -- and each APP
 * decides what they mean. Trackr+ defines them in its own `globals.css`; Trackr Workspace
 * maps them onto its `--ws-*` palette. So the same component is one implementation with two
 * appearances, which is exactly what `docs/PRODUCT_ARCHITECTURE.md` section 3 asks for:
 * identical figures, app-owned layout and typography.
 *
 * WHAT MUST NEVER BE IN HERE: an app's shell, route tree, navigation, marketing, theme
 * bootstrap or an unscoped class from one app's stylesheet. Those are listed as separate
 * per-app implementations in the extraction map, and a package that reached for one would
 * make the two products impossible to keep apart.
 *
 * This package must never import from either application.
 */
export { Icon, ICON_PATHS, type IconName } from "./Icon";
export { Button, IconButton, LinkAction } from "./Button";
export { Card, EmptyState } from "./Card";
export { Badge, type BadgeTone } from "./Badge";
export { Banner } from "./Banner";
export { SegmentedControl, TabBar, type SegmentOption, type TabItem } from "./SegmentedControl";
export {
  LedgerGrid,
  LedgerSecurity,
  LedgerSigned,
  LedgerTag,
  type LedgerColumn,
  type LedgerRow,
} from "./LedgerGrid";
export { DataTable, type DataColumn } from "./DataTable";
export { Modal } from "./Modal";
export { Field, Input, PasswordInput, Select, CopyField, Checkbox } from "./forms";
export { ChartLegend, DonutChart, PieChart, type Slice } from "./charts";
export { BubbleMenu, SlashMenu, type BubbleItem, type SlashGroup, type SlashItem } from "./editor";
export { VintageKpi, VintageXirr, type XirrWindow } from "./VintageKpi";
export { AllocationDonut, donutColor, type DonutSlice } from "./AllocationDonut";
export { VintageLinePlot, type PlotSeries } from "./VintageLinePlot";
export {
  BarSeries,
  CORRELATION_BANDS,
  DistributionBar,
  Gauge,
  HeatMap,
  MeterRow,
  ScatterPlot,
  type BubblePoint,
  type HeatBand,
} from "./analytics-charts";
export {
  AllocationDonutSkeleton,
  LedgerGridSkeleton,
  LinePlotSkeleton,
  Skeleton,
  VintageKpiSkeleton,
} from "./Skeleton";
export {
  ALLOC_RAMP,
  CHART_COLORS,
  CHART_MONO,
  cardTitle,
  eyebrow,
  ledgerFig,
  ledgerHead,
  slipFootLabel,
  slipFootRow,
  slipFootValue,
  slipLabel,
  slipNum,
  slipRule,
  slipShell,
} from "./tokens";
