import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowLeft, Download, Mic, ShieldCheck, Smartphone } from 'lucide-react';
import ThemeToggle from './ThemeToggle';
import Wordmark from './Wordmark.jsx';
import './auth.css';

/**
 * The Android app download page.
 *
 * To publish a build, put the APK at `public/downloads/svarah-ai.apk`. The page
 * checks that the file is really there (a missing file comes back as the
 * site's HTML, because of the SPA rewrite) and says so instead of offering a
 * dead link.
 */

export const APK_URL = '/downloads/svarah-ai.apk';

const megabytes = (bytes) => `${(bytes / (1024 * 1024)).toFixed(1)} MB`;

const STEPS = [
  { title: 'Tap Download', text: 'Your phone saves the Svarah.AI installer. If the browser warns about the file type, choose “Download anyway”.' },
  { title: 'Open the file', text: 'Tap the download notification, or open svarah-ai.apk from your Downloads folder.' },
  { title: 'Allow the install', text: 'Android asks once whether this browser may install apps. Turn it on, go back, and tap Install.' },
  { title: 'Sign in and speak', text: 'Open Svarah.AI, sign in with Google or your phone number, and hold the mic to say your offer.' },
];

export default function DownloadApp() {
  // undefined = still checking, null = no APK published, else { size }.
  const [info, setInfo] = useState(undefined);

  useEffect(() => {
    const controller = new AbortController();
    fetch(APK_URL, { method: 'HEAD', signal: controller.signal, cache: 'no-store' })
      .then((response) => {
        const type = response.headers.get('content-type') || '';
        const size = Number(response.headers.get('content-length')) || null;
        // Any real APK is megabytes; a few-KB reply is the HTML fallback.
        const isApk = response.ok && !type.includes('text/html') && (size === null || size > 1024 * 1024);
        return setInfo(isApk ? { size } : null);
      })
      .catch(() => {
        if (!controller.signal.aborted) setInfo(null);
      });
    return () => controller.abort();
  }, []);

  const onAndroid = typeof navigator !== 'undefined' && /android/i.test(navigator.userAgent);

  return (
    <div className="auth-page demo-page">
      <ThemeToggle />
      <div className="auth-ambient-glow" />

      <div className="demo-wrap apk-wrap">
        <Link to="/" className="demo-back">
          <ArrowLeft size={16} /> Back to home
        </Link>

        <div className="auth-card apk-card">
          <div className="apk-head">
            <span className="apk-icon"><Mic size={26} /></span>
            <div>
              <Wordmark size="md" />
              <p>The voice workspace, on your Android phone.</p>
            </div>
          </div>

          <h1>Say your offer. Get the poster, the posts, the voice-over and the video.</h1>
          <p className="apk-sub">
            Everything the website does, in your pocket: speak in your own language, confirm the facts by
            voice, and post the finished campaign to your own social accounts.
          </p>

          {info ? (
            <>
              <a className="button button-auth-primary apk-button" href={APK_URL} download="svarah-ai.apk">
                <Download size={17} /> Download for Android
              </a>
              <p className="apk-meta">
                {info.size ? `${megabytes(info.size)} · ` : ''}Android 7 or newer
              </p>
              {!onAndroid ? (
                <p className="apk-note">
                  <Smartphone size={14} /> You are on a computer. Open this page on your Android phone, or
                  download here and copy the file to your phone.
                </p>
              ) : null}
            </>
          ) : info === null ? (
            <p className="phone-unavailable">
              The Android app is not available for download yet. Please check back soon.
            </p>
          ) : (
            <p className="apk-meta">Checking for the latest version…</p>
          )}

          <ol className="apk-steps">
            {STEPS.map((step, index) => (
              <li key={step.title}>
                <span className="apk-step-number">{index + 1}</span>
                <div>
                  <b>{step.title}</b>
                  <p>{step.text}</p>
                </div>
              </li>
            ))}
          </ol>

          <div className="auth-security-note">
            <ShieldCheck size={14} />
            <span>
              Android shows a warning for any app installed outside the Play Store. Only install it from this
              page.
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
