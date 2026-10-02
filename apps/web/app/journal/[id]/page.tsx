"use client";

import { use } from "react";

import { Card, State, useApi } from "@/components/ui";
import type { Recommendation } from "@/lib/api";

export default function JournalEntry({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const rec = useApi<Recommendation>(`/recommendations/${id}`);
  return (
    <>
      <h1>Recommendation {id}</h1>
      <State loading={rec.loading} error={rec.error} />
      {rec.data ? (
        <Card title="Explanation (rendered from stored evidence; immutable record)">
          <pre className="md">{rec.data.markdown}</pre>
        </Card>
      ) : null}
    </>
  );
}
