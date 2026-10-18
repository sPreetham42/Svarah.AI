import React, { useEffect, useMemo } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import {
  ArrowLeft,
  AudioLines,
  Check,
  Film,
  FileImage,
  Image as ImageIcon,
  LogOut,
  Megaphone,
  Mic,
  Volume2,
  X,
} from 'lucide-react';
import {
  getCampaign,
  getPlan,
  listAssets,
  listCampaigns,
  listPublishRecords,
  listVideoJobs,
} from '../lib/api';
import { useAuth } from '../AuthContext';
import ThemeToggle from '../ThemeToggle';
import { useBackendStatus, useResource } from './hooks';
import { Banner, EmptyState, ErrorBanner, Spinner, StatusPill } from './ui';
import { AssetGallery } from './AssetsStep';
import './workspace.css';

/**
 * The studio: a live, view-only window onto every campaign.
 *
 * Campaigns are made in the voice workspace. This screen only *shows* them —
 * what was said, the locked facts, the plan, every asset, each check and each
 * post — and keeps itself current by polling. There is nothing here that
 * creates, edits, generates, verifies or publishes: it makes no write calls.
 *
 * The open campaign lives in the URL (`?campaign=12`), so a refresh or a
 * shared link lands on the same screen.
 */

const LIST_POLL_MS = 5000;
const DETAIL_POLL_MS = 3000;

const CHANNEL_LABELS = {
  instagram: 'Instagram',
  facebook: 'Facebook',
  x: 'X',
  whatsapp: 'WhatsApp',
  poster_headline: 'Poster headline',
  poster_subline: 'Poster subline',
  reel_script: 'Reel script',
  voice_script: 'Voice script',
};

/** Audit event → what happened, in plain words. Unknown events fall back to their name. */
const EVENT_LABELS = {
  'campaign.created': 'Offer received',
  'stt.completed': 'Speech turned into words',
  'stt.unavailable': 'Speech could not be transcribed',
  'extraction.completed': 'Facts read from the offer',
  'voice.turn': 'Spoken reply during fact review',
  'voice.publish_turn': 'Spoken answer about posting',
  'factsheet.updated': 'Facts corrected',
  'factsheet.locked': 'Facts confirmed and locked',
  'plan.created': 'Campaign planned',
  'publish.prepared': 'Post prepared',
  'publish.approved': 'Post approved',
  'publish.executed': 'Post sent',
  'publish.social': 'Posted to social media',
};

/** Re-run `tick` on an interval while the tab is visible. */
function usePolling(tick, intervalMs) {
  useEffect(() => {
    const timer = setInterval(() => {
      if (!document.hidden) tick();
    }, intervalMs);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs]);
}

function fillTokens(text, tokens) {
  return String(text ?? '').replace(/\{\{\s*([A-Za-z0-9_]+)\s*\}\}/g, (match, name) => tokens?.[name] ?? match);
}

function clock(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString();
}

