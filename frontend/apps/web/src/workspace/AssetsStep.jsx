import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Clapperboard,
  FileImage,
  Film,
  Image as ImageIcon,
  Mic2,
  Play,
  RefreshCw,
  Trash2,
  Volume2,
  X,
} from 'lucide-react';
import {
  cancelVideoJob,
  createVoiceProfile,
  deleteVoiceProfile,
  generateAiVideo,
  generateCaptions,
  generatePosters,
  generateSpeech,
  generateVoice,
  getVoiceOptions,
  listVideoJobs,
  listVoiceProfiles,
  queueVideoJob,
  runVideoJob,
} from '../lib/api';
import { useAsyncAction, usePolling, useResource } from './hooks';
import {
  AssetAudio,
  AssetImage,
  AssetStatusBadge,
  AssetVideo,
  Banner,
  ErrorBanner,
  MockBadge,
  ProgressBar,
  Spinner,
} from './ui';

/**
 * Step E — asset generation.
 *
 * Posters, captions, speech and video each have an explicit trigger, their own
 * pending state, and their own failure message. Nothing is generated
 * implicitly, because every one of these costs real provider calls in live
 * mode. A provider failure is shown with its reason and a retry — it is never
 * papered over with a placeholder.
 */

const VOICE_LANGUAGES = ['English', 'Hindi', 'Kannada', 'Tamil', 'Telugu'];
const CHANNEL_LABELS = {
  voice_script: 'Voice script',
  whatsapp: 'WhatsApp message',
  instagram: 'Instagram caption',
  facebook: 'Facebook caption',
  reel_script: 'Reel script',
  x: 'X post',
  poster_headline: 'Poster headline',
  poster_subline: 'Poster subline',
};
const VIDEO_POLL_MS = 5000;

