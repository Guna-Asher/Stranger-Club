import { useState } from 'react';
import { ChevronRight, LoaderCircle, ShieldCheck } from 'lucide-react';
import Field from '../components/Field';
import FlowHeader from '../components/FlowHeader';
import FormError from '../components/FormError';
import { api, setCsrfToken } from '../lib/api';

// Email + password: the current player onboarding/login path. Avatar
// assignment happens server-side during signup (reusing the existing avatar
// system) — see backend/app/services_player.create_player_account. Phone
// verification via OTP remains available in the backend for later reuse but
// is not part of this flow.
export default function PlayerAuth({ back, onAuthenticated }) {
  const [mode, setMode] = useState('login');
  return mode === 'login'
    ? <LoginForm back={back} onAuthenticated={onAuthenticated} switchToSignup={() => setMode('signup')} />
    : <SignupForm back={back} onAuthenticated={onAuthenticated} switchToLogin={() => setMode('login')} />;
}

function LoginForm({ back, onAuthenticated, switchToSignup }) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const submit = async (e) => {
    e.preventDefault(); setBusy(true); setError('');
    try {
      const auth = await api('/player/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password }) });
      setCsrfToken(auth.csrf_token, 'player');
      onAuthenticated();
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  };

  return <section className="flow">
    <FlowHeader step="01" label="PLAYER LOGIN" back={back} />
    <div className="intro"><p className="eyebrow">STRANGER CLUB</p><h1>WELCOME BACK</h1><p>Log in to join matches and see your teams.</p></div>
    <form onSubmit={submit} className="form">
      <Field label="EMAIL" placeholder="you@example.com" type="email" value={email} set={setEmail} autoFocus required />
      <Field label="PASSWORD" placeholder="Your password" type="password" value={password} set={setPassword} required />
      <FormError>{error}</FormError>
      <button className="primary-button" disabled={busy || !email || !password}>{busy ? <LoaderCircle className="spin" /> : 'LOG IN'}<ChevronRight /></button>
    </form>
    <p className="signup-line">New to Stranger Club? <a href="#" onClick={(e) => { e.preventDefault(); switchToSignup(); }}>Create a player account</a></p>
    <p className="privacy"><ShieldCheck size={16} /> Your details are only used to organise this match.</p>
  </section>;
}

function SignupForm({ back, onAuthenticated, switchToLogin }) {
  const [form, setForm] = useState({ name: '', email: '', phone: '', password: '', confirm_password: '' });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const set = (field) => (value) => setForm({ ...form, [field]: value });

  const submit = async (e) => {
    e.preventDefault(); setBusy(true); setError('');
    if (form.password !== form.confirm_password) { setError('Passwords do not match'); setBusy(false); return; }
    try {
      const auth = await api('/player/signup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(form) });
      setCsrfToken(auth.csrf_token, 'player');
      onAuthenticated();
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  };

  const ready = form.name && form.email && form.phone.length >= 10 && form.password.length >= 8 && form.confirm_password;
  return <section className="flow">
    <FlowHeader step="01" label="CREATE ACCOUNT" back={back} />
    <div className="intro"><p className="eyebrow">STRANGER CLUB</p><h1>WHO’S PLAYING?</h1><p>Set up your player account to join a match.</p></div>
    <form onSubmit={submit} className="form">
      <Field label="FULL NAME" placeholder="Your name" value={form.name} set={set('name')} autoFocus required />
      <Field label="EMAIL" placeholder="you@example.com" type="email" value={form.email} set={set('email')} required />
      <Field label="MOBILE NUMBER" placeholder="10-digit number" type="tel" inputMode="numeric" value={form.phone} set={set('phone')} required />
      <Field label="PASSWORD" placeholder="At least 8 characters" type="password" value={form.password} set={set('password')} required />
      <Field label="CONFIRM PASSWORD" placeholder="Re-enter your password" type="password" value={form.confirm_password} set={set('confirm_password')} required />
      <FormError>{error}</FormError>
      <button className="primary-button" disabled={busy || !ready}>{busy ? <LoaderCircle className="spin" /> : 'CREATE ACCOUNT'}<ChevronRight /></button>
    </form>
    <p className="signup-line">Already have an account? <a href="#" onClick={(e) => { e.preventDefault(); switchToLogin(); }}>Log in</a></p>
    <p className="privacy"><ShieldCheck size={16} /> Your details are only used to organise this match.</p>
  </section>;
}
