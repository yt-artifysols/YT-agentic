"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";
import { forgetCsrf, api } from "@/lib/api";
import { DEMO, demoRole } from "@/lib/demo";
import { initials } from "@/lib/format";
import { isStaff, useSession } from "@/lib/session";
import { Icon, type IconName } from "./icons";
import { Modal, useToast } from "./ui";

interface NavItem {
  href: string;
  label: string;
  icon: IconName;
  badge?: number;
  staff?: boolean;
  admin?: boolean;
}

export function Shell({ children }: { children: ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const { me, overview } = useSession();
  const toast = useToast();
  const [more, setMore] = useState(false);

  if (path.startsWith("/login")) return <>{children}</>;

  const awaiting = overview?.awaiting_approval.count ?? 0;
  const alerts = overview?.alerts_24h ?? 0;
  const groups: { title: string; items: NavItem[] }[] = [
    {
      title: "Work",
      items: [
        { href: "/", label: "Today", icon: "home" },
        { href: "/review", label: "Review", icon: "inbox", badge: awaiting },
        { href: "/pipeline", label: "Pipeline", icon: "film" },
        { href: "/channels", label: "Channels", icon: "tv" },
        { href: "/inputs", label: "Ideas & inputs", icon: "idea" },
        { href: "/knowledge", label: "Brand & policy", icon: "book" },
      ],
    },
    {
      title: "Insight",
      items: [
        { href: "/alerts", label: "Alerts", icon: "bell", badge: alerts },
        { href: "/scorecards", label: "Scorecards", icon: "chart" },
        { href: "/tiers", label: "Review tiers", icon: "shield" },
        { href: "/clusters", label: "Clusters", icon: "layers" },
      ],
    },
    {
      title: "Safety & admin",
      items: [
        { href: "/controls", label: "Kill switches", icon: "power", staff: true },
        { href: "/admin", label: "People", icon: "users", admin: true },
      ],
    },
  ];
  const visible = groups
    .map((g) => ({ ...g, items: g.items.filter((i) => (!i.staff || isStaff(me)) && (!i.admin || me?.role === "admin")) }))
    .filter((g) => g.items.length > 0);
  const active = (href: string) => (href === "/" ? path === "/" : path === href || path.startsWith(href + "/") || (href === "/review" && path.startsWith("/jobs/")));
  const flat = visible.flatMap((g) => g.items);
  const title = flat.find((i) => active(i.href))?.label ?? "Console";

  const logout = async () => {
    try {
      await api.logout();
    } catch {
      /* the session may already be gone */
    }
    forgetCsrf();
    toast("", "Signed out");
    router.replace("/login");
  };

  const link = (i: NavItem) => (
    <Link key={i.href} href={i.href} className="nav-item" aria-current={active(i.href) ? "page" : undefined}>
      <Icon name={i.icon} />
      {i.label}
      {i.badge ? <span className={`count ${i.href === "/alerts" ? "warn" : ""}`}>{i.badge}</span> : null}
    </Link>
  );

  const mobile: NavItem[] = [
    { href: "/", label: "Today", icon: "home" },
    { href: "/review", label: "Review", icon: "inbox", badge: awaiting },
    { href: "/channels", label: "Channels", icon: "tv" },
    { href: "/alerts", label: "Alerts", icon: "bell", badge: alerts },
  ];

  return (
    <div className="shell">
      <aside className="sidebar" aria-label="Main">
        <div className="brand-row">
          <div className="logo">YT</div>
          <div>
            <div className="brand-name">Control room</div>
            <div className="brand-sub">Every video needs your approval</div>
          </div>
        </div>
        {visible.map((g) => (
          <div key={g.title}>
            <div className="nav-group">{g.title}</div>
            {g.items.map(link)}
          </div>
        ))}
      </aside>
      <div className="main-col">
        {DEMO && (
          <div className="banner demo" role="status">
            <Icon name="eye" size={18} />
            Demo with sample data, nothing is real. View as:{" "}
            {["approver", "operator", "admin"].map((r) => (
              <a key={r} href={`?role=${r}`} style={{ fontWeight: demoRole() === r ? 800 : 500, textDecoration: demoRole() === r ? "underline" : "none", marginLeft: 6 }}>{r}</a>
            ))}
          </div>
        )}
        {overview?.kill_switch.global_paused && (
          <div className="banner" role="alert">
            <Icon name="power" size={18} />
            Publishing is paused for everything (global kill switch).
            {isStaff(me) && <Link href="/controls">Open kill switches</Link>}
          </div>
        )}
        <header className="topbar">
          <span className="page-title">{title}</span>
          <span className="spacer" />
          {me && (
            <div className="userchip">
              <span className="avatar" aria-hidden="true">{initials(me.display_name)}</span>
              <span className="muted" data-testid="whoami" style={{ display: "none" }}>
                {me.display_name} · {me.role}
              </span>
              <span className="muted small" style={{ whiteSpace: "nowrap" }}>
                {me.display_name} · {me.role}
              </span>
              <button className="btn ghost small" onClick={() => void logout()} aria-label="Sign out">
                <Icon name="logout" size={16} />
              </button>
            </div>
          )}
        </header>
        <main className="page">{children}</main>
      </div>
      <nav className="bottomnav" aria-label="Main">
        {mobile.map((i) => (
          <Link key={i.href} href={i.href} aria-current={active(i.href) ? "page" : undefined}>
            <Icon name={i.icon} />
            {i.label}
            {i.badge ? <span className={`count dot-count ${i.href === "/alerts" ? "warn" : ""}`} style={{ position: "absolute", top: 2, right: "calc(50% - 26px)" }}>{i.badge}</span> : null}
          </Link>
        ))}
        <button onClick={() => setMore(true)} aria-label="More">
          <Icon name="more" />
          More
        </button>
      </nav>
      {more && (
        <Modal title="More" onClose={() => setMore(false)}>
          <div className="stack-sm" onClick={() => setMore(false)}>
            {flat
              .filter((i) => !mobile.some((m) => m.href === i.href))
              .map(link)}
            <button className="nav-item" style={{ border: 0, background: "none", textAlign: "left", cursor: "pointer" }} onClick={() => void logout()}>
              <Icon name="logout" />
              Sign out
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
