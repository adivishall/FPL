// The product's display name: one setting, read by the server at request time
// (FPL_PRODUCT_NAME), so renaming the product needs no rebuild. "Copilot" elsewhere in the
// UI names the advisor inside the product (what it recommended), not the product.
export const DEFAULT_PRODUCT_NAME = "FPL Copilot";

export function productName(): string {
  const name = (process.env.FPL_PRODUCT_NAME ?? "").trim();
  return name || DEFAULT_PRODUCT_NAME;
}
