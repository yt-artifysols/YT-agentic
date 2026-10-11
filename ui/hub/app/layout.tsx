import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { TopBar } from "./TopBar";

export const metadata: Metadata = {
  title: "Approval hub",
  description: "Human approval for every video before it is published.",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <TopBar />
        <main>{children}</main>
      </body>
    </html>
  );
}
