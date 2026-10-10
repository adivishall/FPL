import type { Metadata } from "next";
import type { ReactNode } from "react";

import { AuthGate, AuthProvider } from "@/components/auth";
import { Nav } from "@/components/ui";

import "./globals.css";

export const metadata: Metadata = {
  title: "FPL Decision Engine",
  description: "Decision-first Fantasy Premier League engine: probabilistic forecasts, MILP planning, evidence.",
};

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
