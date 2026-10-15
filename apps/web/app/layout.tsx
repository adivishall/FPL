import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { AuthGate, AuthProvider } from "@/components/auth";
import { ProductProvider } from "@/components/product";
import { Nav } from "@/components/ui";
import { productName } from "@/lib/product";

import "./globals.css";

// the product name is read from the environment on every request (no build-time baking)
export const dynamic = "force-dynamic";

export function generateMetadata(): Metadata {
  return {
    title: productName(),
    description: "Decision-first Fantasy Premier League planning: probabilistic forecasts, whole-squad plans compared with holding, evidence you can check.",
  };
}

export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#37003c" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <ProductProvider name={productName()}>
          <AuthProvider>
            <Nav />
            <main>
              <AuthGate>{children}</AuthGate>
            </main>
          </AuthProvider>
        </ProductProvider>
      </body>
    </html>
  );
}
