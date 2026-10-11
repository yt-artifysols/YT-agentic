import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { Shell } from "@/components/Shell";
import { ToastProvider } from "@/components/ui";
import { SessionProvider } from "@/lib/session";

export const metadata: Metadata = {
  title: "Control room",
  description: "Human approval for every video before it is published.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#4a56e2" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <ToastProvider>
          <SessionProvider>
            <Shell>{children}</Shell>
          </SessionProvider>
        </ToastProvider>
      </body>
    </html>
  );
}
