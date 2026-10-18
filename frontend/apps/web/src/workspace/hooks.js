import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, errorMessage, fetchAssetObjectUrl, getHealth, getModes } from '../lib/api';
import { useAuth } from '../AuthContext';

/**
 * Hooks shared by the workspace screens.
 *
 * Two behaviours are centralized here on purpose:
 *  - a 401 from any call signs the session out exactly once, instead of each
 *    screen inventing its own recovery;
 *  - nothing calls `setState` after unmount, so navigating mid-request is not
 *    a React warning or a stale render.
 */

/** Tracks whether the component is still mounted. */
export function useMountedRef() {
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  return mounted;
}

/**
 * Wrap an async backend call with pending/error state.
 *
 * `run()` resolves to `{ ok, data, error }` rather than throwing, so callers
 * can sequence steps without try/catch at every call site.
 */
export function useAsyncAction() {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const mounted = useMountedRef();
  const { handleAuthFailure } = useAuth();

  const run = useCallback(
    async (fn) => {
      setPending(true);
      setError(null);
      try {
        const data = await fn();
        if (mounted.current) setPending(false);
        return { ok: true, data, error: null };
      } catch (caught) {
        if (caught instanceof ApiError && caught.isAuthFailure) handleAuthFailure();
        if (mounted.current) {
          setPending(false);
          setError(caught);
        }
        return { ok: false, data: null, error: caught };
      }
    },
    [handleAuthFailure, mounted],
  );

  return { run, pending, error, clearError: () => setError(null) };
}

/**
 * Load data on mount (and whenever `deps` change), with explicit
 * loading/error/empty states and abort on unmount.
 */
export function useResource(loader, deps = [], { enabled = true } = {}) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(Boolean(enabled));
  const [error, setError] = useState(null);
  const { handleAuthFailure } = useAuth();
  const [nonce, setNonce] = useState(0);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  // A background refresh: same request, but no loading flash and a failed
  // poll leaves the last good data on screen.
  const quiet = useRef(false);
  const refresh = useCallback(() => {
    quiet.current = true;
    setNonce((n) => n + 1);
  }, []);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return undefined;
    }
    const controller = new AbortController();
    let active = true;
    const silent = quiet.current;
    quiet.current = false;
    if (!silent) {
      setLoading(true);
      setError(null);
    }
    Promise.resolve(loader(controller.signal))
      .then((result) => {
        if (!active) return;
        setData(result);
        if (silent) setError(null);
      })
      .catch((caught) => {
        if (!active || controller.signal.aborted) return;
        if (caught instanceof ApiError && caught.isAuthFailure) handleAuthFailure();
        if (!silent) setError(caught);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, enabled]);

  return { data, loading, error, reload, refresh, setData };
}

/**
 * Backend reachability + provider configuration.
 *
 * Polls health so the workspace can say "backend offline" honestly rather than
 * rendering an empty dashboard that looks like "no campaigns yet".
 */
export function useBackendStatus({ intervalMs = 30_000 } = {}) {
  const [state, setState] = useState({ status: 'checking', health: null, modes: null, error: null });

  useEffect(() => {
    let active = true;
    let timer = null;
    const controller = new AbortController();

    const check = async () => {
      try {
        const health = await getHealth(controller.signal);
        let modes = null;
        try {
          modes = await getModes(controller.signal);
        } catch {
          // /modes is richer but optional; health alone is enough to be "online".
        }
        if (!active) return;
        setState({ status: health.status === 'ok' ? 'online' : 'degraded', health, modes, error: null });
      } catch (caught) {
        if (!active || controller.signal.aborted) return;
        setState({ status: 'offline', health: null, modes: null, error: caught });
      }
    };

    check();
    timer = setInterval(check, intervalMs);
    return () => {
      active = false;
      controller.abort();
      if (timer) clearInterval(timer);
    };
  }, [intervalMs]);

  return state;
}

/**
 * Object URL for an asset's bytes.
 *
 * Asset downloads need the bearer token, which the browser will not attach to
 * `<img src>`. Fetch the blob, hand back an object URL, and revoke it on
 * unmount so repeated previews do not leak memory.
 */
export function useAssetObjectUrl(assetId, { enabled = true, nonce = 0 } = {}) {
  const [url, setUrl] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    // `nonce` lets a player retry a failed download.
    setError(null);
    if (!assetId || !enabled) return undefined;
    const controller = new AbortController();
    let active = true;
    let created = null;

    fetchAssetObjectUrl(assetId, controller.signal)
      .then((objectUrl) => {
        if (!active) {
          URL.revokeObjectURL(objectUrl);
          return;
        }
        created = objectUrl;
        setUrl(objectUrl);
      })
      .catch((caught) => {
        if (!active || controller.signal.aborted) return;
        setError(caught);
      });

    return () => {
      active = false;
      controller.abort();
      if (created) URL.revokeObjectURL(created);
    };
  }, [assetId, enabled, nonce]);

  return { url, error };
}

/**
 * Call `callback` every `intervalMs` while `active` is true.
 *
 * The latest callback is always used without restarting the timer, and the
 * timer stops as soon as `active` turns false (or the component unmounts), so
 * a finished job is never polled again.
 */
export function usePolling(callback, { active, intervalMs = 5000 }) {
  const saved = useRef(callback);
  useEffect(() => {
    saved.current = callback;
  }, [callback]);

  useEffect(() => {
    if (!active) return undefined;
    const timer = setInterval(() => {
      // Skip ticks while the tab is hidden; state lives on the server anyway.
      if (typeof document === 'undefined' || !document.hidden) saved.current();
    }, intervalMs);
    return () => clearInterval(timer);
  }, [active, intervalMs]);
}

/** Copy text to the clipboard; resolves to whether it worked. */
export async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall through to the legacy path (insecure context, denied permission).
  }
  try {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  } catch {
    return false;
  }
}

/** Short-lived status message with an error flavour. */
export function useToast(timeoutMs = 4200) {
  const [toast, setToast] = useState(null);
  const timer = useRef(null);

  const show = useCallback(
    (message, { error = false } = {}) => {
      if (timer.current) clearTimeout(timer.current);
      setToast({ message, error });
      timer.current = setTimeout(() => setToast(null), timeoutMs);
    },
    [timeoutMs],
  );

  const showError = useCallback((caught) => show(errorMessage(caught), { error: true }), [show]);

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  return { toast, show, showError, dismiss: () => setToast(null) };
}
