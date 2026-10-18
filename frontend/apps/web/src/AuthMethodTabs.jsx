import React, { useEffect, useState } from 'react';
import { Phone } from 'lucide-react';
import { getModes } from './lib/api';
import { firebaseConfigured } from './lib/firebase';
import GoogleMark from './GoogleMark';

/** The Google / Phone switch shared by the login and signup cards. */
export default function AuthMethodTabs({ method, onChange }) {
  const options = [
    { key: 'google', label: 'Google', icon: <GoogleMark size={14} /> },
    { key: 'phone', label: 'Phone number', icon: <Phone size={14} /> },
  ];
  return (
    <div className="auth-methods" role="tablist" aria-label="Sign-in method">
      {options.map(({ key, label, icon }) => (
        <button
          key={key}
          type="button"
          role="tab"
          aria-selected={method === key}
          className={`auth-method ${method === key ? 'is-on' : ''}`}
          onClick={() => onChange(key)}
        >
          {icon} {label}
        </button>
      ))}
    </div>
  );
}

/**
 * Whether Google and phone sign-in can work right now. Both need two halves:
 * the Firebase web config in this build, and a backend that can verify
 * Firebase tokens. The backend is asked rather than assumed, so a form is only
 * offered when the server will accept what it produces.
 */
export function useFirebaseLoginStatus() {
  const [status, setStatus] = useState(() => (
    firebaseConfigured
      ? { ready: false, message: 'Checking sign-in…' }
      : { ready: false, message: 'Sign-in is not set up yet: add the VITE_FIREBASE_* values to apps/web/.env and restart the website.' }
  ));

  useEffect(() => {
    if (!firebaseConfigured) return undefined;
    const controller = new AbortController();
    getModes(controller.signal)
      .then((modes) => {
        setStatus(
          modes?.auth?.phone_login_enabled
            ? { ready: true, message: '' }
            : { ready: false, message: 'Sign-in is not set up on the server yet: set TITAN_FIREBASE_API_KEY in the backend .env and restart it.' },
        );
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setStatus({ ready: false, message: 'The backend is unreachable, so sign-in is unavailable right now.' });
        }
      });
    return () => controller.abort();
  }, []);

  return status;
}
