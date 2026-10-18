import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  AudioLines, Check, Copy, Download, ExternalLink, History, LayoutGrid, LogOut, Mic, Plus, Share2, Sparkles, X,
} from 'lucide-react';
import {
  createAudioCampaign, createPlan, errorMessage, exportCampaign, fetchAssetObjectUrl,
  generateAiVideo, generateBragVideo, generateCaptions, generatePosters, generateSpeech, getCampaign,
  humanVerifyAsset, listAssets, listCampaigns, listVideoJobs, loadSession, lockFactSheet,
  openSttStream, postCampaignSocial, publishTurn, socialConnect, socialStatus, verifyCampaign, voiceTurn,
} from '../lib/api';
import { useAuth } from '../AuthContext';
import ThemeToggle from '../ThemeToggle';
import Wordmark from '../Wordmark.jsx';
import VoicePill from '../voice/VoicePill';
import { preferredMimeType, useMicRecorder } from '../voice/useMicRecorder';
import '../auth.css';
import './voice-workspace.css';

/**
 * The shopkeeper's workspace: a microphone and a thread. Voice only.
 *
 * 1. Hold the mic and say the offer — the words appear live as you speak.
 * 2. The facts come back with their validation. Hold the mic again and say
 *    "yes", or say what to change ("make it 25 percent").
 * 3. On "yes" the whole package is made without another touch: captions,
 *    a designed poster, voice-over, a campaign video and an AI video. The
 *    slow pieces run side by side, and the wait shows what is being made.
 *
 * 4. When the package is ready it asks, by voice, whether to post it on the
 *    owner's social media. "Yes" links their accounts (once) and posts the
 *    poster and caption; "no" ends the conversation and returns home.
 *
 * Everything it creates is an ordinary campaign, so the full studio
 * (`/studio`) can open the same offer to inspect every detail.
 */

const VIDEO_POLL_MS = 5000;
const CONNECT_POLL_MS = 4000;
const GOODBYE_MS = 2600;
const PLATFORM_NAMES = { instagram: 'Instagram', facebook: 'Facebook', x: 'X' };

/** The package, in the order it is shown. `after` = stages that must finish first. */
const STAGES = [
  { key: 'lock', label: 'Locking your offer', doing: 'Sealing the facts so nothing can change them' },
  { key: 'plan', label: 'Planning the campaign', doing: 'Agnes is choosing the angle and writing your posts' },
  { key: 'captions', label: 'Captions', doing: 'Filling your posts with the locked facts' },
  { key: 'aivideo', label: 'AI video', doing: 'Sent to render — it finishes in the background', optional: true },
  { key: 'poster', label: 'Poster', doing: 'Painting the artwork while the Brag Director picks colours and a hook' },
  { key: 'voice', label: 'Voice-over', doing: 'Recording your offer in a natural voice', optional: true },
  { key: 'brag', label: 'Campaign video', doing: 'Drawing the frames and mixing in the voice-over', optional: true },
  { key: 'verify', label: 'Checking every number', doing: 'Comparing every asset with your locked offer' },
];

/** Shown one at a time while the package is being made. */
const WAIT_NOTES = [
  'Every number on your poster is copied from the offer you confirmed — the AI never types a fact.',
  'The Brag Director (Agnes 3.0 Flash) is art-directing: colours, type and an opening line made for your shop.',
  'Your campaign video is drawn frame by frame, so it comes out identical every time.',
  'The poster artwork is painted without any text; your offer is set on top afterwards so it stays readable.',
  'Captions are ready first — you can read them while the visuals finish.',
  'The AI video keeps rendering after everything else is done and appears here by itself.',
];

/** Why transcription failed, from the create response or the audit trail. */
function sttFailureReason(campaign) {
  if (!campaign || campaign.transcript?.raw || campaign.input_type !== 'audio') return '';
  const fromAudit = [...(campaign.audit_events ?? [])]
    .reverse()
    .find((event) => event.event_type === 'stt.unavailable')?.payload?.message;
  return campaign.stt?.message || fromAudit || 'Speech-to-text was unavailable.';
}

/** The findings worth showing in the thread: rule errors, then warnings. */
function topFindings(validation) {
  const all = [
    ...(validation?.deterministic?.findings ?? []),
    ...(validation?.semantic && !validation.semantic.stale ? validation.semantic.findings ?? [] : []),
  ].filter((item) => item.severity !== 'info' && item.code !== 'missing_required');
  const rank = { error: 0, warning: 1 };
  return all.sort((a, b) => rank[a.severity] - rank[b.severity]).slice(0, 3);
}

function summarize(facts) {
  const business = facts?.business ?? {};
  const offer = facts?.offer ?? {};
  const rows = [];
  if (business.name) rows.push({ label: 'Business', value: business.name });
  const product = (offer.product ?? []).filter(Boolean);
  if (product.length) rows.push({ label: 'Offer', value: product.join(', ') });
  if (offer.discount_percent) rows.push({ label: 'Discount', value: `${offer.discount_percent}% off` });
  if (offer.discount_flat) rows.push({ label: 'Discount', value: `${offer.discount_flat} off` });
  if (offer.price) rows.push({ label: 'Price', value: String(offer.price) });
  if ((offer.audience ?? []).length) rows.push({ label: 'For', value: offer.audience.join(', ') });
  if ((offer.days ?? []).length) rows.push({ label: 'Days', value: offer.days.join(', ') });
  const time = [offer.start_time, offer.end_time].filter(Boolean).join(' – ');
  if (time) rows.push({ label: 'Time', value: time });
  if (offer.location) rows.push({ label: 'Where', value: offer.location });
  if ((offer.conditions ?? []).length) rows.push({ label: 'Conditions', value: offer.conditions.join(', ') });
  return rows;
}

