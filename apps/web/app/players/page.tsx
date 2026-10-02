"use client";

import { useState } from "react";

import { Card, DistBar, FreshnessBanner, PlayerName, State, useApi } from "@/components/ui";
import type { Freshness, PlayerRow } from "@/lib/api";
import { money, pct, pts } from "@/lib/format";

export default function Players() {
  const [position, setPosition] = useState("");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("xp");
  const qs = new URLSearchParams({ sort, limit: "60", ...(position ? { position } : {}), ...(q ? { q } : {}) });
  const res = useApi<{ players: PlayerRow[]; horizon: number; freshness: Freshness }>(`/players?${qs.toString()}`, [qs.toString()]);
  return (
    <>
      <FreshnessBanner f={res.data?.freshness} />
      <h1>Player Lab</h1>
      <div className="row" style={{ marginBottom: 10 }}>
        <select aria-label="position" value={position} onChange={(e) => setPosition(e.target.value)}>
          <option value="">All positions</option>
          {["GK", "DEF", "MID", "FWD"].map((p) => <option key={p}>{p}</option>)}
        </select>
        <input aria-label="search" placeholder="search name" value={q} onChange={(e) => setQ(e.target.value)} />
        <select aria-label="sort" value={sort} onChange={(e) => setSort(e.target.value)}>
          <option value="xp">Horizon xP</option>
          <option value="xp_next">Next GW xP</option>
          <option value="start">Start probability</option>
          <option value="price">Price</option>
        </select>
      </div>
      <Card testId="players">
        <State loading={res.loading} error={res.error} />
        <table>
          <thead><tr><th>Player</th><th>Team</th><th>Pos</th><th>Price</th><th>Next GW (p10–p90)</th><th>xP next</th><th>xP {res.data?.horizon ?? ""} GW</th><th>P(start)</th></tr></thead>
          <tbody>
            {res.data?.players.map((p) => (
              <tr key={p.player_code}>
                <td><PlayerName code={p.player_code} names={{ [String(p.player_code)]: p.name }} /></td>
                <td>{p.team}</td>
                <td>{p.position}</td>
                <td>{money(p.price)}</td>
                <td><DistBar p10={p.p10_next} p90={p.p90_next} mean={p.xp_next} max={16} /></td>
                <td>{pts(p.xp_next)}</td>
                <td>{pts(p.xp)}</td>
                <td>{pct(p.start_probability)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </>
  );
}
