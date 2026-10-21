import React, { useState } from 'react';
import { Lock, Mail, User } from 'lucide-react';
import { useAuth } from './AuthContext';
import { errorMessage } from './lib/api';

/** Email + password: `mode` 'login' signs in, 'signup' creates the account. */
export default function EmailForm({ mode, onSignedIn }) {
  const { login, signup } = useAuth();
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const isSignup = mode === 'signup';

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      if (isSignup) await signup(name.trim(), email.trim(), password);
      else await login(email.trim(), password);
      onSignedIn?.();
    } catch (problem) {
      setError(errorMessage(problem));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="auth-form" onSubmit={submit}>
      {error ? <div className="auth-toast" role="alert"><span>{error}</span></div> : null}
      {isSignup ? (
        <div className="form-group">
          <label htmlFor="auth-name">Your name</label>
          <div className="input-wrap">
            <User size={16} className="input-icon" />
            <input id="auth-name" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" placeholder="Asha Rao" />
          </div>
        </div>
      ) : null}
      <div className="form-group">
        <label htmlFor="auth-email">Email</label>
        <div className="input-wrap">
          <Mail size={16} className="input-icon" />
          <input id="auth-email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" placeholder="you@example.com" />
        </div>
      </div>
      <div className="form-group">
        <label htmlFor="auth-password">Password</label>
        <div className="input-wrap">
          <Lock size={16} className="input-icon" />
          <input
            id="auth-password"
            type="password"
            required
            minLength={isSignup ? 8 : undefined}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={isSignup ? 'new-password' : 'current-password'}
            placeholder={isSignup ? 'At least 8 characters' : 'Your password'}
          />
        </div>
      </div>
      <button type="submit" className="button-auth-primary" disabled={busy}>
        {busy ? 'Please wait...' : isSignup ? 'Create Free Account' : 'Sign in'}
      </button>
    </form>
  );
}
