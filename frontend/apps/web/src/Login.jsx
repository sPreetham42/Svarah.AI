import React, { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ArrowRight, ShieldCheck, Zap } from 'lucide-react';
import { useAuth } from './AuthContext';
import { errorMessage, getModes } from './lib/api';
import AuthMethodTabs, { useFirebaseLoginStatus } from './AuthMethodTabs';
import GoogleLogin from './GoogleLogin';
import PhoneLogin from './PhoneLogin';
import ThemeToggle from './ThemeToggle';
import Wordmark from './Wordmark.jsx';
import './auth.css';

export default function Login() {
  const [loading, setLoading] = useState(false);
  const [toast, setToast] = useState('');
  const [method, setMethod] = useState('google');
  const firebaseStatus = useFirebaseLoginStatus();
  const { user, checking, loginWithDemo } = useAuth();
  const navigate = useNavigate();

  // Someone who is already signed in has no use for this page.
  useEffect(() => {
    if (!checking && user) navigate('/workspace', { replace: true });
  }, [checking, user, navigate]);

  // The password-less demo sign-in exists only in mock mode. Ask the backend
  // rather than guessing, so a live deployment never shows an affordance the
  // server will refuse.
  const [demoEnabled, setDemoEnabled] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    getModes(controller.signal)
      .then((modes) => setDemoEnabled(modes?.mode === 'mock'))
      .catch(() => setDemoEnabled(false));
    return () => controller.abort();
  }, []);

  const handleDemoLogin = async () => {
    setLoading(true);
    setToast('');
    try {
      await loginWithDemo();
      navigate('/workspace');
    } catch (error) {
      setToast(errorMessage(error));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="auth-page">
      <ThemeToggle />
      <div className="auth-ambient-glow" />

      <div className="auth-card-wrap">
        <div className="auth-card">
          <div className="auth-header">
            <Wordmark size="lg" className="auth-logo" />
            <h1>Welcome back</h1>
            <p>Say what you are offering. Svarah writes the posts and gets them ready to share.</p>
          </div>

          {toast && (
            <div className="auth-toast">
              <span>{toast}</span>
            </div>
          )}

          {/* Quick 1-Click Demo Login — mock mode only. */}
          {demoEnabled ? (
            <div className="demo-login-box">
              <div className="demo-login-info">
                <Zap size={18} className="zap-icon"/>
                <div>
                  <b>Instant Demo Access</b>
                  <small>Sign in without a password — available while the backend runs in mock mode</small>
                </div>
              </div>
              <button
                type="button"
                className="btn-demo-quick"
                onClick={handleDemoLogin}
                disabled={loading}
              >
                {loading ? 'Logging in...' : <><span>Demo Login</span> <ArrowRight size={14}/></>}
              </button>
            </div>
          ) : null}

          <AuthMethodTabs method={method} onChange={setMethod} />

          {!firebaseStatus.ready ? (
            <p className="phone-unavailable">{firebaseStatus.message}</p>
          ) : method === 'phone' ? (
            <PhoneLogin onSignedIn={() => navigate('/workspace')} />
          ) : (
            <GoogleLogin onSignedIn={() => navigate('/workspace')} />
          )}

          <div className="auth-footer">
            <p>Don't have an account yet? <Link to="/signup">Sign up for free</Link></p>
            <div className="auth-security-note">
              <ShieldCheck size={14}/>
              <span>Sessions are signed server-side — your facts stay locked to your account</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
