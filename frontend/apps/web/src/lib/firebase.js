/**
 * Firebase, used for exactly one thing on the website: proving that a visitor
 * owns a phone number.
 *
 * The browser sends the SMS code to Firebase and gets a Firebase ID token back.
 * That token is exchanged with our own backend (`POST /api/auth/phone`) for an
 * ordinary Svarah session, so everything after sign-in works exactly as it
 * does for an email account. The Firebase session itself is dropped as soon as
 * the exchange is done.
 *
 * The SDK is loaded on demand — a visitor who signs in by email never
 * downloads it.
 */

const config = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY ?? '',
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN ?? '',
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID ?? '',
  appId: import.meta.env.VITE_FIREBASE_APP_ID ?? '',
};

/** True when the Firebase web config is present in this build. */
export const firebaseConfigured = Boolean(config.apiKey && config.authDomain && config.projectId);

let authPromise = null;

async function loadAuth() {
  if (!firebaseConfigured) {
    throw new Error('Phone sign-in is not set up: the VITE_FIREBASE_* values are missing.');
  }
  authPromise ??= (async () => {
    const [{ initializeApp, getApps, getApp }, authModule] = await Promise.all([
      import('firebase/app'),
      import('firebase/auth'),
    ]);
    const app = getApps().length ? getApp() : initializeApp(config);
    const auth = authModule.getAuth(app);
    auth.useDeviceLanguage(); // the SMS and the reCAPTCHA follow the browser's language
    return { auth, authModule };
  })();
  return authPromise;
}

/**
 * Send an SMS code to `phone` (E.164, e.g. `+919876543210`).
 *
 * `container` is an element for Firebase's invisible reCAPTCHA. Resolves to a
 * handle whose `confirm(code)` returns the Firebase ID token, and whose
 * `dispose()` removes the reCAPTCHA again.
 */
export async function sendPhoneCode(phone, container) {
  const { auth, authModule } = await loadAuth();
  // A reCAPTCHA cannot be rendered twice into one element, so each attempt
  // (including "Resend code") gets a fresh slot.
  const slot = document.createElement('div');
  container.replaceChildren(slot);
  const verifier = new authModule.RecaptchaVerifier(auth, slot, { size: 'invisible' });
  let confirmation;
  try {
    confirmation = await authModule.signInWithPhoneNumber(auth, phone, verifier);
  } catch (error) {
    verifier.clear();
    throw error;
  }
  return {
    async confirm(code) {
      const credential = await confirmation.confirm(code);
      const idToken = await credential.user.getIdToken();
      // Our backend session is the one that counts; do not keep a second one.
      await authModule.signOut(auth).catch(() => {});
      return idToken;
    },
    dispose() {
      verifier.clear();
    },
  };
}

/**
 * Open Google's sign-in window and resolve to the Firebase ID token for the
 * account the visitor picked. Must be called from a click, or the browser
 * blocks the window.
 */
export async function signInWithGoogle() {
  const { auth, authModule } = await loadAuth();
  const provider = new authModule.GoogleAuthProvider();
  provider.setCustomParameters({ prompt: 'select_account' });
  const credential = await authModule.signInWithPopup(auth, provider);
  const idToken = await credential.user.getIdToken();
  // Our backend session is the one that counts; do not keep a second one.
  await authModule.signOut(auth).catch(() => {});
  return idToken;
}

/** Firebase's `auth/…` codes in plain words. */
export function phoneErrorMessage(error) {
  switch (error?.code) {
    case 'auth/invalid-phone-number':
      return 'That phone number does not look right. Include the country code, e.g. +91 98765 43210.';
    case 'auth/missing-phone-number':
      return 'Enter your phone number.';
    case 'auth/too-many-requests':
      return 'Too many attempts from this device. Wait a while and try again.';
    case 'auth/quota-exceeded':
      return 'The SMS limit for today has been reached. Try again later or sign in with email.';
    case 'auth/invalid-verification-code':
      return 'That code is not correct. Check the SMS and try again.';
    case 'auth/code-expired':
      return 'That code has expired. Request a new one.';
    case 'auth/captcha-check-failed':
      return 'The security check failed. Reload the page and try again.';
    case 'auth/invalid-app-credential':
      return window.location.hostname === 'localhost'
        ? 'Firebase does not send SMS codes to real numbers from "localhost". Open this page at http://127.0.0.1:5173 instead.'
        : 'Firebase refused the security check for this number. Reload the page and try again; if it keeps failing, the Firebase project may need billing enabled to send SMS.';
    case 'auth/network-request-failed':
      return 'No connection. Check your internet and try again.';
    case 'auth/popup-closed-by-user':
    case 'auth/cancelled-popup-request':
    case 'auth/user-cancelled':
      return 'The Google window was closed before sign-in finished. Try again.';
    case 'auth/popup-blocked':
      return 'Your browser blocked the Google sign-in window. Allow pop-ups for this site and try again.';
    case 'auth/account-exists-with-different-credential':
      return 'This email is already linked to another sign-in method.';
    case 'auth/operation-not-allowed':
      return 'This sign-in method is not enabled for the Firebase project (Authentication → Sign-in method).';
    case 'auth/billing-not-enabled':
      return 'This Firebase project needs billing enabled before it can send SMS codes.';
    case 'auth/unauthorized-domain':
      return 'This website address is not authorised for sign-in in the Firebase project (Authentication → Settings → Authorized domains).';
    default:
      return error?.message || 'Something went wrong. Please try again.';
  }
}
