import React, { useEffect, useState } from 'react';
import { Github } from 'lucide-react';
import { useAuth } from './AuthContext';
import { errorMessage, getModes } from './lib/api';
import { firebaseConfigured, phoneErrorMessage, signInWithGoogle } from './lib/firebase';
import GoogleMark from './GoogleMark';

const GITHUB_CLIENT_ID = import.meta.env.VITE_GITHUB_CLIENT_ID ?? '';

/** Google and GitHub buttons. Each shows only when both the site and the backend are configured for it. */
export default function SocialLogin({ onSignedIn, verb = 'Continue' }) {
  const { loginWithSocial } = useAuth();
  const [modes, setModes] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    getModes(controller.signal)
      .then((m) => setModes(m?.auth ?? {}))
      .catch(() => {});
    return () => controller.abort();
  }, []);

  const google = firebaseConfigured && modes.google_login_enabled;
  const github = modes.github_login_enabled && GITHUB_CLIENT_ID;
  if (!google && !github) return null;

  // GitHub leaves the page and comes back to /auth/github (GitHubCallback) with a one-time code.
  const startGithub = () => {
    const state = crypto.randomUUID();
    sessionStorage.setItem('gh_oauth_state', state);
    const query = new URLSearchParams({ client_id: GITHUB_CLIENT_ID, scope: 'user:email', state });
    window.location.assign(`https://github.com/login/oauth/authorize?${query}`);
  };

  const startGoogle = async () => {
    setBusy(true);
    setError('');
    let idToken;
    try {
      idToken = await signInWithGoogle();
    } catch (problem) {
      setError(phoneErrorMessage(problem));
      setBusy(false);
      return;
    }
    try {
      await loginWithSocial('google', idToken);
      onSignedIn?.();
    } catch (problem) {
      setError(errorMessage(problem)); // Google accepted it, our backend refused
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-form">
      <div className="auth-divider"><span>or</span></div>
      {error ? (
        <div className="auth-toast" role="alert"><span>{error}</span></div>
      ) : null}
      {google ? (
        <button type="button" className="button button-google" onClick={startGoogle} disabled={busy}>
          <GoogleMark />
          <span>{busy ? 'Waiting for Google...' : `${verb} with Google`}</span>
        </button>
      ) : null}
      {github ? (
        <button type="button" className="button button-google" onClick={startGithub}>
          <Github size={18} />
          <span>{`${verb} with GitHub`}</span>
        </button>
      ) : null}
    </div>
  );
}
