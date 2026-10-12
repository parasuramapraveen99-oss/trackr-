"use client";

import { useState, type CSSProperties, type ReactNode } from "react";
import { Icon, type IconName } from "./Icon";

/**
 * Label + control + hint/error wrapper. Every form row uses this rhythm: 12px medium
 * label, 4px gap, control, 11px hint. An `error` replaces the hint rather than
 * stacking under it — two lines of guidance under one field is noise.
 */
export function Field({
  label,
  hint,
  error,
  htmlFor,
  children,
  style,
}: {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  htmlFor?: string;
  children: ReactNode;
  style?: CSSProperties;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, ...style }}>
      {label && (
        <label
          htmlFor={htmlFor}
          style={{
            font: "var(--type-body-sm)",
            fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
            color: "var(--text-secondary)",
          }}
        >
          {label}
        </label>
      )}
      {children}
      {(error || hint) && (
        <p style={{ margin: 0, font: "var(--type-label)", color: error ? "var(--negative)" : "var(--text-tertiary)" }}>
          {error || hint}
        </p>
      )}
    </div>
  );
}

const controlBase = (focus: boolean): CSSProperties => ({
  width: "100%",
  boxSizing: "border-box",
  padding: "8px 12px",
  borderRadius: "var(--radius-md)",
  border: "1px solid " + (focus ? "var(--border-focus)" : "var(--border)"),
  background: "var(--surface)",
  color: "var(--text-primary)",
  font: "var(--type-body)",
  outline: "none",
  transition: "border-color var(--duration-base) ease",
});

export function Input({
  value,
  onChange,
  placeholder,
  type = "text",
  icon,
  disabled,
  id,
  name,
  required,
  autoComplete,
  style,
}: {
  value: string;
  onChange?: (value: string) => void;
  placeholder?: string;
  type?: string;
  icon?: IconName | string;
  disabled?: boolean;
  id?: string;
  name?: string;
  required?: boolean;
  autoComplete?: string;
  style?: CSSProperties;
}) {
  const [focus, setFocus] = useState(false);
  const input = (
    <input
      id={id}
      name={name}
      type={type}
      value={value}
      required={required}
      disabled={disabled}
      placeholder={placeholder}
      autoComplete={autoComplete}
      onChange={(e) => onChange && onChange(e.target.value)}
      onFocus={() => setFocus(true)}
      onBlur={() => setFocus(false)}
      style={{
        ...controlBase(focus),
        paddingLeft: icon ? 34 : 12,
        opacity: disabled ? 0.6 : 1,
        ...style,
      }}
    />
  );
  if (!icon) return input;
  return (
    <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
      <span style={{ position: "absolute", left: 10, color: "var(--text-tertiary)", pointerEvents: "none" }}>
        <Icon name={icon} size={16} />
      </span>
      {input}
    </div>
  );
}

export function PasswordInput({
  value,
  onChange,
  id,
  name,
  required,
  autoComplete,
  style,
}: {
  value: string;
  onChange?: (value: string) => void;
  id?: string;
  name?: string;
  required?: boolean;
  autoComplete?: string;
  style?: CSSProperties;
}) {
  const [visible, setVisible] = useState(false);
  const [focus, setFocus] = useState(false);
  return (
    <div style={{ position: "relative", ...style }}>
      <input
        id={id}
        name={name}
        type={visible ? "text" : "password"}
        value={value}
        required={required}
        autoComplete={autoComplete}
        onChange={(e) => onChange && onChange(e.target.value)}
        onFocus={() => setFocus(true)}
        onBlur={() => setFocus(false)}
        style={{ ...controlBase(focus), paddingRight: 38 }}
      />
      <button
        type="button"
        tabIndex={-1}
        aria-label={visible ? "Hide password" : "Show password"}
        aria-pressed={visible}
        onClick={() => setVisible((v) => !v)}
        style={{
          position: "absolute",
          top: 0,
          bottom: 0,
          right: 0,
          display: "flex",
          alignItems: "center",
          padding: "0 12px",
          background: "none",
          border: "none",
          cursor: "pointer",
          color: "var(--text-tertiary)",
        }}
      >
        <Icon name={visible ? "eye-off" : "eye"} size={16} />
      </button>
    </div>
  );
}

export function Select({
  value,
  onChange,
  options,
  size = "md",
  id,
  style,
}: {
  value: string;
  onChange?: (value: string) => void;
  options: { value: string; label: ReactNode }[];
  size?: "sm" | "md";
  id?: string;
  style?: CSSProperties;
}) {
  const [focus, setFocus] = useState(false);
  const pad = size === "sm" ? "4px 26px 4px 8px" : "8px 30px 8px 12px";
  return (
    <div style={{ position: "relative", display: "inline-flex", alignItems: "center", ...style }}>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange && onChange(e.target.value)}
        onFocus={() => setFocus(true)}
        onBlur={() => setFocus(false)}
        style={{
          appearance: "none",
          WebkitAppearance: "none",
          padding: pad,
          borderRadius: "var(--radius-sm)",
          border: "1px solid " + (focus ? "var(--border-focus)" : "var(--border)"),
          background: "var(--surface)",
          color: "var(--text-primary)",
          fontFamily: "var(--font-body)",
          fontSize: size === "sm" ? "var(--text-2xs)" : "var(--text-xs)",
          outline: "none",
          cursor: "pointer",
          width: "100%",
        }}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {String(o.label)}
          </option>
        ))}
      </select>
      <span style={{ position: "absolute", right: 8, color: "var(--text-tertiary)", pointerEvents: "none" }}>
        <Icon name="chevron-down" size={14} />
      </span>
    </div>
  );
}

/**
 * A read-only value with a copy affordance — the Gmail forwarding address, mostly.
 * Rendered as `<code>` because the value is an identifier a human has to transcribe
 * accurately, and the mono face is what makes a typo visible.
 */
export function CopyField({ value, label, style }: { value: string; label?: string; style?: CSSProperties }) {
  const [copied, setCopied] = useState(false);
  function copy() {
    try {
      if (navigator.clipboard) navigator.clipboard.writeText(value);
    } catch {
      /* clipboard unavailable (insecure origin, denied permission) — the value is
         still selectable by hand, so this is a silent degrade, not an error state. */
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, ...style }}>
      <code
        style={{
          flex: 1,
          minWidth: 0,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          padding: "8px 12px",
          borderRadius: "var(--radius-md)",
          border: "1px solid var(--border)",
          background: "var(--surface-sunken)",
          color: "var(--text-primary)",
          font: "var(--type-mono)",
        }}
      >
        {value}
      </code>
      <button
        type="button"
        onClick={copy}
        aria-label={label ? "Copy " + label : "Copy"}
        style={{
          flexShrink: 0,
          padding: "8px 12px",
          borderRadius: "var(--radius-md)",
          border: "1px solid var(--border)",
          background: "var(--surface)",
          font: "var(--type-body-sm)",
          fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
          color: "var(--text-secondary)",
          cursor: "pointer",
        }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

export function Checkbox({
  checked,
  onChange,
  label,
  id,
  style,
}: {
  checked: boolean;
  onChange?: (checked: boolean) => void;
  label?: ReactNode;
  id?: string;
  style?: CSSProperties;
}) {
  return (
    <label
      htmlFor={id}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 8,
        cursor: "pointer",
        font: "var(--type-body)",
        color: "var(--text-secondary)",
        ...style,
      }}
    >
      <input
        id={id}
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange && onChange(e.target.checked)}
        style={{ accentColor: "var(--ink-900)", width: 15, height: 15, cursor: "pointer" }}
      />
      {label}
    </label>
  );
}
