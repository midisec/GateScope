// Every entry point shares one selection version, including synchronous search picks.
export class LatestSelection {
  private version = 0;
  get current() { return this.version; }
  invalidate() { this.version += 1; }
  commit(apply: () => void) { this.invalidate(); apply(); }
  async resolve<T>(read: () => Promise<T>, apply: (value: T) => void) {
    const version = ++this.version;
    try {
      const value = await read();
      if (version === this.version) apply(value);
    } catch (error) {
      if (version === this.version) throw error;
    }
  }
}

export function startIndicatorRequest<T>(read: (signal: AbortSignal) => Promise<T>, callbacks: {
  success: (value: T) => void;
  error: (error: unknown, retrying: boolean) => void;
  loading: (value: boolean) => void;
  visible: () => boolean;
}) {
  let disposed = false, pending = false, failed = false, attempts = 0;
  let controller: AbortController | undefined, timer: ReturnType<typeof setTimeout> | undefined;
  async function load() {
    if (disposed || pending || !callbacks.visible()) return;
    clearTimeout(timer); timer = undefined;
    pending = true; attempts += 1; controller = new AbortController(); callbacks.loading(true);
    try {
      const value = await read(controller.signal);
      if (disposed) return;
      failed = false; callbacks.success(value);
    } catch (error) {
      if (disposed) return;
      failed = true;
      const status = typeof error === "object" && error !== null && "status" in error ? Number(error.status) : 0;
      const retrying = attempts < 3 && (status === 0 || status === 408 || status === 429 || status >= 500);
      callbacks.error(error, retrying);
      if (retrying) timer = setTimeout(() => void load(), 2000 * attempts);
    } finally {
      pending = false;
      if (!disposed) callbacks.loading(false);
    }
  }
  const retry = () => { if (!disposed && !pending) { attempts = 0; void load(); } };
  void load();
  return {
    retry,
    resume: () => { if (failed || attempts === 0) retry(); },
    dispose: () => { disposed = true; controller?.abort(); clearTimeout(timer); },
  };
}
