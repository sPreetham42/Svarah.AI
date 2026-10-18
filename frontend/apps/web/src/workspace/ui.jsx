import React from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  FileImage,
  Info,
  Loader2,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
  XCircle,
} from 'lucide-react';
import { useAssetObjectUrl } from './hooks';
import { errorMessage } from '../lib/api';

/** Inline spinner sized for buttons and card headers. */
export function Spinner({ label }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
      <span className="ws-spinner" aria-hidden="true" />
      {label ? <span>{label}</span> : null}
      <span className="sr-only" role="status">{label || 'Loading'}</span>
    </span>
  );
}

/** Message block for errors, warnings and confirmations. */
export function Banner({ tone = 'info', title, children, action }) {
  const Icon = { error: XCircle, warn: AlertTriangle, ok: CheckCircle2, info: Info }[tone] ?? Info;
  const toneClass = { error: 'is-error', warn: 'is-warn', ok: 'is-ok', info: '' }[tone] ?? '';
  return (
    <div className={`ws-banner ${toneClass}`} role={tone === 'error' ? 'alert' : 'status'}>
      <Icon size={17} />
      <div className="ws-banner-body">
        {title ? <b>{title}</b> : null}
        {children}
      </div>
      {action}
    </div>
  );
}

/** Banner rendered straight from a thrown error. */
export function ErrorBanner({ error, title = 'That did not work', action }) {
  if (!error) return null;
  return (
    <Banner tone="error" title={title} action={action}>
      {errorMessage(error)}
      {error?.details?.missing?.length ? (
        <div style={{ marginTop: 6 }}>
          Missing: <span className="ws-mono">{error.details.missing.join(', ')}</span>
        </div>
      ) : null}
    </Banner>
  );
}

export function EmptyState({ icon: Icon = FileImage, title, children, action }) {
  return (
    <div className="ws-empty">
      <Icon size={34} />
      <h3>{title}</h3>
      {children ? <p>{children}</p> : null}
      {action}
    </div>
  );
}

/** Connection/mode pill for the workspace header. */
export function StatusPill({ status, children, title }) {
  const cls = { online: 'is-ok', degraded: 'is-warn', offline: 'is-bad', checking: 'is-checking' }[status] ?? '';
  return (
    <span className={`ws-pill ${cls}`} title={title}>
      <span className="ws-dot" />
      {children}
    </span>
  );
}

/**
 * Guardian verdict badge.
 *
 * PASS is the only verdict rendered as success; WARN and NEEDS_REVIEW stay
 * visibly unresolved, because the publishing gate treats them that way too.
 */
export function VerdictBadge({ verdict }) {
  const map = {
    PASS: { cls: 'is-pass', Icon: ShieldCheck, label: 'Verified' },
    FAIL: { cls: 'is-fail', Icon: ShieldAlert, label: 'Failed' },
    WARN: { cls: 'is-review', Icon: AlertTriangle, label: 'Needs review' },
    NEEDS_REVIEW: { cls: 'is-review', Icon: AlertTriangle, label: 'Needs review' },
  };
  const entry = map[verdict];
  if (!entry) {
    return (
      <span className="ws-tag">
        <Clock size={11} /> Not verified
      </span>
    );
  }
  const { cls, Icon, label } = entry;
  return (
    <span className={`ws-tag ${cls}`}>
      <Icon size={11} /> {label}
    </span>
  );
}

/** Asset status badge, mirroring the backend's `asset_status` vocabulary. */
export function AssetStatusBadge({ status }) {
  const map = {
    verified: { cls: 'is-pass', label: 'Verified' },
    human_verified: { cls: 'is-pass', label: 'Verified by you' },
    failed: { cls: 'is-fail', label: 'Failed' },
    needs_review: { cls: 'is-review', label: 'Needs review' },
    validating: { cls: '', label: 'Awaiting verification' },
    pending: { cls: '', label: 'Pending' },
  };
  const entry = map[status] ?? { cls: '', label: status || 'unknown' };
  return <span className={`ws-tag ${entry.cls}`}>{entry.label}</span>;
}

/**
 * Marks output produced by the offline mock providers.
 *
 * Shown wherever a mock artifact appears, so a demo asset is never mistaken
 * for something a live model generated.
 */
export function MockBadge({ provider }) {
  return (
    <span className="ws-tag is-mock" title={provider ? `Provider: ${provider}` : undefined}>
      Demo output
    </span>
  );
}

/** Authenticated `<img>`: fetches the asset blob with the session token. */
export function AssetImage({ assetId, alt }) {
  const { url, error } = useAssetObjectUrl(assetId);
  if (error) {
    return (
      <div className="ws-asset-media" style={{ padding: 16, textAlign: 'center' }}>
        <small style={{ color: 'var(--muted)' }}>Preview unavailable: {errorMessage(error)}</small>
      </div>
    );
  }
  if (!url) {
    return (
      <div className="ws-asset-media">
        <Loader2 size={20} className="ws-rotate" aria-hidden="true" />
        <span className="sr-only">Loading preview</span>
      </div>
    );
  }
  return (
    <div className="ws-asset-media">
      <img src={url} alt={alt} />
    </div>
  );
}

