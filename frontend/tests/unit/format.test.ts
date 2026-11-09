import { describe, expect, it } from "vitest";

import { filtersToSearch, parseFilters, priorityStyle, relativeTime } from "@/lib/format";

describe("feed filters <-> URL", () => {
  it("round-trips through the URL", () => {
    const f = parseFilters({ source: "demo", priority: "5,4", status: "all", sort: "received" });
    expect(f).toMatchObject({ source: ["demo"], priority: [5, 4], status: "all", sort: "received" });
    expect(filtersToSearch(f)).toBe("?source=demo&priority=5%2C4&status=all&sort=received");
  });

  it("uses safe defaults and ignores junk", () => {
    const f = parseFilters({ priority: "9,x,3", status: "weird" });
    expect(f.priority).toEqual([3]);
    expect(f.status).toBe("open");
    expect(f.sort).toBe("priority");
    expect(filtersToSearch(parseFilters({}))).toBe("");
  });
});

describe("priority styling", () => {
  it("always has a P-label, never colour alone", () => {
    for (const p of [1, 2, 3, 4, 5]) expect(priorityStyle(p).label).toBe(`P${p}`);
    expect(priorityStyle(null).label).toBe("P?");
  });
});

describe("relativeTime", () => {
  it("formats compactly", () => {
    const now = new Date("2026-11-10T12:00:00Z");
    expect(relativeTime("2026-11-10T11:59:50Z", now)).toBe("just now");
    expect(relativeTime("2026-11-10T11:45:00Z", now)).toBe("15m");
    expect(relativeTime("2026-11-10T09:00:00Z", now)).toBe("3h");
    expect(relativeTime("2026-11-08T12:00:00Z", now)).toBe("2d");
  });
});