export default function Workspace() {
  const { user, checking, sessionNotice, logout } = useAuth();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const rawCampaign = Number(searchParams.get('campaign'));
  const openCampaignId = Number.isInteger(rawCampaign) && rawCampaign > 0 ? rawCampaign : null;
  const backend = useBackendStatus();

  useEffect(() => {
    if (!checking && !user) navigate('/login', { replace: true });
  }, [checking, user, navigate]);

  if (checking) {
    return (
      <div className="ws-root">
        <div className="ws-body">
          <Spinner label="Restoring your session…" />
        </div>
      </div>
    );
  }
  if (!user) return null;

  return (
    <div className="ws-root">
      <header className="ws-nav">
        <Link to="/" className="ws-nav-logo" aria-label="Svarah home">
          <span className="logo-mark">
            <AudioLines size={17} strokeWidth={2.5} />
          </span>
          Svarah<span style={{ color: 'var(--muted)' }}>.AI</span>
        </Link>

        <StatusPill status={backend.status === 'online' ? 'online' : backend.status}>
          {backend.status === 'online' ? 'Live · view only' : backend.status === 'offline' ? 'Offline' : 'Connecting'}
        </StatusPill>

        <span className="ws-nav-spacer" />

        <Link to="/workspace" className="ws-btn is-ghost is-small" title="Make a campaign by voice">
          <Mic size={13} />
          <span className="hide-mobile">Voice workspace</span>
        </Link>

        <ThemeToggle />
        <div className="ws-account">
          <span className="ws-avatar">{user.avatar}</span>
          <span className="ws-account-meta">
            <b>{user.name}</b>
            <small>{user.isDemo ? 'Demo account' : user.email || user.phone}</small>
          </span>
          <button
            type="button"
            className="ws-btn is-ghost is-small"
            onClick={() => {
              logout();
              navigate('/');
            }}
          >
            <LogOut size={13} />
            <span className="hide-mobile">Sign out</span>
          </button>
        </div>
      </header>

      <main className="ws-body">
        {sessionNotice ? <Banner tone="warn">{sessionNotice}</Banner> : null}
        {backend.status === 'offline' ? (
          <Banner tone="error" title="Svarah is not reachable right now">
            This page will catch up by itself as soon as the connection is back.
          </Banner>
        ) : null}

        {openCampaignId ? (
          <CampaignLive
            key={openCampaignId}
            campaignId={openCampaignId}
            onClose={() => setSearchParams({})}
          />
        ) : (
          <CampaignList onOpen={(id) => setSearchParams({ campaign: String(id) })} />
        )}
      </main>
    </div>
  );
}

/** Every campaign on the account, newest first, refreshed as they change. */
function CampaignList({ onOpen }) {
  const campaigns = useResource((signal) => listCampaigns({ signal }), []);
  usePolling(campaigns.refresh, LIST_POLL_MS);

  const list = campaigns.data ?? [];
  const counts = useMemo(() => {
    const result = { total: list.length, locked: 0, verified: 0, published: 0 };
    list.forEach((campaign) => {
      if (['locked', 'generating', 'verified', 'needs_review', 'approved', 'published', 'exported'].includes(campaign.status)) {
        result.locked += 1;
      }
      if (['verified', 'approved', 'published', 'exported'].includes(campaign.status)) result.verified += 1;
      if (['published', 'exported'].includes(campaign.status)) result.published += 1;
    });
    return result;
  }, [list]);

  return (
    <>
      <div className="ws-page-head">
        <div>
          <span className="ws-kicker">Studio · live</span>
          <h1>Your campaigns</h1>
          <p>
            Everything Svarah makes from your voice shows up here as it happens. This is a window, not a
            workbench — campaigns are made and changed in the voice workspace.
          </p>
        </div>
      </div>

      <div className="ws-grid cols-3" style={{ marginBottom: 20 }}>
        <Stat label="Campaigns" value={counts.total} sub="On your account" loading={campaigns.loading} />
        <Stat label="Facts locked" value={counts.locked} sub="Confirmed by you" loading={campaigns.loading} />
        <Stat label="Verified" value={counts.verified} sub="Every number checked" loading={campaigns.loading} />
      </div>

      <ErrorBanner error={campaigns.error} title="Campaigns could not be loaded" />

      {campaigns.loading ? (
        <div style={{ display: 'grid', gap: 10 }}>
          <div className="ws-skeleton" style={{ height: 64 }} />
          <div className="ws-skeleton" style={{ height: 64 }} />
        </div>
      ) : list.length === 0 ? (
        <EmptyState
          icon={Megaphone}
          title="No campaigns yet"
          action={
            <Link className="ws-btn is-primary" to="/workspace">
              <Mic size={15} /> Open the voice workspace
            </Link>
          }
        >
          Say an offer in the voice workspace and it will appear here while it is being made.
        </EmptyState>
      ) : (
        <div>
          {list.map((campaign) => (
            <button key={campaign.id} type="button" className="ws-campaign-row" onClick={() => onOpen(campaign.id)}>
              <span className="ws-campaign-id">#{campaign.id}</span>
              <span className="ws-campaign-main">
                <b>{campaign.input_type === 'audio' ? 'Spoken offer' : 'Typed offer'}</b>
                <span>
                  {new Date(campaign.created_at).toLocaleString()}
                  {campaign.has_transcript ? '' : ' · no transcript captured'}
                </span>
              </span>
              <span className="ws-tag">{campaign.status.replace(/_/g, ' ')}</span>
            </button>
          ))}
        </div>
      )}
    </>
  );
}

