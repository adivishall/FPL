import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { AuthGate, AuthProvider } from "@/components/auth";
import { Nav } from "@/components/ui";

import "./globals.css";

export const metadata: Metadata = {
  title: "FPL Copilot",
  description: "Decision-first Fantasy Premier League copilot: probabilistic forecasts, whole-squad planning, evidence you can check.",
};

export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#37003c" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <AuthProvider>
          <Nav />
          <main>
            <AuthGate>{children}</AuthGate>
          </main>
        </AuthProvider>
      </body>
    </html>
  );
}
