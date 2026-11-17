import { describe, expect, it } from "vitest";

import { countOf, dayLabel, firstName, isDay, mgdl, signed, withDays, withParams } from "@/shared/lib/format";

// vitest runs with TZ=Asia/Kolkata (vite.config.ts), the demo cohort's timezone.
describe("day separators in India time", () => {
  const now = new Date("2026-11-17T12:00:00+05:30");

  it("labels today, yesterday, this year and other years", () => {
    expect(new Date("2026-11-17T00:00:00Z").getTimezoneOffset()).toBe(-330); // IST, +05:30
    expect(dayLabel("2026-11-17T00:10:00+05:30", now)).toBe("Today");
    // 23:50 IST on the 16th is 18:20 UTC: still "Yesterday" in India time.
    expect(dayLabel("2026-11-16T18:20:00Z", now)).toBe("Yesterday");
    // 18:40 UTC on the 16th is 00:10 IST on the 17th: "Today".
    expect(dayLabel("2026-11-16T18:40:00Z", now)).toBe("Today");
    expect(dayLabel("2026-11-02T09:00:00+05:30", now)).toBe("2 Nov");
    expect(dayLabel("2025-11-02T09:00:00+05:30", now)).toBe("2 Nov 2025");
  });

  it("inserts a separator before the first message of each local day", () => {
    const msgs = [
      { id: "a", created_at: "2026-11-16T18:20:00Z" },
      { id: "b", created_at: "2026-11-16T18:25:00Z" },
      { id: "c", created_at: "2026-11-16T18:40:00Z" },
    ];
    const out = withDays(msgs, now);
    expect(out.map((x) => (isDay(x) ? `[${x.day}]` : x.id))).toEqual(["[Yesterday]", "a", "b", "[Today]", "c"]);
  });
});

describe("numbers and names", () => {
  it("formats mg/dL, adjustments and counts", () => {
    expect(mgdl(130)).toBe("130");
    expect(mgdl(123.5)).toBe("123.5");
    expect(mgdl(null)).toBe("-");
    expect(signed(5)).toBe("+5");
    expect(signed(-3.5)).toBe("-3.5");
    expect(signed(0)).toBe("0");
    expect(countOf({ count: 12, denominator: 40, rate: 0.3 })).toBe("12 of 40 (30.0%)");
    expect(countOf({ count: 0, denominator: 0, rate: null })).toBe("0 of 0 (n/a)");
  });

  it("keeps only set parameters and only first names", () => {
    expect(withParams("/x", { a: "", b: null, c: 0, d: "y z" })).toBe("/x?c=0&d=y%20z");
    expect(firstName("Meera Iyer", "Practitioner")).toBe("Meera");
    expect(firstName("  ", "Practitioner")).toBe("Practitioner");
  });
});