export default function AssetsStep({ campaignId, hasPlan, assets, onAssetsChanged, showToast }) {
  const posters = useAsyncAction();
  const captions = useAsyncAction();
  // One job list feeds both video panels, so they never disagree.
  const jobs = useResource((signal) => listVideoJobs(campaignId, signal), [campaignId], {
    enabled: hasPlan,
  });

  const byKind = useMemo(() => {
    const groups = { poster: [], caption: [], voice: [], video: [] };
    (assets ?? []).forEach((asset) => {
      (groups[asset.kind] ??= []).push(asset);
    });
    return groups;
  }, [assets]);

  if (!hasPlan) {
    return (
      <div className="ws-card">
        <Banner tone="warn" title="Generate the plan first">
          Assets are rendered from the campaign plan and the locked fact tokens. Complete step 3 first.
        </Banner>
      </div>
    );
  }

  const runPosters = async ({ allowFallbackArt = false } = {}) => {
    const result = await posters.run(() => generatePosters(campaignId, 1, { allowFallbackArt }));
    if (result.ok) {
      const fallback = result.data.some((poster) => poster.used_fallback);
      showToast(
        fallback
          ? 'Poster composed on fallback art (labelled).'
          : `Composed ${result.data.length} poster${result.data.length === 1 ? '' : 's'}.`,
      );
      onAssetsChanged();
    }
  };

  const runCaptions = async () => {
    const result = await captions.run(() => generateCaptions(campaignId));
    if (result.ok) {
      showToast(`Materialized ${result.data.length} captions.`);
      onAssetsChanged();
    }
  };

  // Offer labelled fallback art only when the image provider itself failed.
  const posterProviderFailed = posters.error?.status === 503;

  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <div className="ws-card">
        <div className="ws-card-head">
          <div>
            <span className="ws-kicker">Step 4 · Create</span>
            <h2>Generate the campaign assets</h2>
          </div>
        </div>
        <p style={{ marginTop: 0, fontSize: 13.5, color: 'var(--muted)' }}>
          Each asset records which fact version it was built from. If you change the facts later, assets
          bound to the old version are treated as stale and cannot be published.
        </p>
        <div className="ws-asset-foot">
          <button type="button" className="ws-btn is-primary" onClick={() => runPosters()} disabled={posters.pending}>
            {posters.pending ? <Spinner label="Generating art and composing…" /> : <><ImageIcon size={14} /> Compose a poster</>}
          </button>
          <button type="button" className="ws-btn" onClick={runCaptions} disabled={captions.pending}>
            {captions.pending ? <Spinner label="Writing…" /> : <><FileImage size={14} /> Materialize captions</>}
          </button>
        </div>
        {posters.pending ? (
          <small style={{ color: 'var(--muted)', display: 'block', marginTop: 8 }} role="status">
            Image generation can take a minute or two. Keep this tab open.
          </small>
        ) : null}
        <ErrorBanner
          error={posters.error}
          title="The poster was not created"
          action={
            <span style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              <button type="button" className="ws-btn is-ghost is-small" onClick={() => runPosters()} disabled={posters.pending}>
                Retry
              </button>
              {posterProviderFailed ? (
                <button
                  type="button"
                  className="ws-btn is-ghost is-small"
                  onClick={() => runPosters({ allowFallbackArt: true })}
                  disabled={posters.pending}
                  title="Uses a plain generated background, labelled as fallback art."
                >
                  Use fallback art
                </button>
              ) : null}
            </span>
          }
        />
        <ErrorBanner error={captions.error} title="Caption materialization failed" />
      </div>

      <SpeechPanel campaignId={campaignId} onChanged={onAssetsChanged} showToast={showToast} />
      <AiVideoPanel campaignId={campaignId} jobs={jobs} onChanged={onAssetsChanged} showToast={showToast} />

      <details>
        <summary style={{ cursor: 'pointer', fontSize: 13.5, padding: '4px 2px' }}>
          More options: narrate in your own cloned voice, or build a reel from the poster
        </summary>
        <div style={{ display: 'grid', gap: 16, marginTop: 12 }}>
          <VoicePanel campaignId={campaignId} onChanged={onAssetsChanged} showToast={showToast} />
          <ReelPanel
            campaignId={campaignId}
            jobs={jobs}
            hasPoster={byKind.poster.length > 0}
            onChanged={onAssetsChanged}
            showToast={showToast}
          />
        </div>
      </details>

      <AssetGallery title="Posters" icon={ImageIcon} assets={byKind.poster} kind="poster" />
      <AssetGallery title="Captions" icon={FileImage} assets={byKind.caption} kind="caption" />
      <AssetGallery title="Speech" icon={Volume2} assets={byKind.voice} kind="voice" />
      <AssetGallery title="Videos" icon={Film} assets={byKind.video} kind="video" />
    </div>
  );
}

/**
 * Sarvam text-to-speech.
 *
 * The text that is spoken is always a piece of the plan's copy, so it carries
 * only locked facts. The result is stored as a voice asset and played from the
 * gallery below; the most recent one is also shown here for immediate replay.
 */
