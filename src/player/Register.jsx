import { useEffect, useState } from 'react';
import { ChevronRight, LoaderCircle, ShieldCheck } from 'lucide-react';
import Field from '../components/Field';
import FlowHeader from '../components/FlowHeader';
import FormError from '../components/FormError';
import PlayerAuth from './PlayerAuth';
import { api } from '../lib/api';

export default function Register({ match, back, done, verified, onVerified }) {
  if (!verified) return <PlayerAuth back={back} onAuthenticated={onVerified} />;
  return <RegisterDetails match={match} back={back} done={done} />;
}

function RegisterDetails({ match, back, done }) {
  const [form, setForm] = useState({ name: '', email: '', preferred_position: 'NO_PREFERENCE' }); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  // A returning player already has a persistent PlayerProfile — prefill what
  // it already knows so they never retype their own name/role for an event
  // they're joining. Only fills still-blank fields, so it never clobbers
  // anything the player has already typed by the time this resolves. Also
  // remembered here so submit() below can tell a still-blank profile (first
  // event ever) from one that already has a name.
  const [profileHasName, setProfileHasName] = useState(true);
  useEffect(() => {
    (async () => {
      try {
        const profile = await api('/player/profile', { authScope: 'player' });
        setProfileHasName(Boolean(profile.display_name));
        setForm((current) => ({
          ...current,
          name: current.name || profile.display_name || '',
          preferred_position: current.preferred_position === 'NO_PREFERENCE' && profile.cricket_role !== 'NO_PREFERENCE'
            ? profile.cricket_role : current.preferred_position,
        }));
      } catch { /* no profile yet, or not reachable — the blank form is still correct */ }
    })();
  }, []);
  const submit = async (e) => {
    e.preventDefault(); setBusy(true);
    try {
      const registration = await api(`/events/${match.public_id}/registrations`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(form), authScope: 'player' });
      // First-ever registration seeds the player's persistent profile with
      // the name they just gave, so their Profile page never shows blank —
      // best-effort: a failure here shouldn't block the registration they
      // already completed.
      if (!profileHasName) {
        api('/player/profile', {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' }, authScope: 'player',
          body: JSON.stringify({ display_name: form.name, cricket_role: form.preferred_position }),
        }).catch(() => {});
      }
      done(registration);
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  };
  return <section className="flow"><FlowHeader step="01" label="YOUR DETAILS" back={back} /><div className="intro"><p className="eyebrow">SUNDAY CRICKET</p><h1>WHO’S PLAYING?</h1><p>Just the essentials. No account required.</p></div><form onSubmit={submit} className="form"><Field label="FULL NAME" placeholder="Your name" value={form.name} set={(v) => setForm({ ...form, name: v })} autoFocus required /><Field label="EMAIL · OPTIONAL" placeholder="For your match confirmation" type="email" value={form.email} set={(v) => setForm({ ...form, email: v })} /><label className="field"><span>PREFERRED ROLE · OPTIONAL</span><select value={form.preferred_position} onChange={(event) => setForm({ ...form, preferred_position: event.target.value })}><option value="NO_PREFERENCE">No preference</option><option value="BATSMAN">Batsman</option><option value="BOWLER">Bowler</option><option value="ALL_ROUNDER">All-rounder</option><option value="WICKET_KEEPER">Wicket keeper</option></select></label><FormError>{error}</FormError><button className="primary-button" disabled={busy || !form.name}>{busy ? <LoaderCircle className="spin" /> : 'CONTINUE'}<ChevronRight /></button></form><p className="privacy"><ShieldCheck size={16} /> Your details are only used to organise this match.</p></section>;
}
