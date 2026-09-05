import { useState } from 'react';
import { ChevronRight, LoaderCircle, ShieldCheck } from 'lucide-react';
import Field from '../components/Field';
import FlowHeader from '../components/FlowHeader';
import FormError from '../components/FormError';
import { api } from '../lib/api';

export default function Register({ match, back, done }) {
  const [form, setForm] = useState({ name: '', phone: '', email: '', preferred_position: 'NO_PREFERENCE' }); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const submit = async (e) => { e.preventDefault(); setBusy(true); try { done(await api(`/events/${match.public_id}/registrations`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(form) })); } catch (err) { setError(err.message); } finally { setBusy(false); } };
  return <section className="flow"><FlowHeader step="01" label="YOUR DETAILS" back={back} /><div className="intro"><p className="eyebrow">SUNDAY CRICKET</p><h1>WHO’S PLAYING?</h1><p>Just the essentials. No account required.</p></div><form onSubmit={submit} className="form"><Field label="FULL NAME" placeholder="Your name" value={form.name} set={(v) => setForm({ ...form, name: v })} autoFocus required /><Field label="MOBILE NUMBER" placeholder="10-digit number" type="tel" inputMode="numeric" value={form.phone} set={(v) => setForm({ ...form, phone: v })} required /><Field label="EMAIL · OPTIONAL" placeholder="For your match confirmation" type="email" value={form.email} set={(v) => setForm({ ...form, email: v })} /><label className="field"><span>PREFERRED ROLE · OPTIONAL</span><select value={form.preferred_position} onChange={(event) => setForm({ ...form, preferred_position: event.target.value })}><option value="NO_PREFERENCE">No preference</option><option value="BATSMAN">Batsman</option><option value="BOWLER">Bowler</option><option value="ALL_ROUNDER">All-rounder</option><option value="WICKET_KEEPER">Wicket keeper</option></select></label><FormError>{error}</FormError><button className="primary-button" disabled={busy || !form.name || !form.phone}>{busy ? <LoaderCircle className="spin" /> : 'CONTINUE'}<ChevronRight /></button></form><p className="privacy"><ShieldCheck size={16} /> Your details are only used to organise this match.</p></section>;
}
