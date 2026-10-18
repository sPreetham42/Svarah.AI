import React, { useState } from 'react';
import { useAuth } from './AuthContext';
import { errorMessage } from './lib/api';
import { phoneErrorMessage, signInWithGoogle } from './lib/firebase';
import GoogleMark from './GoogleMark';

/**
 * Sign in (or sign up — the first sign-in creates the account) with a Google
 * account. Firebase runs Google's sign-in window; the backend then issues the
 * Svarah session. `onSignedIn` runs once that session exists.
 */
export default function GoogleLogin({ onSignedIn, label = 'Continue with Google' }) {
  const { loginWithGoogle } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const start = async () => {
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
      await loginWithGoogle(idToken);
      onSignedIn?.();
    } catch (problem) {
      // Google accepted the sign-in but our backend refused the exchange.
      setError(errorMessage(problem));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-form">
      {error ? (
        <div className="auth-toast" role="alert">
          <span>{error}</span>
        </div>
      ) : null}
      <button type="button" className="button button-google" onClick={start} disabled={busy}>
        <GoogleMark />
        <span>{busy ? 'Waiting for Google...' : label}</span>
      </button>
      <small className="phone-hint">Use your Gmail or any Google account. We only read your name and email address.</small>
    </div>
  );
}
