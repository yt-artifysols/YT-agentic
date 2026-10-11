"use client";

import Link from "next/link";
import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Icon, type IconName } from "./icons";
import { STATE_LABEL, STATE_TONE, riskLabel, riskTone, type Tone } from "@/lib/format";

// ------------------------------------------------------------------ small pieces

export function Badge({ tone = "", icon, children, title }: { tone?: Tone; icon?: IconName; children: ReactNode; title?: string }) {
  return (
    <span className={`badge ${tone}`} title={title}>
      {icon && <Icon name={icon} size={13} />}
      {children}
    </span>
  );
}

export function StateBadge({ state }: { state: string }) {
  return <Badge tone={STATE_TONE[state] ?? ""}>{STATE_LABEL[state] ?? state}</Badge>;
}

export function RiskBadge({ risk }: { risk: number | null }) {
  return (
    <Badge tone={riskTone(risk)} icon={risk !== null && risk >= 60 ? "alert" : risk === null ? "alert" : "check"}>
      {risk === null ? riskLabel(risk) : `${riskLabel(risk)} · ${risk}`}
    </Badge>
  );
}

export function Button({
  variant = "",
  size = "",
  icon,
  busy,
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "" | "primary" | "success" | "danger" | "ghost"; size?: "" | "small"; icon?: IconName; busy?: boolean }) {
  return (
    <button {...rest} className={`btn ${variant} ${size} ${rest.className ?? ""}`} disabled={rest.disabled || busy}>
      {icon && <Icon name={icon} size={size === "small" ? 15 : 18} />}
      {busy ? "Working..." : children}
    </button>
  );
}

export function LinkButton({ href, children, variant = "", icon }: { href: string; children: ReactNode; variant?: "" | "primary"; icon?: IconName }) {
  return (
    <Link href={href} className={`btn ${variant}`}>
      {icon && <Icon name={icon} size={18} />}
      {children}
    </Link>
  );
}

export function Card({ title, action, children, pad = true, className = "" }: { title?: ReactNode; action?: ReactNode; children: ReactNode; pad?: boolean; className?: string }) {
  return (
    <section className={`card ${className}`}>
      {(title || action) && (
        <div className="card-head">
          <h2>{title}</h2>
          {action}
        </div>
      )}
      <div className={pad ? "card-pad" : ""}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, hint, icon, href, tone = "" }: { label: string; value: ReactNode; hint?: ReactNode; icon?: IconName; href?: string; tone?: "" | "attention" | "danger" }) {
  const body = (
    <>
      <div className="label">
        {icon && <Icon name={icon} size={15} />}
        {label}
      </div>
      <div className="value">{value}</div>
      {hint && <div className="hint">{hint}</div>}
    </>
  );
  return href ? (
    <Link href={href} className={`card stat ${tone}`}>
      {body}
    </Link>
  ) : (
    <div className={`card stat ${tone}`}>{body}</div>
  );
}

export function PageHead({ title, sub, actions }: { title: string; sub?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {sub && <div className="sub">{sub}</div>}
      </div>
      {actions && <div className="row">{actions}</div>}
    </div>
  );
}

export function Empty({ icon = "check", title, children, action }: { icon?: IconName; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <div className="big"><Icon name={icon} size={34} /></div>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export function Skeleton({ h = 18, w = "100%" }: { h?: number; w?: number | string }) {
  return <div className="skeleton" style={{ height: h, width: w }} />;
}

export function SkeletonList({ rows = 4 }: { rows?: number }) {
  return (
    <div className="list" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="stack-sm">
          <Skeleton w="55%" />
          <Skeleton h={13} w="30%" />
        </div>
      ))}
    </div>
  );
}

export function Notice({ tone = "info", children, icon }: { tone?: "info" | "ok" | "warn" | "err"; children: ReactNode; icon?: IconName }) {
  return (
    <div className={`notice ${tone}`} role={tone === "err" ? "alert" : "status"}>
      <Icon name={icon ?? (tone === "err" || tone === "warn" ? "alert" : tone === "ok" ? "check" : "info")} size={18} />
      <div>{children}</div>
    </div>
  );
}

export function Progress({ value, tone = "" }: { value: number; tone?: "" | "warn" | "bad" }) {
  return (
    <div className={`progress ${tone}`} role="progressbar" aria-valuenow={Math.round(value)} aria-valuemin={0} aria-valuemax={100}>
      <i style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
    </div>
  );
}

export function Gauge({ value, label }: { value: number; label: string }) {
  const color = value >= 60 ? "var(--bad)" : value >= 25 ? "var(--warn)" : "var(--ok)";
  return (
    <div className="gauge" style={{ ["--pct" as string]: value, ["--gauge" as string]: color }} role="img" aria-label={label}>
      <b>{value}</b>
    </div>
  );
}

export function Segmented<T extends string>({ value, onChange, options, label }: { value: T; onChange: (v: T) => void; options: { value: T; label: ReactNode }[]; label: string }) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} type="button" aria-pressed={o.value === value} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Tabs<T extends string>({ value, onChange, tabs }: { value: T; onChange: (v: T) => void; tabs: { value: T; label: ReactNode }[] }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t.value} role="tab" aria-selected={t.value === value} onClick={() => onChange(t.value)}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Field({ label, children, hint }: { label: string; children: (id: string) => ReactNode; hint?: string }) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children(id)}
      {hint && <span className="faint small" style={{ fontWeight: 500 }}>{hint}</span>}
    </div>
  );
}

// ------------------------------------------------------------------ modal & toasts

export function Modal({ title, onClose, children, footer }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      prev?.focus?.();
    };
  }, [onClose]);
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} ref={ref}>
        <div className="row between nowrap">
          <h2>{title}</h2>
          <Button variant="ghost" size="small" icon="x" onClick={onClose} aria-label="Close" />
        </div>
        {children}
        {footer && <div className="row" style={{ justifyContent: "flex-end" }}>{footer}</div>}
      </div>
    </div>
  );
}

interface Toast {
  id: number;
  kind: "" | "ok" | "err";
  text: string;
}
const ToastCtx = createContext<(kind: Toast["kind"], text: string) => void>(() => undefined);
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((kind: Toast["kind"], text: string) => {
    const id = Date.now() + Math.random();
    setItems((cur) => [...cur.slice(-2), { id, kind, text }]);
    setTimeout(() => setItems((cur) => cur.filter((t) => t.id !== id)), kind === "err" ? 7000 : 4000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.kind}`} role="status">
            {t.text}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

// A confirm step for actions with consequences.
export function useConfirm() {
  const [state, setState] = useState<{ title: string; body: ReactNode; confirm: string; danger: boolean; resolve: (v: boolean) => void } | null>(null);
  const ask = useCallback(
    (title: string, body: ReactNode, opts: { confirm?: string; danger?: boolean } = {}) =>
      new Promise<boolean>((resolve) => setState({ title, body, confirm: opts.confirm ?? "Confirm", danger: opts.danger ?? false, resolve })),
    [],
  );
  const dialog = state && (
    <Modal
      title={state.title}
      onClose={() => {
        state.resolve(false);
        setState(null);
      }}
      footer={
        <>
          <Button onClick={() => { state.resolve(false); setState(null); }}>Cancel</Button>
          <Button variant={state.danger ? "danger" : "primary"} className={state.danger ? "solid" : ""} onClick={() => { state.resolve(true); setState(null); }}>
            {state.confirm}
          </Button>
        </>
      }
    >
      <div className="muted">{state.body}</div>
    </Modal>
  );
  return { ask, dialog };
}