/**
 * Authenticated audio player with an explicit replay control.
 *
 * The bytes are fetched with the session token (see `useAssetObjectUrl`); a
 * failed download or an undecodable file is shown as an error with a retry,
 * never as a silent, dead player.
 */
export function AssetAudio({ assetId, label = 'Generated audio' }) {
  const [attempt, setAttempt] = React.useState(0);
  const [playError, setPlayError] = React.useState('');
  const ref = React.useRef(null);
  const { url, error } = useAssetObjectUrl(assetId, { enabled: true, nonce: attempt });

  const retry = () => {
    setPlayError('');
    setAttempt((value) => value + 1);
  };

  if (error || playError) {
    return (
      <div className="ws-player is-error" role="alert">
        <small>Audio unavailable: {playError || errorMessage(error)}</small>
        <button type="button" className="ws-btn is-ghost is-small" onClick={retry}>
          <RotateCcw size={12} /> Retry
        </button>
      </div>
    );
  }
  if (!url) {
    return (
      <div className="ws-player">
        <Loader2 size={16} className="ws-rotate" aria-hidden="true" />
        <small>Loading audio…</small>
      </div>
    );
  }
  const replay = () => {
    const element = ref.current;
    if (!element) return;
    element.currentTime = 0;
    element.play().catch(() => setPlayError('The browser could not play this audio.'));
  };
  return (
    <div className="ws-player">
      <audio
        ref={ref}
        controls
        preload="metadata"
        src={url}
        aria-label={label}
        onError={() => setPlayError('The file could not be decoded.')}
      >
        <track kind="captions" />
      </audio>
      <button type="button" className="ws-btn is-ghost is-small" onClick={replay} aria-label={`Replay ${label}`}>
        <RotateCcw size={12} /> Replay
      </button>
    </div>
  );
}

/** Authenticated video player. `playable=false` marks a demo container. */
export function AssetVideo({ assetId, playable = true }) {
  const [attempt, setAttempt] = React.useState(0);
  const [playError, setPlayError] = React.useState('');
  const { url, error } = useAssetObjectUrl(assetId, { enabled: playable, nonce: attempt });

  if (!playable) {
    return (
      <div className="ws-asset-media" style={{ padding: 16, textAlign: 'center' }}>
        <small style={{ color: 'var(--muted)' }}>
          Demo job output — mock mode stores an empty video container. Run in live mode for real footage.
        </small>
      </div>
    );
  }
  if (error || playError) {
    return (
      <div className="ws-asset-media" style={{ padding: 16, textAlign: 'center' }} role="alert">
        <small style={{ color: 'var(--muted)' }}>Video unavailable: {playError || errorMessage(error)}</small>
        <button
          type="button"
          className="ws-btn is-ghost is-small"
          onClick={() => {
            setPlayError('');
            setAttempt((value) => value + 1);
          }}
        >
          <RotateCcw size={12} /> Retry
        </button>
      </div>
    );
  }
  if (!url) {
    return (
      <div className="ws-asset-media">
        <Loader2 size={20} className="ws-rotate" aria-hidden="true" />
        <span className="sr-only">Loading video</span>
      </div>
    );
  }
  return (
    <div className="ws-asset-media is-video">
      <video controls preload="metadata" src={url} onError={() => setPlayError('The file could not be decoded.')}>
        <track kind="captions" />
      </video>
    </div>
  );
}

/** Thin determinate/indeterminate progress bar for long-running jobs. */
export function ProgressBar({ value, label }) {
  const known = typeof value === 'number' && Number.isFinite(value);
  const clamped = known ? Math.max(0, Math.min(100, value)) : null;
  return (
    <div
      className={`ws-progress ${known ? '' : 'is-indeterminate'}`}
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      {...(known ? { 'aria-valuenow': clamped } : {})}
    >
      <i style={known ? { width: `${Math.max(clamped, 4)}%` } : undefined} />
    </div>
  );
}

/** Step rail across the campaign journey. */
export function StepRail({ steps, current, onSelect }) {
  return (
    <nav className="ws-steps" aria-label="Campaign steps">
      {steps.map((step, index) => {
        const isActive = step.id === current;
        const cls = isActive ? 'is-active' : step.done ? 'is-done' : '';
        return (
          <button
            key={step.id}
            type="button"
            className={`ws-step ${cls}`}
            aria-current={isActive ? 'step' : undefined}
            disabled={step.disabled}
            title={step.disabled ? step.disabledReason : undefined}
            onClick={() => onSelect(step.id)}
          >
            <span className="ws-step-num">{step.done ? '✓' : index + 1}</span>
            {step.label}
          </button>
        );
      })}
    </nav>
  );
}

export function Toast({ toast }) {
  if (!toast) return null;
  return (
    <div className={`ws-toast ${toast.error ? 'is-error' : ''}`} role="status" aria-live="polite">
      {toast.error ? <XCircle size={15} /> : <CheckCircle2 size={15} />}
      <span>{toast.message}</span>
    </div>
  );
}
