// @vitest-environment node
import { describe, expect, it } from "vitest";
import { formatMinor, formatSize, formatYuan, parseMinor } from "./format";

describe("parseMinor", () => {
  it.each([
    ["19.99", 2, 1999],
    ["0.1", 2, 10],
    ["128", 2, 12800],
    [" 1.25 ", 3, 1250],
    ["1.005", 3, 1005],
  ])("把 %s 转成整数，不受浮点误差影响", (text, digits, expected) => {
    expect(parseMinor(text, digits)).toBe(expected);
  });

  it("空内容返回 null，表示没有填", () => {
    expect(parseMinor("  ", 2)).toBeNull();
  });

  it.each(["abc", "-1", "1.234", "1,000", "0", "0.00", "99999999999999999"])(
    "拒绝不合法的金额 %s",
    (text) => {
      expect(parseMinor(text, 2)).toBeUndefined();
    },
  );
});

describe("formatMinor", () => {
  it.each([
    [1999, 2, "19.99"],
    [5, 2, "0.05"],
    [12800, 2, "128.00"],
    [1250, 3, "1.25"],
    [100000, 3, "100"],
    [1005, 3, "1.005"],
    [null, 2, ""],
  ])("格式化 %s", (value, digits, expected) => {
    expect(formatMinor(value, digits)).toBe(expected);
  });

  it("金额转成文字再转回来不变", () => {
    for (const cents of [1, 99, 100, 1999, 123456789]) {
      expect(parseMinor(formatMinor(cents, 2), 2)).toBe(cents);
    }
  });
});

describe("formatYuan", () => {
  it("带千分位和两位小数", () => {
    expect(formatYuan(123456789)).toBe("¥1,234,567.89");
    expect(formatYuan(5)).toBe("¥0.05");
  });

  it("没有金额时显示占位符，不显示 0", () => {
    expect(formatYuan(null)).toBe("—");
  });
});

describe("formatSize", () => {
  it("按大小选择单位", () => {
    expect(formatSize(512)).toBe("512 B");
    expect(formatSize(2048)).toBe("2 KB");
    expect(formatSize(6 * 1024 * 1024)).toBe("6.0 MB");
  });
});