function SpeechPanel({ campaignId, onChanged, showToast }) {
  const options = useResource((signal) => getVoiceOptions(signal), []);
  const speak = useAsyncAction();
  const [language, setLanguage] = useState('English');
  const [speaker, setSpeaker] = useState('');
  const [channel, setChannel] = useState('voice_script');
  const [latest, setLatest] = useState(null);

  const languages = options.data?.languages ?? VOICE_LANGUAGES;
  const speakers = options.data?.speakers ?? [];
  const channels = options.data?.channels ?? ['voice_script'];
  const effectiveSpeaker = speaker || options.data?.default_speaker || '';

  const handleSpeak = async () => {
    const result = await speak.run(() =>
      generateSpeech(campaignId, { language, speaker: effectiveSpeaker || undefined, channel }),
    );
    if (result.ok) {
      setLatest(result.data);
      showToast(`Speech generated in ${language}.`);
      onChanged();
    }
  };

  return (
    <div className="ws-card">
      <div className="ws-card-head">
        <h3>
          <Volume2 size={15} style={{ verticalAlign: -2, marginRight: 6 }} />
          Speech (Sarvam text-to-speech)
        </h3>
        {options.data?.model ? <span className="ws-tag">{options.data.model}</span> : null}
      </div>
      <p style={{ marginTop: 0, fontSize: 13.5, color: 'var(--muted)' }}>
        Turn a piece of the campaign copy into spoken audio. The words come from the plan, so every number
        is one you locked.
      </p>

      <ErrorBanner error={options.error} title="Speech options could not be loaded" />

      <div className="ws-grid cols-3" style={{ alignItems: 'end' }}>
        <div className="ws-field" style={{ marginBottom: 0 }}>
          <label htmlFor="tts-channel">Text to speak</label>
          <select id="tts-channel" value={channel} onChange={(event) => setChannel(event.target.value)}>
            {channels.map((item) => (
              <option key={item} value={item}>
                {CHANNEL_LABELS[item] ?? item}
              </option>
            ))}
          </select>
        </div>
        <div className="ws-field" style={{ marginBottom: 0 }}>
          <label htmlFor="tts-language">Language</label>
          <select id="tts-language" value={language} onChange={(event) => setLanguage(event.target.value)}>
            {languages.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </div>
        <div className="ws-field" style={{ marginBottom: 0 }}>
          <label htmlFor="tts-speaker">Voice</label>
          <select
            id="tts-speaker"
            value={effectiveSpeaker}
            onChange={(event) => setSpeaker(event.target.value)}
            disabled={!speakers.length}
          >
            {speakers.map((item) => (
              <option key={item} value={item}>
                {item.charAt(0).toUpperCase() + item.slice(1)}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="ws-asset-foot" style={{ marginTop: 14 }}>
        <button type="button" className="ws-btn is-primary" onClick={handleSpeak} disabled={speak.pending}>
          {speak.pending ? <Spinner label="Generating speech…" /> : <><Volume2 size={14} /> Generate speech</>}
        </button>
      </div>

      <ErrorBanner
        error={speak.error}
        title="Speech was not generated"
        action={
          <button type="button" className="ws-btn is-ghost is-small" onClick={handleSpeak} disabled={speak.pending}>
            Retry
          </button>
        }
      />

      {latest ? (
        <div style={{ marginTop: 14 }}>
          <div className="ws-chips" style={{ marginBottom: 8 }}>
            <span className="ws-tag">Latest · {latest.locale}</span>
            {latest.is_mock ? <MockBadge provider={latest.provider} /> : null}
          </div>
          <AssetAudio assetId={latest.id} label={`Speech ${latest.id}`} />
          {latest.is_mock ? (
            <small style={{ color: 'var(--muted)', display: 'block', marginTop: 6 }}>
              Mock mode plays a test tone, not a voice. Live mode returns Sarvam speech.
            </small>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

const JOB_STATUS_TEXT = {
  queued: 'Queued',
  running: 'Generating',
  validating: 'Downloading',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
};
const PROVIDER_STATUS_TEXT = { queued: 'waiting in the provider queue', in_progress: 'rendering' };

function jobTagClass(status) {
  if (status === 'completed') return 'is-pass';
  if (status === 'failed') return 'is-fail';
  if (status === 'cancelled') return 'is-review';
  return '';
}

/**
 * Agnes AI video.
 *
 * The provider is asynchronous: submitting returns a job, and the job list is
 * polled until it reaches a terminal state. All state is on the server, so
 * leaving the page or refreshing mid-generation loses nothing — the panel
 * picks the job up again on load.
 */
function AiVideoPanel({ campaignId, jobs, onChanged, showToast }) {
  const submit = useAsyncAction();
  const cancel = useAsyncAction();
  const [seconds, setSeconds] = useState(5);
  const [aspectRatio, setAspectRatio] = useState('9:16');

  const list = useMemo(() => (jobs.data ?? []).filter((job) => job.kind === 'ai_video'), [jobs.data]);
  const active = list.find((job) => job.active);

  usePolling(jobs.reload, { active: Boolean(active), intervalMs: VIDEO_POLL_MS });

  // Tell the parent exactly once per newly finished job, so the gallery picks
  // the video up without the user pressing reload.
  const announced = useRef(null);
  useEffect(() => {
    const finished = list.filter((job) => job.status === 'completed' && job.asset_id);
    if (announced.current === null) {
      announced.current = new Set(finished.map((job) => job.id)); // already there on load
      return;
    }
    const fresh = finished.filter((job) => !announced.current.has(job.id));
    if (!fresh.length) return;
    fresh.forEach((job) => announced.current.add(job.id));
    showToast('The AI video is ready.');
    onChanged();
  }, [list, onChanged, showToast]);

  const handleSubmit = async () => {
    const result = await submit.run(() => generateAiVideo(campaignId, { seconds, aspectRatio }));
    jobs.reload();
    if (result.ok) {
      if (result.data.status === 'failed') {
        showToast(result.data.error || 'The provider rejected the video request.', { error: true });
      } else {
        showToast('Video generation started.');
      }
    }
  };

  const handleCancel = async (jobId) => {
    const result = await cancel.run(() => cancelVideoJob(jobId));
    jobs.reload();
    if (result.ok) showToast('Stopped tracking that video job.');
  };

  return (
    <div className="ws-card">
      <div className="ws-card-head">
        <h3>
          <Clapperboard size={15} style={{ verticalAlign: -2, marginRight: 6 }} />
          AI video (Agnes)
        </h3>
        <button type="button" className="ws-btn is-ghost is-small" onClick={jobs.reload} disabled={jobs.loading}>
          <RefreshCw size={12} /> Refresh status
        </button>
      </div>
      <p style={{ marginTop: 0, fontSize: 13.5, color: 'var(--muted)' }}>
        Generates a short clip from the plan’s video brief. It runs in the background and usually takes a
        few minutes — you can leave this page and come back.
      </p>

      <ErrorBanner error={jobs.error} title="Video jobs could not be loaded" />

      <div className="ws-grid cols-3" style={{ alignItems: 'end' }}>
        <div className="ws-field" style={{ marginBottom: 0 }}>
          <label htmlFor="video-seconds">Length</label>
          <select id="video-seconds" value={seconds} onChange={(event) => setSeconds(Number(event.target.value))}>
            {[4, 5, 6, 8, 10, 12].map((value) => (
              <option key={value} value={value}>
                {value} seconds
              </option>
            ))}
          </select>
        </div>
        <div className="ws-field" style={{ marginBottom: 0 }}>
          <label htmlFor="video-ratio">Shape</label>
          <select id="video-ratio" value={aspectRatio} onChange={(event) => setAspectRatio(event.target.value)}>
            <option value="9:16">Vertical (9:16) — Reels, Status</option>
            <option value="1:1">Square (1:1)</option>
            <option value="16:9">Wide (16:9)</option>
          </select>
        </div>
        <button
          type="button"
          className="ws-btn is-primary"
          onClick={handleSubmit}
          disabled={submit.pending || Boolean(active)}
          title={active ? 'A video is already being generated.' : undefined}
        >
          {submit.pending ? <Spinner label="Submitting…" /> : <><Clapperboard size={14} /> Generate an AI video</>}
        </button>
      </div>

      <ErrorBanner
        error={submit.error}
        title="The video was not started"
        action={
          <button type="button" className="ws-btn is-ghost is-small" onClick={handleSubmit} disabled={submit.pending}>
            Retry
          </button>
        }
      />
      <ErrorBanner error={cancel.error} title="The job was not cancelled" />

      {jobs.loading && !jobs.data ? <div className="ws-skeleton" style={{ height: 40, marginTop: 14 }} /> : null}

      {list.length ? (
        <div style={{ marginTop: 14 }} aria-live="polite">
          {list.map((job) => (
            <div key={job.id} className="ws-job">
              <div className="ws-job-head">
                <span className="ws-mono">#{job.id}</span>
                <span className={`ws-tag ${jobTagClass(job.status)}`}>{JOB_STATUS_TEXT[job.status] ?? job.status}</span>
                {job.is_mock ? <MockBadge provider={job.provider} /> : null}
                <span style={{ color: 'var(--muted)', flex: 1 }}>
                  {job.active
                    ? [
                        PROVIDER_STATUS_TEXT[job.provider_status] ?? job.provider_status,
                        typeof job.progress === 'number' ? `${job.progress}%` : null,
                      ]
                        .filter(Boolean)
                        .join(' · ')
                    : job.error ||
                      (job.status === 'completed' ? `${job.seconds ?? ''}s · ${job.aspect_ratio ?? ''}` : '')}
                </span>
                {job.active ? (
                  <button
                    type="button"
                    className="ws-btn is-ghost is-small"
                    onClick={() => handleCancel(job.id)}
                    disabled={cancel.pending}
                  >
                    <X size={12} /> Stop
                  </button>
                ) : null}
              </div>
              {job.active ? (
                <ProgressBar
                  value={job.status === 'validating' ? null : job.progress || null}
                  label={`Video job ${job.id} progress`}
                />
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/** Voice profile management + localized cloned-voice narration. */
function VoicePanel({ campaignId, onChanged, showToast }) {
  const profiles = useResource((signal) => listVoiceProfiles(signal), [campaignId]);
  const create = useAsyncAction();
  const generate = useAsyncAction();
  const remove = useAsyncAction();

  const [displayName, setDisplayName] = useState('My shop voice');
  const [consent, setConsent] = useState(false);
  const [language, setLanguage] = useState('English');
  const [profileId, setProfileId] = useState(null);

  const list = profiles.data ?? [];

  useEffect(() => {
    if (!profileId && list.length) setProfileId(list[0].id);
  }, [list, profileId]);

  const handleCreate = async () => {
    const result = await create.run(() =>
      createVoiceProfile({
        displayName,
        consentConfirmed: consent,
        consentRecord: { source: 'web_workspace', confirmed_at: new Date().toISOString() },
        campaignId,
      }),
    );
    if (result.ok) {
      showToast('Voice profile created.');
      setConsent(false);
      profiles.reload();
    }
  };

  const handleGenerate = async () => {
    const result = await generate.run(() => generateVoice(campaignId, { profileId, language }));
    if (result.ok) {
      showToast(`Narration generated in ${language}.`);
      onChanged();
    }
  };

  const handleDelete = async (id) => {
    const result = await remove.run(() => deleteVoiceProfile(id));
    if (result.ok) {
      showToast('Voice profile deleted.');
      if (profileId === id) setProfileId(null);
      profiles.reload();
    }
  };

  return (
    <div className="ws-card">
      <div className="ws-card-head">
        <h3>
          <Mic2 size={15} style={{ verticalAlign: -2, marginRight: 6 }} />
          Cloned-voice narration
        </h3>
        <button type="button" className="ws-btn is-ghost is-small" onClick={profiles.reload}>
          <RefreshCw size={12} /> Refresh
        </button>
      </div>

      <ErrorBanner error={profiles.error} title="Voice profiles could not be loaded" />
      <ErrorBanner error={create.error} title="The voice profile was not created" />
      <ErrorBanner error={generate.error} title="Narration failed" />
      <ErrorBanner error={remove.error} title="The profile was not deleted" />

      {profiles.loading && !profiles.data ? (
        <div className="ws-skeleton" style={{ height: 40 }} />
      ) : list.length === 0 ? (
        <>
          <p style={{ marginTop: 0, fontSize: 13.5, color: 'var(--muted)' }}>
            A voice profile is reused across campaigns. Recorded consent is stored with it.
          </p>
          <div className="ws-field">
            <label htmlFor="voice-name">Profile name</label>
            <input
              id="voice-name"
              type="text"
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
            />
          </div>
          <div className="ws-field">
            <label htmlFor="voice-consent" style={{ display: 'flex', gap: 9, alignItems: 'flex-start' }}>
              <input
                id="voice-consent"
                type="checkbox"
                checked={consent}
                onChange={(event) => setConsent(event.target.checked)}
                style={{ marginTop: 3 }}
              />
              <span>
                I confirm this voice is mine (or I have the speaker’s permission) and may be used to narrate
                my campaigns.
              </span>
            </label>
          </div>
          <button
            type="button"
            className="ws-btn"
            onClick={handleCreate}
            disabled={create.pending || !consent || !displayName.trim()}
          >
            {create.pending ? <Spinner label="Creating…" /> : 'Create voice profile'}
          </button>
        </>
      ) : (
        <>
          <div className="ws-grid cols-3" style={{ alignItems: 'end' }}>
            <div className="ws-field" style={{ marginBottom: 0 }}>
              <label htmlFor="voice-profile">Profile</label>
              <select
                id="voice-profile"
                value={profileId ?? ''}
                onChange={(event) => setProfileId(Number(event.target.value))}
              >
                {list.map((profile) => (
                  <option key={profile.id} value={profile.id}>
                    {profile.display_name}
                    {profile.is_mock ? ' (demo voice)' : ''}
                  </option>
                ))}
              </select>
            </div>
            <div className="ws-field" style={{ marginBottom: 0 }}>
              <label htmlFor="voice-language">Language</label>
              <select id="voice-language" value={language} onChange={(event) => setLanguage(event.target.value)}>
                {VOICE_LANGUAGES.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </div>
            <button
              type="button"
              className="ws-btn"
              onClick={handleGenerate}
              disabled={generate.pending || !profileId}
            >
              {generate.pending ? <Spinner label="Generating…" /> : 'Generate narration'}
            </button>
          </div>

          <ul style={{ listStyle: 'none', padding: 0, margin: '14px 0 0', display: 'grid', gap: 8 }}>
            {list.map((profile) => (
              <li
                key={profile.id}
                style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 13 }}
              >
                <b style={{ flex: 1 }}>{profile.display_name}</b>
                {profile.is_mock ? <MockBadge provider={profile.provider} /> : null}
                <span className="ws-tag">{profile.provider}</span>
                <button
                  type="button"
                  className="ws-btn is-ghost is-small"
                  onClick={() => handleDelete(profile.id)}
                  disabled={remove.pending}
                  aria-label={`Delete ${profile.display_name}`}
                >
                  <Trash2 size={12} />
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

/**
 * Reel composition.
 *
 * Composition runs through the backend's job table, so the UI queues a job and
 * then drives it rather than blocking on one long request.
 */
function ReelPanel({ campaignId, jobs, hasPoster, onChanged, showToast }) {
  const queue = useAsyncAction();
  const drive = useAsyncAction();

  const list = (jobs.data ?? []).filter((job) => job.kind === 'reel');
  const active = list.find((job) => job.status === 'queued' || job.status === 'running');

  const handleQueue = async () => {
    const result = await queue.run(() => queueVideoJob(campaignId));
    if (result.ok) {
      showToast('Reel job queued.');
      jobs.reload();
    }
  };

  const handleRun = async (jobId) => {
    const result = await drive.run(() => runVideoJob(jobId));
    jobs.reload();
    if (result.ok) {
      if (result.data.asset_id) {
        showToast('Reel composed.');
        onChanged();
      } else {
        showToast(result.data.job?.error || 'The reel job did not produce a video.', { error: true });
      }
    }
  };

  return (
    <div className="ws-card">
      <div className="ws-card-head">
        <h3>
          <Film size={15} style={{ verticalAlign: -2, marginRight: 6 }} />
          Poster reel (local FFmpeg)
        </h3>
        <button type="button" className="ws-btn is-ghost is-small" onClick={jobs.reload}>
          <RefreshCw size={12} /> Refresh jobs
        </button>
      </div>

      {!hasPoster ? (
        <Banner tone="warn">Compose a poster first — the reel animates it.</Banner>
      ) : (
        <p style={{ marginTop: 0, fontSize: 13.5, color: 'var(--muted)' }}>
          The reel is built locally with FFmpeg from the poster plus any generated speech. If FFmpeg is not
          installed on the server, the job fails with that reason instead of producing a placeholder.
        </p>
      )}

      <ErrorBanner error={queue.error} title="The reel job was not queued" />
      <ErrorBanner error={drive.error} title="The reel job failed" />

      <div className="ws-asset-foot">
        <button type="button" className="ws-btn" onClick={handleQueue} disabled={!hasPoster || queue.pending}>
          {queue.pending ? <Spinner label="Queueing…" /> : 'Queue a reel'}
        </button>
        {active ? (
          <button
            type="button"
            className="ws-btn is-primary"
            onClick={() => handleRun(active.id)}
            disabled={drive.pending}
          >
            {drive.pending ? <Spinner label="Composing…" /> : <><Play size={13} /> Run job #{active.id}</>}
          </button>
        ) : null}
      </div>

      {list.length ? (
        <ul style={{ listStyle: 'none', padding: 0, margin: '14px 0 0', display: 'grid', gap: 7 }}>
          {list.map((job) => (
            <li key={job.id} style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 12.5 }}>
              <span className="ws-mono">#{job.id}</span>
              <span className={`ws-tag ${jobTagClass(job.status)}`}>{JOB_STATUS_TEXT[job.status] ?? job.status}</span>
              <span style={{ color: 'var(--muted)', flex: 1 }}>
                {job.error || `attempts: ${job.attempts}`}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** Previews for one asset kind, with per-asset provenance. */
export function AssetGallery({ title, icon, assets, kind }) {
  if (!assets?.length) return null;
  const Icon = icon;
  return (
    <div className="ws-card">
      <div className="ws-card-head">
        <h3>
          <Icon size={15} style={{ verticalAlign: -2, marginRight: 6 }} />
          {title}
        </h3>
        <span className="ws-tag">{assets.length}</span>
      </div>
      <div className={kind === 'poster' ? 'ws-grid cols-3 is-gallery' : 'ws-grid cols-2'}>
        {assets.map((asset) => (
          <article key={asset.id} className="ws-asset">
            {kind === 'poster' ? <AssetImage assetId={asset.id} alt={`Poster ${asset.id}`} /> : null}
            {kind === 'video' ? (
              <AssetVideo assetId={asset.id} playable={asset.provenance?.playable !== false} />
            ) : null}
            <div className="ws-asset-body">
              <header>
                <span className="ws-mono">#{asset.id}</span>
                <span className="ws-tag">{asset.locale}</span>
                <AssetStatusBadge status={asset.asset_status} />
                {asset.is_mock ? <MockBadge provider={asset.provider} /> : null}
                {asset.used_fallback ? <span className="ws-tag is-review">Fallback art</span> : null}
                {!asset.is_mock && asset.model ? <span className="ws-tag">{asset.model}</span> : null}
              </header>
              {kind === 'voice' ? <AssetAudio assetId={asset.id} label={`Speech ${asset.id}`} /> : null}
              {asset.text_content ? <p className="ws-asset-text">{asset.text_content}</p> : null}
              {asset.provenance?.art_error ? (
                <small style={{ color: 'var(--muted)' }}>
                  Image model unavailable when this was made: {asset.provenance.art_error}
                </small>
              ) : null}
              {asset.provenance?.channel ? (
                <small style={{ color: 'var(--muted)' }}>
                  {kind === 'voice' ? 'Speaks' : 'Channel'}: {CHANNEL_LABELS[asset.provenance.channel] ?? asset.provenance.channel}
                  {asset.provenance.speaker ? ` · voice ${asset.provenance.speaker}` : ''}
                </small>
              ) : null}
              <small style={{ color: 'var(--muted)' }} className="ws-hash">
                {asset.fact_hash}
              </small>
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}
