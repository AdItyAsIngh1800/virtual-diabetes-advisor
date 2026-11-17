/**
 * The patient's glucose chart (R6), drawn with Recharts from the tested chart model
 * (lib/chart.ts): personal limits as step lines (dashed before the first stored
 * limit), the shaded and hatched pre-breach band under each, readings as shapes per
 * context (circle fasting, triangle post-meal, diamond random) with breaches ringed,
 * a legend that names every mark, and a hover/tap tooltip. The readings table under
 * it is the non-visual fallback.
 */
import { useEffect, useState } from "react";
import {
  ComposedChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { chartModel, type ChartPoint } from "@/practitioner/lib/chart";
import type { S } from "@/shared/api/client";
import { mgdl, shortWhen } from "@/shared/lib/format";
import { CONTEXT_LABELS, bandLabel, type LimitKind } from "@/shared/lib/labels";

type Context = ChartPoint["context"];

// Categorical slots 1-3 of the validated chart palette (index.css), fixed order.
const SERIES: Record<Context, { color: string; label: string }> = {
  fasting: { color: "var(--s-fasting)", label: "Fasting reading" },
  post_meal: { color: "var(--s-post)", label: "Post-meal reading" },
  random: { color: "var(--s-random)", label: "Random reading" },
};
const LIMIT_STYLE: Record<LimitKind, { color: string; label: string }> = {
  fasting_high: { color: "var(--s-fasting)", label: "Fasting limit" },
  post_meal_high: { color: "var(--s-post)", label: "Post-meal limit" },
  low: { color: "var(--foreground)", label: "Low limit" },
};
const CONTEXTS: Context[] = ["fasting", "post_meal", "random"];

/** One reading marker (shape carries the context, so colour is not the only signal). */
function Marker({ context, cx, cy, r = 4.5 }: { context: Context; cx: number; cy: number; r?: number }) {
  const common = { fill: SERIES[context].color, stroke: "var(--card)", strokeWidth: 1.5 };
  if (context === "post_meal") {
    const h = r * 1.25;
    return <path d={`M${cx} ${cy - h}L${cx + h} ${cy + h * 0.8}L${cx - h} ${cy + h * 0.8}Z`} {...common} />;
  }
  if (context === "random") {
    const h = r * 1.2;
    return <path d={`M${cx} ${cy - h}L${cx + h} ${cy}L${cx} ${cy + h}L${cx - h} ${cy}Z`} {...common} />;
  }
  return <circle cx={cx} cy={cy} r={r} {...common} />;
}

function PointShape(props: { cx?: number; cy?: number; payload?: ChartPoint }) {
  const { cx, cy, payload } = props;
  if (cx === undefined || cy === undefined || !payload) return <g />;
  return (
    <g>
      <Marker context={payload.context} cx={cx} cy={cy} />
      {payload.breach && <circle cx={cx} cy={cy} r={8.5} fill="none" stroke="var(--chart-critical)" strokeWidth={2} />}
    </g>
  );
}

function Tip({ active, payload }: { active?: boolean; payload?: readonly { payload?: unknown }[] }) {
  const p = active ? (payload?.[0]?.payload as ChartPoint | undefined) : undefined;
  if (!p) return null;
  return (
    <div className="rounded-xl border bg-popover px-3 py-2 text-sm text-popover-foreground shadow-lg">
      <p className="font-semibold tabular">{mgdl(p.value)} mg/dL</p>
      <p className="text-muted-foreground">
        {CONTEXT_LABELS[p.context]}, {shortWhen(p.measuredAt)}
      </p>
      <p>{bandLabel(p.state)}</p>
    </div>
  );
}

/** Width of the chart's box, to pick label detail and tick count. */
function useWide(): boolean {
  const query = "(min-width: 640px)";
  const [wide, setWide] = useState(() => (typeof window === "undefined" ? true : window.matchMedia(query).matches));
  useEffect(() => {
    const m = window.matchMedia(query);
    const on = () => setWide(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  return wide;
}

function Swatch({ children }: { children: React.ReactNode }) {
  return (
    <svg width={22} height={14} viewBox="0 0 22 14" aria-hidden className="shrink-0">
      {children}
    </svg>
  );
}

export function GlucoseChart({ detail }: { detail: S["PatientDetail"] }) {
  const wide = useWide();
  const m = chartModel(detail, wide ? 8 : 4);
  const contexts = CONTEXTS.filter((c) => m.points.some((p) => p.context === c));
  const bandPct = Math.round(m.band * 100);
  const day = (t: number) => new Date(t).toLocaleDateString("en-GB", { day: "numeric", month: "short" });

  if (m.points.length === 0 && m.limits.length === 0) {
    return <p className="rounded-2xl border border-dashed bg-card p-5 text-muted-foreground">No readings or limits in this period.</p>;
  }

  return (
    <figure className="rounded-2xl border bg-card p-3 sm:p-4">
      {/* Hatch for the pre-breach band: texture as well as colour (CVD, print). */}
      <svg width="0" height="0" aria-hidden className="absolute">
        <defs>
          <pattern id="band-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="6" height="6" fill="var(--band)" fillOpacity={0.22} />
            <line x1="0" y1="0" x2="0" y2="6" stroke="var(--band)" strokeWidth={1.5} strokeOpacity={0.75} />
          </pattern>
        </defs>
      </svg>
      <div
        role="img"
        aria-label={`Glucose readings and personal limits, ${m.points.length} readings from ${day(m.t0)} to ${day(m.t1)}. The table below lists every reading.`}
        className="h-[260px] w-full sm:h-[320px] [&_.recharts-cartesian-axis-tick_text]:fill-[var(--axis)] [&_.recharts-text]:text-xs"
      >
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart margin={{ top: 8, right: wide ? 96 : 36, bottom: 4, left: 0 }} accessibilityLayer={false}>
            <XAxis
              dataKey="t"
              type="number"
              domain={[m.t0, m.t1]}
              ticks={m.xTicks}
              tickFormatter={day}
              stroke="var(--axis)"
              tickLine={{ stroke: "var(--axis)" }}
              allowDataOverflow
            />
            <YAxis
              dataKey="value"
              type="number"
              domain={m.domain}
              ticks={m.yTicks}
              width={44}
              stroke="var(--axis)"
              axisLine={false}
              tickLine={false}
              label={{ value: "mg/dL", position: "insideTopLeft", offset: 0, dy: -4, fill: "var(--axis)", fontSize: 12 }}
              allowDataOverflow
            />
            {m.yTicks.map((v) => (
              <ReferenceLine key={`grid-${v}`} y={v} stroke="var(--grid)" strokeWidth={1} ifOverflow="hidden" />
            ))}

            {m.limits.flatMap((l) =>
              l.segments.map((s, i) => (
                <ReferenceArea key={`band-${l.kind}-${i}`} x1={s.from} x2={s.to} y1={s.bandLo} y2={s.bandHi} fill="url(#band-hatch)" fillOpacity={1} stroke="none" ifOverflow="hidden" />
              )),
            )}

            {m.limits.flatMap((l) => {
              const style = LIMIT_STYLE[l.kind];
              const last = l.segments[l.segments.length - 1];
              return l.segments.flatMap((s, i) => {
                const next = l.segments[i + 1];
                const isLast = s === last;
                const lines = [
                  <ReferenceLine
                    key={`limit-${l.kind}-${i}`}
                    segment={[{ x: s.from, y: s.value }, { x: s.to, y: s.value }]}
                    stroke={style.color}
                    strokeWidth={2}
                    strokeDasharray={s.beforeFirst ? "5 4" : undefined}
                    ifOverflow="hidden"
                    label={
                      isLast
                        ? {
                            value: wide ? `${style.label.replace(" limit", "")} ${mgdl(s.value)}` : mgdl(s.value),
                            position: "right",
                            fill: "var(--foreground)",
                            fontSize: 12,
                            fontWeight: 600,
                          }
                        : undefined
                    }
                  />,
                ];
                if (next) {
                  lines.push(
                    <ReferenceLine
                      key={`step-${l.kind}-${i}`}
                      segment={[{ x: s.to, y: s.value }, { x: s.to, y: next.value }]}
                      stroke={style.color}
                      strokeWidth={2}
                      ifOverflow="hidden"
                    />,
                  );
                }
                return lines;
              });
            })}

            {contexts.map((c) => (
              <Scatter
                key={c}
                name={SERIES[c].label}
                data={m.points.filter((p) => p.context === c)}
                dataKey="value"
                shape={PointShape}
                isAnimationActive={false}
              />
            ))}
            <Tooltip content={(props) => <Tip active={props.active} payload={props.payload} />} cursor={{ stroke: "var(--axis)", strokeDasharray: "3 3" }} isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      <figcaption>
        <ul className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-sm" aria-label="Legend">
          {contexts.map((c) => (
            <li key={c} className="flex items-center gap-1.5">
              <Swatch>
                <Marker context={c} cx={11} cy={7} />
              </Swatch>
              {SERIES[c].label}
            </li>
          ))}
          {m.limits.map((l) => (
            <li key={l.kind} className="flex items-center gap-1.5">
              <Swatch>
                <line x1={1} y1={7} x2={21} y2={7} stroke={LIMIT_STYLE[l.kind].color} strokeWidth={2} />
              </Swatch>
              {LIMIT_STYLE[l.kind].label}
            </li>
          ))}
          {m.limits.length > 0 && (
            <li className="flex items-center gap-1.5">
              <Swatch>
                <rect x={1} y={2} width={20} height={10} fill="url(#band-hatch)" />
              </Swatch>
              Pre-breach band (within {bandPct} %)
            </li>
          )}
          <li className="flex items-center gap-1.5">
            <Swatch>
              <circle cx={11} cy={7} r={5.5} fill="none" stroke="var(--chart-critical)" strokeWidth={2} />
            </Swatch>
            Breach (ringed)
          </li>
          {m.beforeFirst && (
            <li className="flex items-center gap-1.5">
              <Swatch>
                <line x1={1} y1={7} x2={21} y2={7} stroke="var(--axis)" strokeWidth={2} strokeDasharray="4 3" />
              </Swatch>
              Dashed: first limit, applied to readings taken before it was set
            </li>
          )}
        </ul>
      </figcaption>
    </figure>
  );
}
