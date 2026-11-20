export type ApiErrorShape = {
  error?: { code?: string; message?: string };
  detail?: string | Array<{ loc?: (string | number)[]; msg?: string }>;
};

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

function csrfToken(): string | undefined {
  return document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith("gatescope_csrf="))
    ?.slice("gatescope_csrf=".length);
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = csrfToken();
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (init.signal?.aborted) controller.abort();
  init.signal?.addEventListener("abort", abort, { once: true });
  let timedOut = false;
  const timer = globalThis.setTimeout(() => { timedOut = true; controller.abort(); }, 30000);
  try {
  const response = await fetch(path, { ...init, headers, signal: controller.signal, credentials: "same-origin" });
  const payload = (await response.json().catch(() => null)) as ApiErrorShape | null;
  if (payload === null) throw new ApiError(response.status, "INVALID_RESPONSE", "服务返回异常，请刷新重试；不要重复提交敏感操作");
  if (!response.ok) {
    if (payload.error?.code === "AUTH_REQUIRED") window.dispatchEvent(new Event("gatescope:session-expired"));
    throw new ApiError(
      response.status,
      payload.error?.code ?? `HTTP_${response.status}`,
      payload.error?.message ?? (typeof payload.detail === "string" ? payload.detail : Array.isArray(payload.detail) ? "请检查表单中的必填项、数值范围和格式" : "请求失败"),
    );
  }
  return payload as T;
  } catch (error) {
    if (timedOut) throw new ApiError(408, "REQUEST_TIMEOUT", method === "GET" ? "请求超时，请重试" : "请求超时，提交状态尚未确认；请先刷新记录，避免重复提交");
    if (error instanceof TypeError) throw new ApiError(0, "NETWORK_ERROR", method === "GET" ? "网络连接失败，请检查连接后重试" : "网络中断，提交状态尚未确认；请先刷新记录，避免重复提交");
    throw error;
  } finally {
    globalThis.clearTimeout(timer); init.signal?.removeEventListener("abort", abort);
  }
}

export function jsonBody(value: unknown): string {
  return JSON.stringify(value);
}
