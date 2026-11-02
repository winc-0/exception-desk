const dateTime = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

export function formatTime(value: string): string {
  return dateTime.format(new Date(value));
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * 把用户输入的小数转成整数（元→分、千克→克）。全程按字符串处理，不经过浮点数，
 * 否则 19.99 * 100 这样的计算会算出 1998.9999。
 * 空字符串返回 null，格式不对返回 undefined。
 */
export function parseMinor(text: string, digits: number): number | null | undefined {
  const value = text.trim();
  if (!value) return null;
  const match = new RegExp(`^(\\d+)(?:\\.(\\d{1,${digits}}))?$`).exec(value);
  if (!match) return undefined;
  const result = Number(match[1] + (match[2] ?? "").padEnd(digits, "0"));
  return Number.isSafeInteger(result) && result > 0 ? result : undefined;
}

export function formatMinor(value: number | null, digits: number): string {
  if (value === null) return "";
  const text = String(value).padStart(digits + 1, "0");
  const fraction = text.slice(-digits);
  const whole = text.slice(0, -digits);
  // 金额固定两位小数，重量去掉末尾的 0
  return digits === 2 ? `${whole}.${fraction}` : `${whole}.${fraction}`.replace(/\.?0+$/, "");
}

export function formatYuan(cents: number | null): string {
  if (cents === null) return "—";
  const [whole, fraction] = formatMinor(cents, 2).split(".");
  return `¥${whole.replace(/\B(?=(\d{3})+$)/g, ",")}.${fraction}`;
}