function shortLabel(campaign) {
  const name = campaign.factsheet?.facts?.business?.name;
  if (name) return name;
  return campaign.input_type === 'audio' ? 'Voice note' : 'Typed offer';
}

/** One line for the validator's verdict on the drafted facts. */
function validationVerdict(validation) {
  if (!validation) return null;
  const semantic = validation.semantic;
  if (validation.status === 'failed' || validation.status === 'invalid') {
    return { good: false, text: 'Something here does not add up — see below.' };
  }
  if (semantic?.status === 'unavailable') {
    return { good: false, text: 'Rules checked. The double-check against your words could not run, so listen carefully.' };
  }
  if (semantic?.stale) return { good: true, text: 'Rules checked.' };
  return { good: true, text: 'Checked against what you said.' };
}

export default function VoiceWorkspace() {
  const { user, checking, logout } = useAuth();
  const navigate = useNavigate();
  const mic = useMicRecorder();

  const [sessions, setSessions] = useState([]);
  const [session, setSession] = useState(null);
  const [assets, setAssets] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [media, setMedia] = useState({}); // asset id → object URL
  const [turns, setTurns] = useState([]); // spoken replies after the offer
  const [busy, setBusy] = useState('');
  // Package progress: stage key → { status: 'doing'|'done'|'failed', startedAt, ms, detail }.
  const [stages, setStages] = useState({});
  const [making, setMaking] = useState(null); // { startedAt } while the package is being made
  const [now, setNow] = useState(() => Date.now());
  const [error, setError] = useState('');
  const [historyOpen, setHistoryOpen] = useState(false);
  // Posting to social media: null until the question is answered, then
  // { phase: 'connect'|'done'|'declined'|'unavailable', … }.
  const [publish, setPublish] = useState(null);
  const [publishSaid, setPublishSaid] = useState([]); // spoken answers to the posting question
  const goodbyeRef = useRef(null);
  // Live transcription while the speaker is still talking.
  const [liveText, setLiveText] = useState('');
  const [liveNote, setLiveNote] = useState('');
  const [liveIsMock, setLiveIsMock] = useState(false);
  const streamRef = useRef(null);
  const threadRef = useRef(null);
  const mediaRef = useRef({});

  // A signed-out visitor must land on the login page — rendering `null` here
  // would leave a blank screen with no way forward.
  useEffect(() => {
    if (!checking && !user) navigate('/login', { replace: true });
  }, [checking, user, navigate]);

  const loadSessions = useCallback(async (signal) => {
    try {
      setSessions(await listCampaigns({ signal }));
    } catch {
      /* the offline banner already covers this */
    }
  }, []);

  useEffect(() => {
    // Nothing to fetch until there is a session; this also avoids a stray
    // 401 while a signed-out visitor is being redirected to /login.
    if (!user) return undefined;
    const controller = new AbortController();
    loadSessions(controller.signal);
    return () => controller.abort();
  }, [loadSessions, user]);

  const clearMedia = useCallback(() => {
    Object.values(mediaRef.current).forEach((url) => URL.revokeObjectURL(url));
    mediaRef.current = {};
    setMedia({});
  }, []);

  useEffect(() => () => clearMedia(), [clearMedia]);

  const resetThread = useCallback(() => {
    setAssets([]);
    setJobs([]);
    setTurns([]);
    setStages({});
    setMaking(null);
    setError('');
    setBusy('');
    setPublish(null);
    setPublishSaid([]);
    clearMedia();
  }, [clearMedia]);

  const openSession = useCallback(async (campaign) => {
    setHistoryOpen(false);
    resetThread();
    setSession(campaign);
    try {
      const fresh = await getCampaign(campaign.id);
      setSession(fresh);
      setAssets(await listAssets(fresh.id));
      setJobs(await listVideoJobs(fresh.id).catch(() => []));
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [resetThread]);

  const startNew = useCallback(() => {
    setSession(null);
    resetThread();
    setHistoryOpen(false);
  }, [resetThread]);

  // Asset bytes need the bearer token, so <img>/<video>/<audio> cannot fetch
  // them by URL: each playable asset is turned into an object URL once.
  useEffect(() => {
    const wanted = assets.filter(
      // A mock video is an empty container; everything else is playable.
      (a) => ['poster', 'video', 'voice'].includes(a.kind) && !(a.kind === 'video' && a.is_mock) && !mediaRef.current[a.id],
    );
    if (!wanted.length) return;
    wanted.forEach((asset) => {
      mediaRef.current[asset.id] = 'pending';
      fetchAssetObjectUrl(asset.id)
        .then((url) => {
          if (mediaRef.current[asset.id] !== 'pending') {
            URL.revokeObjectURL(url);
            return;
          }
          mediaRef.current[asset.id] = url;
          setMedia((prev) => ({ ...prev, [asset.id]: url }));
        })
        .catch(() => {
          delete mediaRef.current[asset.id];
        });
    });
  }, [assets]);

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: 'smooth' });
    // The page itself scrolls when the thread is taller than the window.
    threadRef.current?.lastElementChild?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [busy, stages, session, assets, turns, jobs, liveText, publish, publishSaid]);

  // ── video jobs: polling is what advances an AI video to "done" ────────
  const sessionId = session?.id;
  const sessionIdRef = useRef(null);
  sessionIdRef.current = sessionId;
  const videoActive = jobs.some((job) => job.active);
  useEffect(() => {
    if (!sessionId || !videoActive) return undefined;
    let cancelled = false;
    const timer = setInterval(async () => {
      try {
        const next = await listVideoJobs(sessionId);
        if (cancelled) return;
        setJobs(next);
        if (!next.some((job) => job.active)) {
          // A finished video is a new asset: check it, then show it. Setting
          // the jobs above ends this effect, so "still the same offer" is
          // read from a ref rather than from `cancelled`.
          await verifyCampaign(sessionId).catch(() => {});
          const fresh = await listAssets(sessionId);
          if (sessionIdRef.current === sessionId) setAssets(fresh);
        }
      } catch {
        /* a failed poll is retried on the next tick */
      }
    }, VIDEO_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [sessionId, videoActive]);

  // A clock for the waiting view (elapsed time, rotating notes).
  useEffect(() => {
    if (!making) return undefined;
    const timer = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(timer);
  }, [making]);

  // ── the package: runs by itself once the facts are confirmed ──────────
  const makePackage = useCallback(async (campaign) => {
    const sheetId = campaign?.factsheet?.id;
    if (!sheetId) return;
    const id = campaign.id;
    setError('');
    setStages({});
    setMaking({ startedAt: Date.now() });
    const language = campaign.factsheet?.facts?.languages?.[0] || 'English';
    const refresh = () => listAssets(id).then(setAssets).catch(() => {});

    /** Run one stage and record how it went. Resolves true on success. */
    const stage = async (key, run) => {
      const startedAt = Date.now();
      setStages((prev) => ({ ...prev, [key]: { status: 'doing', startedAt } }));
      try {
        await run();
        setStages((prev) => ({ ...prev, [key]: { status: 'done', startedAt, ms: Date.now() - startedAt } }));
        refresh(); // show each piece as soon as it exists
        return true;
      } catch (err) {
        setStages((prev) => ({
          ...prev,
          [key]: { status: 'failed', startedAt, ms: Date.now() - startedAt, detail: errorMessage(err), missing: err?.details?.missing },
        }));
        return false;
      }
    };
    const stop = (key, missing) => {
      setError(
        missing?.length
          ? `I still need: ${missing.join(', ').replaceAll('offer.', '').replaceAll('_', ' ')}. Hold the mic and tell me.`
          : `${STAGES.find((item) => item.key === key).label} did not finish. Hold the mic and say “yes” to try again.`,
      );
    };

    let finished = false;
    try {
      // 1 — the two steps everything else depends on.
      let lockMissing;
      if (!(await stage('lock', () => lockFactSheet(sheetId).catch((err) => { lockMissing = err?.details?.missing; throw err; })))) {
        stop('lock', lockMissing);
        return;
      }
      if (!(await stage('plan', () => createPlan(id)))) { stop('plan'); return; }

      // 2 — everything that only needs the plan, side by side.
      const [captionsOk, , posterOk] = await Promise.all([
        stage('captions', () => generateCaptions(id)),
        stage('aivideo', async () => {
          const job = await generateAiVideo(id, { seconds: 5, aspectRatio: '9:16' });
          setJobs((prev) => [job, ...prev.filter((j) => j.id !== job.id)]);
          if (job.status === 'failed') throw new Error(job.error || 'The video could not be started.');
        }),
        stage('poster', () => generatePosters(id, 1)),
        stage('voice', () => generateSpeech(id, { language })
          .catch((err) => (language === 'English' ? Promise.reject(err) : generateSpeech(id, { language: 'English' })))),
      ]);
      if (!captionsOk) { stop('captions'); return; }
      if (!posterOk) { stop('poster'); return; }

      // 3 — the campaign video uses the poster's artwork and the voice-over.
      await stage('brag', () => generateBragVideo(id));
      await stage('verify', () => verifyCampaign(id));
      finished = true;
    } finally {
      setMaking(null);
      try {
        setSession(await getCampaign(id));
        // Jobs first: reading them is what turns a finished AI video into an
        // asset, and a video that landed after the last check still needs one.
        if (finished) {
          setJobs(await listVideoJobs(id));
          const fresh = await listAssets(id);
          if (fresh.some((a) => a.asset_status === 'validating')) await verifyCampaign(id).catch(() => {});
        }
        setAssets(await listAssets(id));
      } catch (err) {
        setError(errorMessage(err));
      }
      loadSessions();
    }
  }, [loadSessions]);

  const acceptResults = useCallback(async (campaignId, current) => {
    // Only assets Guardian actually reached a verdict on can be accepted;
    // human-verify refuses anything unverified.
    const pending = current.filter((a) => a.asset_status === 'needs_review');
    if (!pending.length) return;
    setBusy('Saving your review…');
    try {
      for (const asset of pending) {
        await humanVerifyAsset(asset.id, 'Confirmed by voice in the workspace.');
      }
      setAssets(await listAssets(campaignId));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy('');
    }
  }, []);

  // ── posting to the owner's social media ───────────────────────────────
  useEffect(() => () => clearTimeout(goodbyeRef.current), []);

  const postNow = useCallback(async (campaignId) => {
    setBusy('Posting your campaign…');
    try {
      const out = await postCampaignSocial(campaignId);
      setPublish({ phase: 'done', results: out.results, isMock: out.is_mock });
    } catch (err) {
      setPublish(null); // the question stays open, so "yes" tries again
      setError(`${errorMessage(err)} Say “yes” to try again, or “no” to finish.`);
    } finally {
      setBusy('');
    }
  }, []);

  /** A spoken "yes": post straight away, or first ask the owner to sign in. */
  const startPublish = useCallback(async (campaignId) => {
    setBusy('Checking your social accounts…');
    try {
      const status = await socialStatus();
      if (!status.configured) {
        setPublish({ phase: 'unavailable' });
        return;
      }
      if (status.accounts.some((account) => account.connected)) {
        await postNow(campaignId);
        return;
      }
      const link = await socialConnect(`${window.location.origin}/connected`);
      setPublish({ phase: 'connect', accessUrl: link.access_url, accounts: status.accounts });
      // Usually blocked (this is not a direct click); the button below is the fallback.
      window.open(link.access_url, '_blank', 'noopener');
    } catch (err) {
      setPublish(null);
      setError(`${errorMessage(err)} Say “yes” to try again, or “no” to finish.`);
    } finally {
      setBusy((current) => (current.startsWith('Checking') ? '' : current));
    }
  }, [postNow]);

  /** A spoken "no": end the conversation and go back to the home page. */
  const declinePublish = useCallback(() => {
    setPublish({ phase: 'declined' });
    goodbyeRef.current = setTimeout(() => navigate('/'), GOODBYE_MS);
  }, [navigate]);

  // While the owner is signing in on the connect page, watch for the first
  // linked account and then post — they already said yes.
  const connecting = publish?.phase === 'connect';
  useEffect(() => {
    if (!connecting || !sessionId) return undefined;
    let cancelled = false;
    const timer = setInterval(async () => {
      try {
        const status = await socialStatus();
        if (cancelled) return;
        if (status.accounts.some((account) => account.connected)) {
          clearInterval(timer);
          await postNow(sessionId);
        } else {
          setPublish((prev) => (prev?.phase === 'connect' ? { ...prev, accounts: status.accounts } : prev));
        }
      } catch {
        /* a failed check is retried on the next tick */
      }
    }, CONNECT_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [connecting, sessionId, postNow]);

  // ── derived state ─────────────────────────────────────────────────────
  const facts = session?.factsheet?.facts;
  const rows = useMemo(() => summarize(facts), [facts]);
  const sttReason = sttFailureReason(session);
  const validation = session?.factsheet?.validation;
  const findings = useMemo(() => topFindings(validation), [validation]);
  const verdict = useMemo(() => validationVerdict(validation), [validation]);
  const studioHref = session ? `/studio?campaign=${session.id}` : '/studio';
  const locked = session?.factsheet?.status === 'locked' || session?.factsheet?.status === 'superseded';
  const captions = assets.filter((a) => a.kind === 'caption' && a.text_content);
  const master = captions.find((c) => c.locale === 'en-IN' && c.provenance?.channel === 'instagram')
    ?? captions.find((c) => c.locale === 'en-IN') ?? captions[0];
  const poster = assets.find((a) => a.kind === 'poster');
  const voice = assets.find((a) => a.kind === 'voice');
  const videos = assets.filter((a) => a.kind === 'video');
  const aiJob = jobs.find((job) => job.kind === 'ai_video');
  const needsReview = assets.some((a) => a.asset_status === 'needs_review');
  const reviewing = Boolean(session?.factsheet) && rows.length > 0 && !locked;
  const awaitingAccept = Boolean(poster) && needsReview;
  // A locked offer whose package was interrupted can be resumed by voice.
  const resumable = locked && !poster && rows.length > 0;
  const ready = Boolean(poster) && !needsReview && !videoActive && !busy && !making;
  // The poster and caption are what gets posted, so the question does not
  // wait for the AI video that is still rendering.
  const canPost = Boolean(poster) && locked && !needsReview && !making;
  const askingPublish = canPost && (!publish || publish.phase === 'connect');
  const bragVideo = videos.find((v) => v.provider === 'brag_director');
  const aiVideo = videos.find((v) => v.provider !== 'brag_director' && v.provider !== 'ffmpeg');
  const reel = videos.find((v) => v.provider === 'ffmpeg');
  const stageList = STAGES.map((item) => ({ ...item, ...(stages[item.key] ?? { status: 'todo' }) }));
  const doneCount = stageList.filter((item) => item.status === 'done' || item.status === 'failed').length;
  const elapsed = making ? Math.max(0, Math.round((now - making.startedAt) / 1000)) : 0;
  const waitNote = WAIT_NOTES[Math.floor(elapsed / 4) % WAIT_NOTES.length];
  const showPackage = Boolean(making) || Boolean(poster);
  const socialCaptions = ['instagram', 'whatsapp']
    .map((channel) => captions.find((c) => c.locale === 'en-IN' && c.provenance?.channel === channel))
    .filter(Boolean);

  // What the next spoken reply means. Read through a ref so the recorder
  // callbacks always see the current state.
  const stageRef = useRef({});
  stageRef.current = { session, assets, reviewing, awaitingAccept, resumable, askingPublish };

  // ── recording ─────────────────────────────────────────────────────────
  const closeStream = useCallback(() => {
    streamRef.current?.close();
    streamRef.current = null;
  }, []);

  useEffect(() => () => closeStream(), [closeStream]);

  const handleStart = useCallback(async () => {
    setLiveText('');
    setLiveNote('');
    setError('');
    await mic.start({ onChunk: (buffer) => streamRef.current?.send(buffer) });
    // Open the preview only once the microphone is actually live.
    streamRef.current = openSttStream({
      token: loadSession()?.token,
      mime: preferredMimeType() || 'audio/webm',
      onEvent: (event) => {
        if (event.type === 'ready') {
          setLiveIsMock(Boolean(event.is_mock));
        } else if (event.type === 'partial' || event.type === 'final') {
          if (event.text) setLiveText(event.text);
        } else if (event.type === 'unavailable') {
          setLiveNote(event.reason || '');
        } else if (event.type === 'error') {
          setLiveNote(event.message || '');
        }
      },
    });
  }, [mic]);

  const handleStop = useCallback(
    async ({ reason }) => {
      if (reason === 'cancel' || reason === 'escape') {
        mic.cancel();
        closeStream();
        setLiveText('');
        setLiveNote('');
        return;
      }
      // 'unmount'/'disabled'/'error' mean nothing was captured to save.
      if (reason === 'unmount' || reason === 'disabled' || reason === 'error') return;

      setError('');
      const stream = streamRef.current;
      streamRef.current = null;
      stream?.stop(); // preview is done; the clip below is authoritative
      const blob = await mic.stop();
      if (!blob) {
        stream?.close();
        setLiveText('');
        setError('Nothing was recorded. Hold the mic and speak.');
        return;
      }
      const stage = stageRef.current;
      const answering = stage.reviewing || stage.awaitingAccept || stage.resumable;
      try {
        if (stage.askingPublish) {
          // The answer to "shall I post this for you?".
          setBusy('Listening to your answer…');
          const turn = await publishTurn(stage.session.id, { blob, mimeType: blob.type });
          setPublishSaid((prev) => [...prev, { heard: turn.heard, reply: turn.intent === 'unclear' ? turn.reply : '' }]);
          if (turn.intent === 'yes') await startPublish(stage.session.id);
          else if (turn.intent === 'no') declinePublish();
        } else if (answering) {
          // A spoken reply to what is on screen: yes / a correction / start over.
          setBusy('Listening to your answer…');
          const turn = await voiceTurn(stage.session.id, { blob, mimeType: blob.type });
          setTurns((prev) => [...prev, { heard: turn.heard, reply: turn.reply, intent: turn.intent, changed: turn.changed }]);
          if (turn.campaign) setSession(turn.campaign);
          if (turn.intent === 'restart') {
            startNew();
          } else if (turn.intent === 'confirm') {
            if (stage.awaitingAccept) await acceptResults(stage.session.id, stage.assets);
            else await makePackage(turn.campaign ?? stage.session);
          }
        } else {
          // A new offer (also after a finished package, or a clip that could
          // not be understood).
          resetThread();
          setSession(null);
          setBusy('Transcribing what you said…');
          const created = await createAudioCampaign({ blob, mimeType: blob.type });
          setSession(created);
          setAssets(created.assets ?? []);
          loadSessions();
        }
      } catch (err) {
        setError(errorMessage(err));
      } finally {
        stream?.close();
        setLiveText('');
        setLiveNote('');
        setBusy((current) => (current.startsWith('Listening') || current.startsWith('Transcribing') ? '' : current));
      }
    },
    [mic, loadSessions, closeStream, makePackage, acceptResults, startNew, resetThread, startPublish, declinePublish],
  );

  const downloadPack = useCallback(async () => {
    setBusy('Preparing your download…');
    try {
      const bundle = await exportCampaign(session.id);
      const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `svarah-offer-${session.id}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy('');
    }
  }, [session]);

  if (checking) {
    return <div className="vw-loading">One moment…</div>;
  }
  if (!user) return null;

  const hint = (() => {
    if (busy || making) return '';
    if (awaitingAccept) return 'Look at the poster. Hold Space or the mic and say “yes” if it is right.';
    if (resumable) return 'Hold Space or the mic and say “yes” to make your posts.';
    if (reviewing) return 'Hold Space or the mic: say “yes” if this is right, or say what to change.';
    if (publish?.phase === 'declined') return '';
    if (connecting) return 'Sign in on the page that opened. Say “no” to cancel.';
    if (askingPublish) return 'Hold Space or the mic and say “yes” to post it, or “no” to finish.';
    if (ready) return 'Hold Space or the mic to start a new offer.';
    return '';
  })();

  return (
    <div className="vw-root">
      <header className="vw-head">
        <Wordmark className="vw-head-logo" />

        <button type="button" className="vw-ghost" onClick={startNew} title="Start a new offer">
          <Plus size={15} /> New offer
        </button>

        <button type="button" className="vw-ghost" onClick={() => setHistoryOpen((v) => !v)}>
          <History size={15} /> History
        </button>

        <Link className="vw-ghost" to={studioHref} title="Inspect every detail in the studio">
          <LayoutGrid size={15} /> Studio
        </Link>

        <span className="vw-grow" />

        <ThemeToggle />
        <span className="vw-avatar" title={user.name}>{user.avatar}</span>
        <button
          type="button"
          className="vw-ghost"
          onClick={() => {
            logout();
            // Leave the workspace explicitly: without this the route keeps
            // rendering with no user and the screen goes blank.
            navigate('/', { replace: true });
          }}
          aria-label="Sign out"
          title="Sign out"
        >
          <LogOut size={15} />
          <span className="vw-signout-label">Sign out</span>
        </button>
      </header>

      <div className="vw-body">
        {historyOpen ? (
          <aside className="vw-rail">
            <span className="vw-rail-label">Past offers</span>
            {sessions.length === 0 ? (
              <p className="vw-rail-empty">Nothing yet. Your first offer will appear here.</p>
            ) : (
              sessions.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={`vw-rail-item ${session?.id === item.id ? 'is-on' : ''}`}
                  onClick={() => openSession(item)}
                >
                  <b>{shortLabel(item)}</b>
                  <small>{new Date(item.created_at).toLocaleDateString()}</small>
                </button>
              ))
            )}
          </aside>
        ) : null}

        <main className="vw-thread" ref={threadRef}>
          {!session && !busy && !mic.recording ? (
            <div className="vw-hello">
              <span className="vw-hello-mark"><AudioLines size={26} /></span>
              <h1>What are you offering today?</h1>
              <p>Hold <kbd className="vw-kbd">Space</kbd> or the microphone and say it in your own words.</p>
            </div>
          ) : null}

          {session?.transcript?.raw ? (
            <div className="vw-row is-user">
              <div className="vw-bubble is-user">
                <span className="vw-tag">You said</span>
                {session.transcript.raw}
              </div>
            </div>
          ) : null}

          {sttReason ? (
            <div className="vw-row">
              <div className="vw-bubble is-warn" role="alert">
                I could not turn that recording into words: {sttReason} Hold the mic and say it again.
              </div>
            </div>
          ) : null}

          {session?.transcript?.raw && rows.length === 0 && !busy ? (
            <div className="vw-row">
              <div className="vw-bubble is-app">
                <span className="vw-tag"><Sparkles size={11} /> I need a little more</span>
                {session.factsheet?.extraction?.status && session.factsheet.extraction.status !== 'ok'
                  ? session.factsheet.extraction.message || 'The offer details could not be read automatically.'
                  : 'I could not pick out a product, price or discount from that.'}{' '}
                Nothing was guessed. Hold the mic and say the offer again.
              </div>
            </div>
          ) : null}

          {rows.length > 0 ? (
            <div className="vw-row">
              <div className="vw-bubble is-app">
                <span className="vw-tag"><Sparkles size={11} /> Here is what I understood</span>
                <dl className="vw-facts">
                  {rows.map((row) => (
                    <React.Fragment key={row.label + row.value}>
                      <dt>{row.label}</dt>
                      <dd>{row.value}</dd>
                    </React.Fragment>
                  ))}
                </dl>

                {verdict ? (
                  <p className={`vw-note ${verdict.good && !findings.length ? 'is-good' : ''}`}>
                    {verdict.good && !findings.length ? <Check size={13} /> : null} {verdict.text}
                  </p>
                ) : null}
                {findings.length ? (
                  <ul className="vw-findings">
                    {findings.map((item) => (
                      <li key={`${item.field}-${item.code}-${item.message}`} className={item.severity === 'error' ? 'is-bad' : ''}>
                        {item.message}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {locked ? <p className="vw-note is-good"><Check size={13} /> Confirmed and locked.</p> : null}
              </div>
            </div>
          ) : null}

          {turns.map((turn, index) => (
            // eslint-disable-next-line react/no-array-index-key
            <React.Fragment key={index}>
              {turn.heard ? (
                <div className="vw-row is-user">
                  <div className="vw-bubble is-user">
                    <span className="vw-tag">You said</span>
                    {turn.heard}
                  </div>
                </div>
              ) : null}
              {turn.intent !== 'confirm' && turn.reply ? (
                <div className="vw-row">
                  <div className={`vw-bubble ${turn.intent === 'unclear' ? 'is-warn' : 'is-app'}`}>
                    {turn.reply}
                    {turn.intent === 'correct' ? ' Check the details above.' : ''}
                  </div>
                </div>
              ) : null}
            </React.Fragment>
          ))}

          {showPackage ? (
            <div className="vw-row">
              <div className="vw-bubble is-app vw-package">
                <span className="vw-tag">
                  <Sparkles size={11} /> {making ? 'Making your campaign' : 'Your campaign'}
                  {making ? <span className="vw-elapsed">{elapsed}s</span> : null}
                </span>

                {making || stageList.some((item) => item.status === 'failed') ? (
                  <div className="vw-making">
                    {making ? (
                      <div className="vw-bar"><i style={{ width: `${Math.max(6, (doneCount / STAGES.length) * 100)}%` }} /></div>
                    ) : null}
                    <ul className="vw-stages">
                      {stageList.map((item) => (
                        <li key={item.key} className={`is-${item.status}`}>
                          <span className="vw-stage-mark">
                            {item.status === 'done' ? <Check size={12} /> : item.status === 'failed' ? <X size={12} /> : item.status === 'doing' ? <span className="vw-spin" /> : null}
                          </span>
                          <span className="vw-stage-label">{item.label}</span>
                          <span className="vw-stage-note">
                            {item.status === 'doing' ? item.doing
                              : item.status === 'failed' ? item.detail
                              : item.status === 'done' ? `${(item.ms / 1000).toFixed(1)}s` : ''}
                          </span>
                        </li>
                      ))}
                    </ul>
                    {making ? <p className="vw-wait-note" key={waitNote}>{waitNote}</p> : null}
                  </div>
                ) : null}

                <div className="vw-media">
                  <figure>
                    {poster && media[poster.id] ? (
                      <img className="vw-poster" src={media[poster.id]} alt="Your promotional poster" />
                    ) : (
                      <div className="vw-poster is-loading"><span>{stages.poster?.status === 'failed' ? 'Not made' : 'Painting the artwork…'}</span></div>
                    )}
                    <figcaption>Poster</figcaption>
                  </figure>
                  {bragVideo || making ? (
                    <figure>
                      {bragVideo && media[bragVideo.id] ? (
                        // eslint-disable-next-line jsx-a11y/media-has-caption
                        <video className="vw-video" src={media[bragVideo.id]} controls playsInline loop autoPlay muted />
                      ) : (
                        <div className="vw-video is-loading"><span>{stages.brag?.status === 'failed' ? 'Not made' : stages.brag?.status === 'doing' ? 'Drawing the frames…' : 'Waiting for poster and voice…'}</span></div>
                      )}
                      <figcaption>Campaign video</figcaption>
                    </figure>
                  ) : null}
                  {reel ? (
                    <figure>
                      {media[reel.id] ? (
                        // eslint-disable-next-line jsx-a11y/media-has-caption
                        <video className="vw-video" src={media[reel.id]} controls playsInline loop />
                      ) : <div className="vw-video is-loading" />}
                      <figcaption>Poster reel</figcaption>
                    </figure>
                  ) : null}
                  {aiVideo || aiJob?.active || (making && stages.aivideo?.status !== 'failed') ? (
                    <figure>
                      {aiVideo && media[aiVideo.id] ? (
                        // eslint-disable-next-line jsx-a11y/media-has-caption
                        <video className="vw-video" src={media[aiVideo.id]} controls playsInline loop autoPlay muted />
                      ) : (
                        <div className="vw-video is-loading"><span>Rendering{aiJob?.progress ? ` ${aiJob.progress}%` : '…'}</span></div>
                      )}
                      <figcaption>AI video</figcaption>
                    </figure>
                  ) : null}
                </div>
                {aiJob?.status === 'failed' ? (
                  <p className="vw-note is-bad">The AI video could not be made: {aiJob.error}</p>
                ) : null}

                {voice && media[voice.id] ? (
                  <div className="vw-audio">
                    <span className="vw-tag">Voice-over</span>
                    {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
                    <audio src={media[voice.id]} controls />
                  </div>
                ) : null}

                {socialCaptions.length ? (
                  <div className="vw-captions">
                    {socialCaptions.map((c) => (
                      <div key={c.id} className="vw-caption-card">
                        <span className="vw-tag">{c.provenance.channel}</span>
                        <p className="vw-caption">{c.text_content}</p>
                      </div>
                    ))}
                  </div>
                ) : making ? (
                  <div className="vw-captions">
                    <div className="vw-caption-card is-loading" /><div className="vw-caption-card is-loading" />
                  </div>
                ) : null}

                {poster && !making ? (
                  <div className="vw-actions">
                    {master ? (
                      <button
                        type="button"
                        className="vw-ghost"
                        onClick={() => navigator.clipboard?.writeText(master.text_content)}
                      >
                        <Copy size={13} /> Copy caption
                      </button>
                    ) : null}
                    <button type="button" className="vw-btn is-primary" onClick={downloadPack} disabled={Boolean(busy)}>
                      <Download size={14} /> Download
                    </button>
                  </div>
                ) : null}
                {poster?.used_fallback ? (
                  <p className="vw-note">
                    {poster.is_mock && !poster.provenance?.art_error
                      ? 'Demo mode: this poster uses a plain placeholder background, not AI artwork.'
                      : 'The AI artwork was unavailable, so this poster uses a plain background.'}
                  </p>
                ) : null}
                {making ? null : needsReview ? (
                  <p className="vw-note">One check could not finish by itself.</p>
                ) : ready ? (
                  <p className="vw-note is-good"><Check size={13} /> Every number matches your offer.</p>
                ) : null}
              </div>
            </div>
          ) : null}

          {canPost ? (
            <div className="vw-row">
              <div className="vw-bubble is-app">
                <span className="vw-tag"><Share2 size={11} /> Post it for you?</span>
                Shall I post this on your social media for you? Say “yes” and I will post the
                poster with its caption on your own accounts, or say “no” to finish here.
              </div>
            </div>
          ) : null}

          {canPost ? publishSaid.map((said, index) => (
            // eslint-disable-next-line react/no-array-index-key
            <React.Fragment key={index}>
              {said.heard ? (
                <div className="vw-row is-user">
                  <div className="vw-bubble is-user"><span className="vw-tag">You said</span>{said.heard}</div>
                </div>
              ) : null}
              {said.reply ? (
                <div className="vw-row"><div className="vw-bubble is-warn">{said.reply}</div></div>
              ) : null}
            </React.Fragment>
          )) : null}

          {canPost && publish?.phase === 'connect' ? (
            <div className="vw-row">
              <div className="vw-bubble is-app">
                <span className="vw-tag"><Share2 size={11} /> Connect your accounts</span>
                First sign in to the accounts you want me to post to. You sign in with the social
                network itself, so Svarah.AI never sees your password.
                <ul className="vw-accounts">
                  {(publish.accounts ?? []).map((account) => (
                    <li key={account.platform} className={account.connected ? 'is-on' : ''}>
                      <span className="vw-stage-mark">{account.connected ? <Check size={12} /> : null}</span>
                      {PLATFORM_NAMES[account.platform] ?? account.platform}
                      <small>{account.reauth_required ? 'sign in again' : account.connected ? account.handle : 'not connected'}</small>
                    </li>
                  ))}
                </ul>
                <div className="vw-actions">
                  <a className="vw-btn is-primary" href={publish.accessUrl} target="_blank" rel="noopener noreferrer">
                    <ExternalLink size={14} /> Sign in to my accounts
                  </a>
                </div>
                <p className="vw-note"><span className="vw-spin" /> Waiting for you to connect — I will post as soon as you are back.</p>
              </div>
            </div>
          ) : null}

          {publish?.phase === 'done' ? (
            <div className="vw-row">
              <div className="vw-bubble is-app">
                <span className="vw-tag"><Share2 size={11} /> Posting</span>
                <ul className="vw-accounts">
                  {publish.results.map((result) => (
                    <li key={result.platform} className={result.status === 'FAILED' ? 'is-bad' : 'is-on'}>
                      <span className="vw-stage-mark">{result.status === 'FAILED' ? <X size={12} /> : <Check size={12} />}</span>
                      {result.status === 'PUBLISHED'
                        ? `${result.already_posted ? 'Already posted' : 'Posted'} on ${PLATFORM_NAMES[result.platform] ?? result.platform}`
                        : result.status === 'PUBLISHING'
                          ? `${PLATFORM_NAMES[result.platform] ?? result.platform} is still uploading it`
                          : `${PLATFORM_NAMES[result.platform] ?? result.platform} did not accept it`}
                      <small>
                        {result.status === 'FAILED' ? result.error : result.account}
                        {result.url ? <> · <a href={result.url} target="_blank" rel="noopener noreferrer">View post</a></> : null}
                      </small>
                    </li>
                  ))}
                </ul>
                {publish.isMock ? <p className="vw-note">Demo mode: nothing was really posted.</p> : null}
              </div>
            </div>
          ) : null}

          {publish?.phase === 'unavailable' ? (
            <div className="vw-row">
              <div className="vw-bubble is-warn">
                Posting is not set up on this Svarah server yet, so nothing was posted. Your campaign
                is saved — use Download to post it yourself.
              </div>
            </div>
          ) : null}

          {publish?.phase === 'declined' ? (
            <div className="vw-row">
              <div className="vw-bubble is-app">
                No problem — nothing was posted. Your campaign is saved in History. Taking you back
                to the home page…
              </div>
            </div>
          ) : null}

          {captions.length > 0 && !showPackage && !busy ? (
            <div className="vw-row">
              <div className="vw-bubble is-app">
                <span className="vw-tag">Your captions</span>
                {captions.slice(0, 3).map((c) => (
                  <p key={c.id} className="vw-caption">{c.text_content}</p>
                ))}
              </div>
            </div>
          ) : null}

          {mic.recording || liveText ? (
            <div className="vw-row is-user">
              <div className="vw-bubble is-user is-live">
                <span className="vw-tag">
                  <span className="vw-live-dot" /> Live{liveIsMock ? ' · demo transcript' : ''}
                </span>
                {liveText || liveNote || 'Listening…'}
                {liveText ? <span className="vw-caret" aria-hidden="true" /> : null}
              </div>
            </div>
          ) : null}

          {busy ? (
            <div className="vw-row">
              <div className="vw-bubble is-app is-thinking">
                <span className="vw-dots"><i /><i /><i /></span> {busy}
              </div>
            </div>
          ) : null}

          {error ? (
            <div className="vw-row">
              <div className="vw-bubble is-warn">{error}</div>
            </div>
          ) : null}
        </main>
      </div>

      <footer className="vw-dock">
        {hint ? <p className="vw-hint"><Mic size={13} /> {hint}</p> : null}
        {mic.supported ? (
          <VoicePill
            accentColor="#f5f5f5"
            iconColor="#a1a1aa"
            background="#27272a"
            size={44}
            shape="pill"
            reach={8}
            showTime
            waveform
            slideToCancel
            cancelDistance={64}
            attack={40}
            release={240}
            sensitivity={1}
            floor={0.1}
            openDuration={200}
            pressScale={0.95}
            mode="auto"
            holdAfter={300}
            reactive="external"
            maxSeconds={120}
            iconSize={30}
            holdKey="Space"
            ariaLabel="Hold Space, or hold or tap the mic, to speak"
            getLevel={mic.getLevel}
            disabled={Boolean(busy) || Boolean(making) || publish?.phase === 'declined'}
            onStart={handleStart}
            onStop={handleStop}
          />
        ) : (
          <p className="vw-note is-bad">
            This browser cannot record audio here. Open Svarah in Chrome, Edge or Safari over
            https (or on localhost) and allow the microphone.
          </p>
        )}
        {mic.error ? <p className="vw-note is-bad">{mic.error}</p> : null}
      </footer>
    </div>
  );
}
