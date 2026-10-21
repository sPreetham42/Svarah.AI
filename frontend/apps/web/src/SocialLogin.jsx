import React, { useEffect, useState } from 'react';
import { Github } from 'lucide-react';
import { getModes } from './lib/api';

/**
 * "Continue with GitHub". The OAuth client ID comes from the API (/api/modes),
 * so the backend is the only place GitHub is configured. Hidden until it is.
 */
export default function SocialLogin({ verb = 'Continue' }) {
  const [clientId, setClientId] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    getModes(controller.signal)
      .then((modes) => setClientId(modes?.auth?.github_client_id || ''))
      .catch(() => {});
    return () => controller.abort();
  }, []);

  if (!clientId) return null;

  // GitHub leaves the page and comes back to /auth/github (GitHubCallback) with a one-time code.
  const startGithub = () => {
    const state = crypto.randomUUID();
    sessionStorage.setItem('gh_oauth_state', state);
    const query = new URLSearchParams({
      client_id: clientId,
      // Explicit, so an OAuth App registered for another site fails at GitHub instead of redirecting there.
      redirect_uri: `${window.location.origin}/auth/github`,
      scope: 'user:email',
      state,
    });
    window.location.assign(`https://github.com/login/oauth/authorize?${query}`);
  };

  return (
    <div className="auth-form">
      <div className="auth-divider"><span>or</span></div>
      <button type="button" className="button button-google" onClick={startGithub}>
        <Github size={18} />
        <span>{`${verb} with GitHub`}</span>
      </button>
    </div>
  );
}
