// Lenovmail — authored by satuapps (satuapps.com)
// Shared formatters for the GUI; used by every feature to keep date/size display consistent.

const DATE_TIME = new Intl.DateTimeFormat("en-US", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

const TIME_ONLY = new Intl.DateTimeFormat("en-US", { hour: "2-digit", minute: "2-digit" });

const DAY_MONTH = new Intl.DateTimeFormat("en-US", { day: "2-digit", month: "short" });

/** Short date for the message list: time for today, date for last month, year for the rest. */
export function shortDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const sameDay =
    date.getDate() === now.getDate() &&
    date.getMonth() === now.getMonth() &&
    date.getFullYear() === now.getFullYear();
  if (sameDay) return TIME_ONLY.format(date);
  if (date.getFullYear() === now.getFullYear()) return DAY_MONTH.format(date);
  return date.getFullYear().toString();
}

/** Full date + time, for read headers and tables. */
export function fullDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "—" : DATE_TIME.format(date);
}

function ago(count: number, unit: string): string {
  return `${count} ${unit}${count === 1 ? "" : "s"} ago`;
}

/** "3 minutes ago", "yesterday", "Sep 12, 2026". */
export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "never";
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return ago(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (hours < 24) return ago(hours, "hour");
  const days = Math.round(hours / 24);
  if (days === 1) return "yesterday";
  if (days < 30) return ago(days, "day");
  return fullDate(iso);
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

/** Sender display name: name, or the local part of the address. */
export function senderLabel(name: string | null | undefined, addr: string | null | undefined): string {
  if (name && name.trim()) return name.trim();
  if (!addr) return "(no sender)";
  return addr.split("@")[0] ?? addr;
}
