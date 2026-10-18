import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Microphone capture for the voice workspace.
 *
 * Owns the MediaRecorder lifecycle and a live level meter, but not the UI.
 * `start()` resolves once audio is actually flowing, so the caller can turn a
 * permission failure into a visible message instead of a silent no-op.
 */

/** True only where real microphone capture is possible. */
export function recordingSupported() {
  return (
    typeof window !== 'undefined' &&
    typeof window.MediaRecorder !== 'undefined' &&
    Boolean(navigator?.mediaDevices?.getUserMedia) &&
    (window.isSecureContext || window.location.hostname === 'localhost')
  );
}

export function preferredMimeType() {
  const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];
  return candidates.find((type) => window.MediaRecorder?.isTypeSupported?.(type)) ?? '';
}

export function useMicRecorder() {
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState('');

  const recorderRef = useRef(null);
  const chunksRef = useRef([]);
  const streamRef = useRef(null);
  const analyserRef = useRef(null);
  const audioCtxRef = useRef(null);
  const rafRef = useRef(null);
  const levelRef = useRef(0);

  const release = useCallback(() => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
    analyserRef.current = null;
    levelRef.current = 0;
    if (audioCtxRef.current) {
      audioCtxRef.current.close().catch(() => {});
      audioCtxRef.current = null;
    }
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  }, []);

  useEffect(() => () => release(), [release]);

  /** Live 0..1 level for the pill's meter. Read from a ref, never state. */
  const getLevel = useCallback(() => levelRef.current, []);

  const start = useCallback(async (options = {}) => {
    const onChunk = options.onChunk;
    setError('');
    if (!recordingSupported()) {
      const message = 'This browser cannot record audio here. Type your offer instead.';
      setError(message);
      throw new Error(message);
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (AudioCtx) {
        const ctx = new AudioCtx();
        audioCtxRef.current = ctx;
        const source = ctx.createMediaStreamSource(stream);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 512;
        source.connect(analyser);
        analyserRef.current = analyser;
        const buffer = new Uint8Array(analyser.frequencyBinCount);
        const tick = () => {
          if (!analyserRef.current) return;
          analyserRef.current.getByteTimeDomainData(buffer);
          let sum = 0;
          for (let i = 0; i < buffer.length; i += 1) {
            const v = (buffer[i] - 128) / 128;
            sum += v * v;
          }
          levelRef.current = Math.min(1, Math.sqrt(sum / buffer.length) * 3.2);
          rafRef.current = requestAnimationFrame(tick);
        };
        rafRef.current = requestAnimationFrame(tick);
      }

      chunksRef.current = [];
      const mimeType = preferredMimeType();
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      recorder.ondataavailable = (event) => {
        if (!event.data || event.data.size === 0) return;
        chunksRef.current.push(event.data);
        // Feed the live preview without blocking the recorder.
        if (typeof onChunk === 'function') {
          event.data.arrayBuffer().then((buffer) => onChunk(buffer)).catch(() => {});
        }
      };
      recorder.start(250);
      recorderRef.current = recorder;
      setRecording(true);
    } catch (err) {
      release();
      const message =
        err?.name === 'NotAllowedError'
          ? 'Microphone permission was denied. Allow it and try again.'
          : err?.message || 'The microphone could not be started.';
      setError(message);
      throw new Error(message);
    }
  }, [release]);

  /** Stop and return the clip, or null when nothing usable was captured. */
  const stop = useCallback(() => {
    return new Promise((resolve) => {
      const recorder = recorderRef.current;
      recorderRef.current = null;
      setRecording(false);
      if (!recorder || recorder.state === 'inactive') {
        release();
        resolve(null);
        return;
      }
      recorder.onstop = () => {
        const type = recorder.mimeType || 'audio/webm';
        const blob = chunksRef.current.length ? new Blob(chunksRef.current, { type }) : null;
        chunksRef.current = [];
        release();
        resolve(blob && blob.size > 0 ? blob : null);
      };
      try {
        recorder.stop();
      } catch {
        release();
        resolve(null);
      }
    });
  }, [release]);

  const cancel = useCallback(() => {
    const recorder = recorderRef.current;
    recorderRef.current = null;
    chunksRef.current = [];
    setRecording(false);
    try {
      if (recorder && recorder.state !== 'inactive') recorder.stop();
    } catch {
      /* nothing left to stop */
    }
    release();
  }, [release]);

  return { supported: recordingSupported(), recording, start, stop, cancel, getLevel, error };
}
