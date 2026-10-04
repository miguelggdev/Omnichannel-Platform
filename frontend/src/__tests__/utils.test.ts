import { describe, expect, it } from "vitest";
import { contactName, formatBytes, initials } from "@/lib/utils";

describe("utils", () => {
  it("initials", () => {
    expect(initials("Ana Maria Perez")).toBe("AP");
    expect(initials("ana")).toBe("A");
    expect(initials("")).toBe("?");
    expect(initials(null)).toBe("?");
  });

  it("formatBytes", () => {
    expect(formatBytes(null)).toBe("—");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(50 * 1024 * 1024)).toBe("50 MB");
  });

  it("contactName prefiere display_name y cae a nombre + apellido", () => {
    expect(contactName({ display_name: "Ana P.", first_name: "Ana", last_name: "Perez" })).toBe("Ana P.");
    expect(contactName({ first_name: "Ana", last_name: "Perez" })).toBe("Ana Perez");
    expect(contactName({ first_name: "Ana" })).toBe("Ana");
    expect(contactName({})).toBe("—");
  });
});
