"use client";

import Link from "next/link";
import { useState, type CSSProperties, type ReactNode } from "react";
import { Icon, type IconName } from "./Icon";

type Variant = "primary" | "secondary" | "ghost" | "plain" | "danger";
type Size = "sm" | "md" | "lg";

const VARIANTS: Record<Variant, { background: string; color: string; borderColor: string }> = {
  primary: { background: "var(--ink-900)", color: "var(--text-inverse)", borderColor: "var(--ink-900)" },
  secondary: { background: "var(--surface)", color: "var(--text-primary)", borderColor: "var(--border)" },
  ghost: { background: "transparent", color: "var(--text-secondary)", borderColor: "transparent" },
  plain: { background: "transparent", color: "var(--text-secondary)", borderColor: "transparent" },
  danger: { background: "var(--negative)", color: "#fff", borderColor: "var(--negative)" },
};

const SIZES: Record<Size, CSSProperties> = {
  sm: { padding: "6px 12px", fontSize: "var(--text-xs)" },
  md: { padding: "8px 16px", fontSize: "var(--text-sm)" },
  lg: { padding: "10px 20px", fontSize: "var(--text-sm)" },
};

interface ButtonProps {
  children?: ReactNode;
  variant?: Variant;
  size?: Size;
  icon?: IconName | string;
  iconRight?: IconName | string;
  disabled?: boolean;
  fullWidth?: boolean;
  onClick?: () => void;
  type?: "button" | "submit" | "reset";
  title?: string;
  style?: CSSProperties;
}

/**
 * Hover and press are React state rather than CSS pseudo-classes because the whole
 * component is styled inline — there is no class to hang `:hover` off. The press
 * state is the system's one microinteraction: a 120ms scale to 0.97.
 */
export function Button({
  children,
  variant = "primary",
  size = "md",
  icon,
  iconRight,
  disabled = false,
  fullWidth = false,
  onClick,
  type = "button",
  title,
  style,
}: ButtonProps) {
  const [hover, setHover] = useState(false);
  const [press, setPress] = useState(false);
  const v = VARIANTS[variant] ?? VARIANTS.primary;
  const hoverBg =
    variant === "primary" ? "var(--ink-700)" : variant === "danger" ? "var(--negative)" : "var(--surface-hover)";

  return (
    <button
      type={type}
      title={title}
      disabled={disabled}
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => {
        setHover(false);
        setPress(false);
      }}
      onMouseDown={() => setPress(true)}
      onMouseUp={() => setPress(false)}
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 8,
        width: fullWidth ? "100%" : "auto",
        fontFamily: "var(--font-body)",
        fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
        borderRadius: "var(--radius-md)",
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.5 : 1,
        borderStyle: "solid",
        borderWidth: 1,
        transition:
          "background-color var(--duration-base) ease, transform var(--duration-fast) ease, border-color var(--duration-base) ease",
        transform: press && !disabled ? "scale(var(--press-scale))" : "none",
        ...SIZES[size],
        ...v,
        background: hover && !disabled ? hoverBg : v.background,
        borderColor: hover && !disabled && variant === "secondary" ? "var(--border-strong)" : v.borderColor,
        ...style,
      }}
    >
      {icon ? <Icon name={icon} size={size === "sm" ? 14 : 16} /> : null}
      {children}
      {iconRight ? <Icon name={iconRight} size={size === "sm" ? 14 : 16} /> : null}
    </button>
  );
}

interface IconButtonProps {
  name: IconName | string;
  label: string;
  size?: number;
  variant?: "outline" | "plain";
  onClick?: () => void;
  active?: boolean;
  style?: CSSProperties;
}

export function IconButton({
  name,
  label,
  size = 36,
  variant = "outline",
  onClick,
  active = false,
  style,
}: IconButtonProps) {
  const [hover, setHover] = useState(false);
  const [press, setPress] = useState(false);
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => {
        setHover(false);
        setPress(false);
      }}
      onMouseDown={() => setPress(true)}
      onMouseUp={() => setPress(false)}
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        height: size,
        width: size,
        borderRadius: "var(--radius-md)",
        border: variant === "outline" ? "1px solid var(--border)" : "1px solid transparent",
        background: active
          ? "var(--surface-active)"
          : hover
            ? "var(--surface-hover)"
            : variant === "outline"
              ? "var(--surface)"
              : "transparent",
        color: active || hover ? "var(--text-primary)" : "var(--text-secondary)",
        cursor: "pointer",
        transition:
          "background-color var(--duration-base) ease, color var(--duration-base) ease, transform var(--duration-fast) ease",
        transform: press ? "scale(var(--press-scale))" : "none",
        ...style,
      }}
    >
      <Icon name={name} size={18} />
    </button>
  );
}


/**
 * A navigation action in a card header — "View all" on the holdings panels.
 *
 * A `<Link>` rather than a `<Button>` with an onClick, because it goes somewhere: it must
 * be middle-clickable, openable in a new tab, and readable in the status bar. It borrows
 * the secondary button's silhouette so it still reads as the header's action, with the
 * card's own square corner rather than a pill.
 */
export function LinkAction({
  href,
  children,
  iconRight = "chevron-right",
}: {
  href: string;
  children: ReactNode;
  iconRight?: IconName | string;
}) {
  const [hover, setHover] = useState(false);
  return (
    <Link
      href={href}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        flexShrink: 0,
        padding: "5px 10px",
        border: "1px solid var(--border)",
        borderRadius: 2,
        background: hover ? "var(--surface-hover)" : "transparent",
        fontFamily: "var(--font-label)",
        fontSize: 9.5,
        fontWeight: 600,
        letterSpacing: ".12em",
        textTransform: "uppercase",
        color: "var(--ledger-ink-soft)",
        textDecoration: "none",
        whiteSpace: "nowrap",
        transition: "background-color var(--duration-fast) ease",
      }}
    >
      {children}
      {iconRight && <Icon name={iconRight} size={12} />}
    </Link>
  );
}
