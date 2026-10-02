"use client";

import { PlayerName } from "@/components/ui";

export interface PitchPlayer {
  code: number;
  position: string;
  xp?: number;
}

/** Starting XI by line (GK → FWD) and the ordered bench; captain/vice marked. */
export function Pitch(props: {
  starters: PitchPlayer[];
  bench: PitchPlayer[];
  captain: number;
  vice: number;
  names?: Record<string, string | null>;
}) {
  const lines = ["GK", "DEF", "MID", "FWD"].map((p) => props.starters.filter((s) => s.position === p));
  const tag = (c: number) => (c === props.captain ? " (C)" : c === props.vice ? " (V)" : "");
  return (
    <div className="pitch" data-testid="pitch">
      {lines.map((line, i) => (
        <div className="line" key={i}>
          {line.map((p) => (
            <div className="shirt" key={p.code}>
              <PlayerName code={p.code} names={props.names} />
              {tag(p.code)}
              {p.xp !== undefined ? <div className="small">{p.xp.toFixed(1)} xP</div> : null}
            </div>
          ))}
        </div>
      ))}
      <div className="bench">
        Bench:{" "}
        {props.bench.map((p, i) => (
          <span key={p.code}>
            {i + 1}. <PlayerName code={p.code} names={props.names} />{" "}
          </span>
        ))}
      </div>
    </div>
  );
}
