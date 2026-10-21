import React from 'react';
import { Link } from 'react-router-dom';
import { ArrowLeft, ArrowRight, Mic, Smartphone } from 'lucide-react';
import ThemeToggle from './ThemeToggle';
import Wordmark from './Wordmark.jsx';
import './auth.css';

/** The Android app is not released yet; this page says so and points to the web workspace. */
export default function DownloadApp() {
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

          <span className="apk-soon">Coming soon</span>
          <h1>The Svarah.AI Android app is on its way.</h1>
          <p className="apk-sub">
            Speak your offer in your own language, confirm the facts by voice, and post the finished campaign,
            all from your phone. Until it launches, everything works in your phone's browser.
          </p>

          <Link className="button button-auth-primary apk-button" to="/workspace">
            Open the web workspace <ArrowRight size={17} />
          </Link>
          <p className="apk-note">
            <Smartphone size={14} /> On your phone, open this website and use "Add to Home screen" for an app-like shortcut.
          </p>
        </div>
      </div>
    </div>
  );
}