function Stat({ label, value, sub, loading }) {
  return (
    <div className="ws-stat">
      <span className="ws-stat-label">{label}</span>
      {loading ? (
        <div className="ws-skeleton" style={{ height: 30, margin: '8px 0 6px' }} />
      ) : (
        <div className="ws-stat-value">{value}</div>
      )}
      <span className="ws-stat-sub">{sub}</span>
    </div>
  );
}

/** One campaign, top to bottom, kept current while it is being made. */
function CampaignLive({ campaignId, onClose }) {
  const campaign = useResource((signal) => getCampaign(campaignId, signal), [campaignId]);
  const assets = useResource((signal) => listAssets(campaignId, signal), [campaignId]);
  // No plan, jobs or posts yet is an ordinary state here, not an error.
  const plan = useResource((signal) => getPlan(campaignId, signal).catch(() => null), [campaignId]);
  const jobs = useResource((signal) => listVideoJobs(campaignId, signal).catch(() => []), [campaignId]);
  const posts = useResource((signal) => listPublishRecords(campaignId, signal).catch(() => []), [campaignId]);

  usePolling(() => {
    campaign.refresh();
    assets.refresh();
    plan.refresh();
    jobs.refresh();
    posts.refresh();
  }, DETAIL_POLL_MS);

  const data = campaign.data;
  const factsheet = data?.factsheet ?? null;
  const facts = factsheet?.facts;
  const tokens = factsheet?.tokens;
  const locked = factsheet?.status === 'locked' || factsheet?.status === 'superseded';
  const all = assets.data ?? [];
  const byKind = useMemo(() => {
    const groups = { poster: [], caption: [], voice: [], video: [] };
    all.forEach((asset) => {
      (groups[asset.kind] ??= []).push(asset);
    });
    return groups;
  }, [all]);
  const verified = all.filter((a) => a.asset_status === 'verified' || a.asset_status === 'human_verified').length;
  const failed = all.filter((a) => a.asset_status === 'failed').length;
  const open = all.length - verified - failed;
  const activeJobs = (jobs.data ?? []).filter((job) => job.active);
  const posted = (posts.data ?? []).filter((record) => record.status === 'PUBLISHED');
  const master = plan.data?.substituted?.master ?? {};

  if (campaign.loading && !data) {
    return (
      <>
        <BackLink onClose={onClose} />
        <div className="ws-card">
          <div className="ws-skeleton" style={{ height: 22, width: 220, marginBottom: 12 }} />
          <div className="ws-skeleton" style={{ height: 90 }} />
        </div>
      </>
    );
  }
  if (campaign.error && !data) {
    return (
      <>
        <BackLink onClose={onClose} />
        <ErrorBanner error={campaign.error} title="This campaign could not be opened" />
      </>
    );
  }

  const stages = [
    { label: 'Offer heard', done: Boolean(data?.transcript) },
    { label: 'Facts read', done: Boolean(facts) },
    { label: 'Facts locked', done: locked },
    { label: 'Planned', done: Boolean(plan.data?.plan) },
    { label: 'Poster', done: byKind.poster.length > 0 },
    { label: 'Captions', done: byKind.caption.length > 0 },
    { label: 'Voice-over', done: byKind.voice.length > 0 },
    { label: 'Video', done: byKind.video.length > 0, doing: activeJobs.length > 0 },
    { label: 'Checked', done: all.length > 0 && open === 0 && failed === 0 },
    { label: 'Posted', done: posted.length > 0 },
  ];
  // The first unfinished stage is the one in progress — unless a later stage
  // is already done, in which case it was skipped rather than still running.
  const firstOpen = stages.findIndex((stage) => !stage.done);
  const current = stages.slice(firstOpen + 1).some((stage) => stage.done) ? -1 : firstOpen;
  const events = [...(data?.audit_events ?? [])].reverse();
  const rows = factRows(facts);

  return (
    <div className="ws-live">
      <div className="ws-page-head">
        <div>
          <BackLink onClose={onClose} />
          <span className="ws-kicker">Campaign #{campaignId} · live</span>
          <h1>{facts?.business?.name || 'Untitled campaign'}</h1>
          <p>
            Status <b>{data?.status?.replace(/_/g, ' ')}</b> · updates by itself
          </p>
        </div>
      </div>

      <ol className="ws-timeline" aria-label="Progress">
        {stages.map((stage, index) => {
          const state = stage.done ? 'done' : stage.doing || index === current ? 'doing' : 'todo';
          return (
            <li key={stage.label} className={`is-${state}`}>
              <span className="ws-timeline-mark">
                {state === 'done' ? <Check size={12} /> : state === 'doing' ? <span className="ws-pulse" /> : null}
              </span>
              {stage.label}
            </li>
          );
        })}
      </ol>

      <div className="ws-live-grid">
        <div className="ws-live-main">
          {data?.transcript ? (
            <div className="ws-card">
              <div className="ws-card-head">
                <h3>What was said</h3>
                <span className="ws-tag">{data.input_type === 'audio' ? 'spoken' : 'typed'}</span>
              </div>
              <div className="ws-readout">{data.transcript.raw}</div>
            </div>
          ) : null}

          {rows.length ? (
            <div className="ws-card">
              <div className="ws-card-head">
                <h3>The offer</h3>
                <span className={`ws-tag ${locked ? 'is-locked' : 'is-review'}`}>
                  {locked ? `locked · version ${factsheet.version}` : 'waiting for a spoken “yes”'}
                </span>
              </div>
              <dl className="ws-kv">
                {rows.map(([label, value]) => (
                  <React.Fragment key={label}>
                    <dt>{label}</dt>
                    <dd>{value}</dd>
                  </React.Fragment>
                ))}
              </dl>
            </div>
          ) : null}

          {plan.data?.plan ? (
            <div className="ws-card">
              <div className="ws-card-head">
                <h3>{fillTokens(plan.data.plan.strategy?.angle, tokens) || 'Campaign plan'}</h3>
                {plan.data.plan.model ? <span className="ws-tag">{plan.data.plan.model}</span> : null}
              </div>
              {plan.data.plan.strategy?.rationale ? (
                <p style={{ marginTop: 0, fontSize: 13.5, color: 'var(--muted)' }}>
                  {fillTokens(plan.data.plan.strategy.rationale, tokens)}
                </p>
              ) : null}
              <div style={{ display: 'grid', gap: 12 }}>
                {Object.entries(master).map(([channel, text]) => (
                  <div key={channel}>
                    <span className="ws-stat-label">{CHANNEL_LABELS[channel] ?? channel}</span>
                    <div className="ws-readout" style={{ marginTop: 5 }}>{text}</div>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {activeJobs.length ? (
            <div className="ws-card" aria-live="polite">
              <div className="ws-card-head">
                <h3>Rendering now</h3>
              </div>
              {activeJobs.map((job) => (
                <p key={job.id} style={{ margin: '0 0 6px', fontSize: 13.5 }}>
                  <span className="ws-pulse" style={{ marginRight: 8 }} />
                  {job.kind === 'ai_video' ? 'AI video' : 'Poster reel'}
                  {job.provider_status ? ` · ${job.provider_status}` : ''}
                  {typeof job.progress === 'number' ? ` · ${Math.round(job.progress)}%` : ''}
                </p>
              ))}
            </div>
          ) : null}

          <AssetGallery title="Posters" icon={ImageIcon} assets={byKind.poster} kind="poster" />
          <AssetGallery title="Videos" icon={Film} assets={byKind.video} kind="video" />
          <AssetGallery title="Voice-over" icon={Volume2} assets={byKind.voice} kind="voice" />
          <AssetGallery title="Captions" icon={FileImage} assets={byKind.caption} kind="caption" />
        </div>

        <aside className="ws-live-side">
          {all.length ? (
            <div className="ws-card">
              <div className="ws-card-head">
                <h3>Checks</h3>
              </div>
              <div className="ws-chips">
                <span className="ws-tag is-pass">{verified} verified</span>
                {open ? <span className="ws-tag is-review">{open} waiting</span> : null}
                {failed ? <span className="ws-tag is-fail">{failed} failed</span> : null}
              </div>
              <p style={{ margin: '10px 0 0', fontSize: 12.5, color: 'var(--muted)' }}>
                Every asset is compared with the locked offer. Only verified ones can be posted.
              </p>
            </div>
          ) : null}

          {(posts.data ?? []).length ? (
            <div className="ws-card">
              <div className="ws-card-head">
                <h3>Posts</h3>
              </div>
              <ul className="ws-feed">
                {posts.data.map((record) => (
                  <li key={record.id}>
                    <span className={`ws-timeline-mark ${record.status === 'PUBLISHED' ? 'is-ok' : record.status === 'FAILED' ? 'is-bad' : ''}`}>
                      {record.status === 'PUBLISHED' ? <Check size={11} /> : record.status === 'FAILED' ? <X size={11} /> : null}
                    </span>
                    <span>
                      <b>{CHANNEL_LABELS[record.destination] ?? record.destination}</b>
                      <small>{record.status.toLowerCase().replace(/_/g, ' ')}</small>
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <div className="ws-card">
            <div className="ws-card-head">
              <h3>Activity</h3>
              <span className="ws-tag">{events.length}</span>
            </div>
            {events.length === 0 ? (
              <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>Nothing has happened yet.</p>
            ) : (
              <ul className="ws-feed">
                {events.map((event) => (
                  <li key={event.id}>
                    <span className="ws-timeline-mark" />
                    <span>
                      <b>{EVENT_LABELS[event.event_type] ?? event.event_type.replace(/[._]/g, ' ')}</b>
                      <small>{clock(event.created_at)}</small>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}

function factRows(facts) {
  const business = facts?.business ?? {};
  const offer = facts?.offer ?? {};
  const rows = [];
  if (business.name) rows.push(['Business', business.name]);
  if ((offer.product ?? []).length) rows.push(['Offer', offer.product.join(', ')]);
  if (offer.discount_percent) rows.push(['Discount', `${offer.discount_percent}% off`]);
  if (offer.discount_flat) rows.push(['Flat off', String(offer.discount_flat)]);
  if (offer.price) rows.push(['Price', String(offer.price)]);
  if ((offer.audience ?? []).length) rows.push(['For', offer.audience.join(', ')]);
  if ((offer.days ?? []).length) rows.push(['Days', offer.days.join(', ')]);
  const time = [offer.start_time, offer.end_time].filter(Boolean).join(' – ');
  if (time) rows.push(['Time', time]);
  const dates = [offer.date_start, offer.date_end].filter(Boolean).join(' – ');
  if (dates) rows.push(['Dates', dates]);
  if (offer.location || business.location) rows.push(['Where', offer.location || business.location]);
  if ((offer.conditions ?? []).length) rows.push(['Conditions', offer.conditions.join(', ')]);
  if ((facts?.languages ?? []).length) rows.push(['Languages', facts.languages.join(', ')]);
  return rows;
}

function BackLink({ onClose }) {
  return (
    <button type="button" className="ws-btn is-ghost is-small" onClick={onClose} style={{ marginBottom: 12 }}>
      <ArrowLeft size={13} /> All campaigns
    </button>
  );
}
