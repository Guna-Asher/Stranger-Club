import { useState } from 'react';
import { ChevronRight, LoaderCircle } from 'lucide-react';
import Brand from '../components/Brand';
import Field from '../components/Field';
import FormError from '../components/FormError';
import { api, setCsrfToken } from '../lib/api';

export default function AdminLogin() {
  const [username, setUsername] = useState(''); const [password, setPassword] = useState(''); const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  const submit = async (event) => { event.preventDefault(); setBusy(true); setError(''); try { const auth = await api('/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }) }); setCsrfToken(auth.csrf_token); window.location.assign('/admin'); } catch (err) { setError(err.message); } finally { setBusy(false); } };
  return <main className="login-page"><Brand /><section><p className="eyebrow">ORGANIZER ACCESS</p><h1>WELCOME BACK.</h1><p>Sign in to manage events and payments.</p><form className="form" onSubmit={submit}><Field label="USERNAME" value={username} set={setUsername} autoComplete="username" required /><Field label="PASSWORD" type="password" value={password} set={setPassword} autoComplete="current-password" required /><FormError>{error}</FormError><button className="primary-button" disabled={busy}>{busy ? <LoaderCircle className="spin" /> : 'SIGN IN'}<ChevronRight /></button></form></section></main>;
}
