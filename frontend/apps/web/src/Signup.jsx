import React, { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ShieldCheck } from 'lucide-react';
import { useAuth } from './AuthContext';
import AuthMethodTabs, { useFirebaseLoginStatus } from './AuthMethodTabs';
import GoogleLogin from './GoogleLogin';
import PhoneLogin from './PhoneLogin';
import ThemeToggle from './ThemeToggle';
import Wordmark from './Wordmark.jsx';
import './auth.css';

export default function Signup() {
  const [method, setMethod] = useState('google');
  const firebaseStatus = useFirebaseLoginStatus();
  const { user, checking } = useAuth();
  const navigate = useNavigate();

  // Someone who is already signed in has no use for this page.
  useEffect(() => {
    if (!checking && user) navigate('/workspace', { replace: true });
  }, [checking, user, navigate]);

  return (
    <div className="auth-page">
      <ThemeToggle />
      <div className="auth-ambient-glow" />

      <div className="auth-card-wrap">
        <div className="auth-card">
          <div className="auth-header">
            <Wordmark size="lg" className="auth-logo" />
            <h1>Create your Svarah.AI account</h1>
            <p>Tell Svarah about your offer in your own words and it writes the posts for you.</p>
          </div>

          <AuthMethodTabs method={method} onChange={setMethod} />

          {!firebaseStatus.ready ? (
            <p className="phone-unavailable">{firebaseStatus.message}</p>
          ) : method === 'phone' ? (
            <PhoneLogin onSignedIn={() => navigate('/workspace')} submitLabel="Create Free Account" />
          ) : (
            <GoogleLogin onSignedIn={() => navigate('/workspace')} label="Sign up with Google" />
          )}

          <div className="auth-footer">
            <p>Already have an account? <Link to="/login">Sign in here</Link></p>
            <div className="auth-security-note">
              <ShieldCheck size={14}/>
              <span>Privacy first • Your audio is encrypted & never sold</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
