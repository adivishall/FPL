"use client";

import { type ReactNode, createContext, useContext } from "react";

import { DEFAULT_PRODUCT_NAME } from "@/lib/product";

const ProductContext = createContext<string>(DEFAULT_PRODUCT_NAME);

/** Carries the server-read product name (lib/product.ts) to client components. */
export function ProductProvider({ name, children }: { name: string; children: ReactNode }) {
  return <ProductContext.Provider value={name}>{children}</ProductContext.Provider>;
}

export function useProductName(): string {
  return useContext(ProductContext);
}
