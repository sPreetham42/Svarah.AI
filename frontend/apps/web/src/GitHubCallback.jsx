import React, { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from './AuthContext';
import { errorMessage } from './lib/api';
import './auth.css';

/** GitHub redirects here with `?code&state`; the code is exchanged for a Svarah session. */
export default function GitHubCallback() {
  const { loginWithSocial } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState('');
  const started = useRef(false); // StrictMode runs effects twice, and a code works once

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const params = new URLSearchParams(window.location.search);
    const expected = sessionStorage.getItem('gh_oauth_state');
    sessionStorage.removeItem('gh_oauth_state');
    if (params.get('error') || !params.get('code') || !expected || params.get('state') !== expected) {
      setError('GitHub sign-in was cancelled or could not be verified. Please try again.');
      return;
    }
    loginWithSocial('github', undefined, params.get('code'))
      .then(() => navigate('/workspace', { replace: true }))
      .catch((problem) => setError(errorMessage(problem)));
  }, [loginWithSocial, navigate]);

  return (
    <div className="auth-page">
      <div className="auth-card-wrap">
        <div className="auth-card">
          {error ? (
            <>
              <div className="auth-toast" role="alert"><span>{error}</span></div>
              <p><Link to="/login">Back to sign in</Link></p>
            </>
          ) : (
            <p>Signing you in with GitHub...</p>
          )}
        </div>
      </div>
    </div>
  );
}
