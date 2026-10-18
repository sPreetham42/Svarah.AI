import React, { useEffect, useRef, useState } from 'react';
import { ArrowRight, KeyRound, Phone } from 'lucide-react';
import { useAuth } from './AuthContext';
import { errorMessage } from './lib/api';
import { phoneErrorMessage, sendPhoneCode } from './lib/firebase';

/**
 * Sign in (or sign up — the first sign-in creates the account) with a phone
 * number: enter the number, receive an SMS code, enter the code.
 *
 * Firebase verifies the number; the backend then issues the Svarah session.
 * `onSignedIn` runs once that session exists.
 */

const RESEND_SECONDS = 30;

/** `98765 43210` → `+919876543210`; a number already starting with `+` is kept. */
function toE164(countryCode, raw) {
  const trimmed = raw.trim();
  if (trimmed.startsWith('+')) return `+${trimmed.replace(/\D/g, '')}`;
  return `${countryCode}${trimmed.replace(/\D/g, '').replace(/^0+/, '')}`;
}

export default function PhoneLogin({ onSignedIn, submitLabel = 'Sign in to Svarah.AI' }) {
  const { loginWithPhone } = useAuth();
  const [countryCode, setCountryCode] = useState('+91');
  const [number, setNumber] = useState('');
  const [code, setCode] = useState('');
  const [sentTo, setSentTo] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [resendIn, setResendIn] = useState(0);

  const pending = useRef(null); // the code-confirmation handle from Firebase
  const recaptcha = useRef(null);
  const codeInput = useRef(null);

  useEffect(() => () => pending.current?.dispose(), []);

  useEffect(() => {
    if (resendIn <= 0) return undefined;
    const timer = setTimeout(() => setResendIn((value) => value - 1), 1000);
    return () => clearTimeout(timer);
  }, [resendIn]);

  useEffect(() => {
    if (sentTo) codeInput.current?.focus();
  }, [sentTo]);

  const send = async () => {
    const phone = toE164(countryCode, number);
    if (!/^\+[1-9]\d{6,14}$/.test(phone)) {
      setError('Enter your full phone number.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      pending.current?.dispose();
      pending.current = await sendPhoneCode(phone, recaptcha.current);
      setSentTo(phone);
      setCode('');
      setResendIn(RESEND_SECONDS);
    } catch (problem) {
      setError(phoneErrorMessage(problem));
    } finally {
      setBusy(false);
    }
  };

  const verify = async () => {
    if (!/^\d{6}$/.test(code)) {
      setError('Enter the 6-digit code from the SMS.');
      return;
    }
    setBusy(true);
    setError('');
    let idToken;
    try {
      idToken = await pending.current.confirm(code);
    } catch (problem) {
      setError(phoneErrorMessage(problem));
      setBusy(false);
      return;
    }
    try {
      await loginWithPhone(idToken);
      onSignedIn?.();
    } catch (problem) {
      // Firebase accepted the code but our backend refused the exchange.
      setError(errorMessage(problem));
    } finally {
      setBusy(false);
    }
  };

  const changeNumber = () => {
    pending.current?.dispose();
    pending.current = null;
    setSentTo('');
    setCode('');
    setError('');
  };

  return (
    <form
      className="auth-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (sentTo) verify();
        else send();
      }}
    >
      {error ? (
        <div className="auth-toast" role="alert">
          <span>{error}</span>
        </div>
      ) : null}

      {!sentTo ? (
        <div className="form-group">
          <label htmlFor="phone-number">Phone number</label>
          <div className="phone-row">
            <select
              className="auth-select phone-country"
              aria-label="Country code"
              value={countryCode}
              onChange={(event) => setCountryCode(event.target.value)}
            >
              <option value="+91">🇮🇳 +91</option>
              <option value="+1">🇺🇸 +1</option>
              <option value="+44">🇬🇧 +44</option>
              <option value="+971">🇦🇪 +971</option>
              <option value="+65">🇸🇬 +65</option>
              <option value="+61">🇦🇺 +61</option>
            </select>
            <div className="input-wrap">
              <Phone size={16} className="input-icon" />
              <input
                id="phone-number"
                type="tel"
                inputMode="tel"
                autoComplete="tel-national"
                placeholder="98765 43210"
                value={number}
                onChange={(event) => setNumber(event.target.value)}
                required
              />
            </div>
          </div>
          <small className="phone-hint">We text you a 6-digit code. Standard SMS rates may apply.</small>
        </div>
      ) : (
        <div className="form-group">
          <div className="label-row">
            <label htmlFor="phone-code">Code sent to {sentTo}</label>
            <button type="button" className="link-btn" onClick={changeNumber}>
              Change number
            </button>
          </div>
          <div className="input-wrap">
            <KeyRound size={16} className="input-icon" />
            <input
              id="phone-code"
              ref={codeInput}
              className="phone-code-input"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="[0-9]*"
              maxLength={6}
              placeholder="••••••"
              value={code}
              onChange={(event) => setCode(event.target.value.replace(/\D/g, ''))}
              required
            />
          </div>
          <div className="form-row-between">
            <small className="phone-hint">Did not get it?</small>
            <button type="button" className="link-btn" onClick={send} disabled={busy || resendIn > 0}>
              {resendIn > 0 ? `Resend in ${resendIn}s` : 'Resend code'}
            </button>
          </div>
        </div>
      )}

      <button type="submit" className="button button-auth-primary" disabled={busy}>
        {busy
          ? (sentTo ? 'Checking the code...' : 'Sending the code...')
          : sentTo
            ? <>{submitLabel} <ArrowRight size={16} /></>
            : <>Send code <ArrowRight size={16} /></>}
      </button>

      {/* Firebase's invisible reCAPTCHA mounts here. */}
      <div ref={recaptcha} />
    </form>
  );
}
