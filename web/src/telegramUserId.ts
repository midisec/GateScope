const LABELED_USER_ID = /(?:telegram\s*)?(?:user\s*)?id\s*[:：#-]?\s*([1-9]\d{0,15})/i;
const CHINESE_USER_ID = /用户\s*(?:id|编号)\s*[:：#-]?\s*([1-9]\d{0,15})/i;
const PLAIN_USER_ID = /^[1-9]\d{0,15}$/;

function validUserId(candidate: string): string | null {
  if (!PLAIN_USER_ID.test(candidate)) return null;
  const value = Number(candidate);
  return Number.isSafeInteger(value) && value > 0 ? String(value) : null;
}

export function parseTelegramUserId(text: string): string | null {
  const value = text.trim();
  const plain = validUserId(value);
  if (plain) return plain;
  const labeled = value.match(LABELED_USER_ID) ?? value.match(CHINESE_USER_ID);
  return labeled ? validUserId(labeled[1]) : null;
}

export function appendTelegramUserId(current: string, userId: string): string {
  const normalized = validUserId(userId);
  if (!normalized) return current;
  const ids = current.split(/[\s,]+/).map((item) => validUserId(item)).filter((item): item is string => Boolean(item));
  if (!ids.includes(normalized)) ids.push(normalized);
  return ids.join(", ");
}
