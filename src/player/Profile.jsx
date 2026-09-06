import { useEffect, useState } from 'react';
import { ArrowLeft, ChevronRight, LoaderCircle, Pencil, RefreshCw, Shuffle } from 'lucide-react';
import { Link, useNavigate } from 'react-router-dom';
import Avatar from '../components/Avatar';
import Brand from '../components/Brand';
import Field from '../components/Field';
import FormError from '../components/FormError';
import Loading from '../components/Loading';
import ErrorPage from '../components/ErrorPage';
import Status from '../components/Status';
import Toast from '../components/Toast';
import AvatarPicker from './AvatarPicker';
import PhoneVerify from './PhoneVerify';
import { api, setCsrfToken } from '../lib/api';
import { dateText, timeText } from '../lib/format';

const ROLE_LABEL = {
  NO_PREFERENCE: 'No preference', BATSMAN: 'Batsman', BOWLER: 'Bowler',
  ALL_ROUNDER: 'All-rounder', WICKET_KEEPER: 'Wicket keeper',
};

// An event whose own status has reached one of these is done — its
// registration no longer belongs alongside still-current events (see
// services.player_dashboard: event.status is the only thing that decides
// this, never date, never whether one of its matches happened to finish).
const EVENT_CONCLUDED_STATUSES = new Set(['COMPLETED', 'CANCELLED']);

// Merges registration + payment status the same way lib/format.playerStatus
// does, for the shape the dashboard endpoint returns (payment_status is a
// flat field here, not a nested payment object).
const registrationStatus = (r) => (r.status !== 'CANCELLED' && r.payment_status === 'SUBMITTED' ? 'PAYMENT_SUBMITTED' : r.status);

function AwardRow({ label, award }) {
  if (!award) return null;
  return <div className="award-row"><small>{label}</small><Avatar designId={award.avatar_design_id} name={award.name} size={22} /><b>{award.name}</b></div>;
}

export default function Profile() {
  const navigate = useNavigate();
  const [checked, setChecked] = useState(false);
  const [verified, setVerified] = useState(false);
  const [profile, setProfile] = useState();
  const [dashboard, setDashboard] = useState();
  const [error, setError] = useState('');
  const [editing, setEditing] = useState(false);
  const [avatarSheetOpen, setAvatarSheetOpen] = useState(false);
  const [form, setForm] = useState();
  const [formError, setFormError] = useState('');
  const [busy, setBusy] = useState(false);
  const [avatarBusy, setAvatarBusy] = useState(false);
  const [toast, setToast] = useState('');

  const checkAuth = async () => {
    try { const auth = await api('/player/me', { authScope: 'player' }); setCsrfToken(auth.csrf_token, 'player'); setVerified(true); }
    catch { setVerified(false); }
    finally { setChecked(true); }
  };
  useEffect(() => { checkAuth(); }, []);

  const load = async () => {
    try {
      const [p, m] = await Promise.all([
        api('/player/profile', { authScope: 'player' }), api('/player/matches', { authScope: 'player' }),
      ]);
      setProfile(p); setDashboard(m);
      setForm({ display_name: p.display_name || '', cricket_role: p.cricket_role, bio: p.bio || '' });
    } catch (err) { setError(err.message); }
  };
  useEffect(() => { if (verified) load(); }, [verified]);

  if (!checked) return <Loading />;
  if (!verified) return <PhoneVerify back={() => navigate('/')} onVerified={() => setVerified(true)} />;
  if (error) return <ErrorPage message={error} />;
  if (!profile || !dashboard) return <Loading />;

  const save = async (e) => {
    e.preventDefault(); setBusy(true); setFormError('');
    try {
      const saved = await api('/player/profile', {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(form), authScope: 'player',
      });
      setProfile(saved); setEditing(false); setToast('Profile updated');
    } catch (err) { setFormError(err.message); } finally { setBusy(false); }
  };

  const cancelEdit = () => {
    setForm({ display_name: profile.display_name || '', cricket_role: profile.cricket_role, bio: profile.bio || '' });
    setFormError(''); setEditing(false);
  };

  const generateAvatar = async () => {
    setAvatarBusy(true);
    try { setProfile(await api('/player/avatar/generate', { method: 'POST', authScope: 'player' })); setToast('New avatar generated'); }
    catch (err) { setToast(err.message); } finally { setAvatarBusy(false); }
  };

  // Seeds the per-event "which registration is mine" key PlayerApp reads on
  // load, so continuing into an event from here lands on the player's own
  // status ticket instead of the generic join screen.
  const openEvent = (registration) => { try { localStorage.setItem(`sc-registration-${registration.event.public_id}`, registration.public_id); } catch { /* best-effort */ } };

  // A concluded event's registration is already represented (if it matters)
  // under RECENT RESULTS below — keeping it here too would show the same
  // event as both current and past at once.
  const currentRegistrations = dashboard.registrations.filter((r) => !EVENT_CONCLUDED_STATUSES.has(r.event.status));

  return <section className="flow profile-page">
    <Toast text={toast} clear={() => setToast('')} />
    <header className="flow-header"><button onClick={() => navigate('/')}><ArrowLeft /></button><span>MY PROFILE</span><Brand /></header>
    <div className="intro"><p className="eyebrow">STRANGER CLUB PLAYER</p><h1>{profile.display_name || 'YOUR PROFILE'}</h1></div>

    <div className="profile-identity">
      <Avatar designId={profile.avatar_design_id} name={profile.display_name} size={64} />
      <div><b>{profile.display_name || 'No name set yet'}</b><span>{ROLE_LABEL[profile.cricket_role] || profile.cricket_role}</span></div>
    </div>
    {profile.avatar_design_id == null ? (
      <div className="form-actions">
        <button className="primary-button" disabled={avatarBusy} onClick={generateAvatar}>
          {avatarBusy ? <LoaderCircle className="spin" /> : <><Shuffle size={16} /> GENERATE NEW</>}
        </button>
        <button className="ghost-button" onClick={() => setAvatarSheetOpen(true)}>CHOOSE AVATAR</button>
      </div>
    ) : (
      <button className="ghost-button" onClick={() => setAvatarSheetOpen(true)}><RefreshCw size={15} /> CHANGE AVATAR</button>
    )}

    {editing && (
      <form className="form" onSubmit={save}>
        <Field label="DISPLAY NAME" placeholder="Your name" value={form.display_name} set={(v) => setForm({ ...form, display_name: v })} />
        <label className="field"><span>CRICKET ROLE</span>
          <select value={form.cricket_role} onChange={(e) => setForm({ ...form, cricket_role: e.target.value })}>
            {Object.entries(ROLE_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <label className="field"><span>BIO · OPTIONAL</span><textarea value={form.bio} maxLength={500} onChange={(e) => setForm({ ...form, bio: e.target.value })} /></label>
        <FormError>{formError}</FormError>
        <div className="form-actions">
          <button className="primary-button" disabled={busy}>{busy ? <LoaderCircle className="spin" /> : 'SAVE PROFILE'}</button>
          <button type="button" className="ghost-button" disabled={busy} onClick={cancelEdit}>CANCEL</button>
        </div>
      </form>
    )}
    {!editing && <button className="ghost-button" onClick={() => setEditing(true)}><Pencil size={15} /> EDIT PROFILE</button>}
    {profile.bio && !editing && <p className="quiet">{profile.bio}</p>}

    {dashboard.upcoming.length > 0 && <>
      <div className="section-title"><div><p className="eyebrow">NEXT UP</p><h2>UPCOMING</h2></div></div>
      {dashboard.upcoming.map((f) => (
        <div className="ticket" key={f.id}>
          <div><b>VS {f.opponent ? f.opponent.name.toUpperCase() : 'TBD'}</b><span>{dateText(f.scheduled_at.slice(0, 10))} · {timeText(f.scheduled_at.slice(11, 16))}</span></div>
          <Status status={f.status} />
        </div>
      ))}
    </>}

    {currentRegistrations.length > 0 && <>
      <div className="section-title"><div><p className="eyebrow">MY EVENTS</p><h2>REGISTRATIONS</h2></div></div>
      {currentRegistrations.map((r) => (
        <Link className="match-row" to={`/events/${r.event.public_id}`} onClick={() => openEvent(r)} key={r.public_id}>
          <span><small>{dateText(r.event.date)}</small><b>{r.event.name}</b><i>{r.event.venue}{r.team ? ` · ${r.team.name.toUpperCase()}` : ''}</i></span>
          <Status status={registrationStatus(r)} />
          <ChevronRight />
        </Link>
      ))}
    </>}

    {dashboard.completed.length > 0 && <>
      <div className="section-title"><div><p className="eyebrow">RECENT RESULTS</p><h2>COMPLETED</h2></div></div>
      {dashboard.completed.map((f) => (
        <div className="ticket" key={f.id}>
          <div>
            <b>{f.result_type === 'DRAW' ? 'MATCH DRAWN' : f.result_type === 'NO_RESULT' ? 'NO RESULT' : `${f.winning_team?.name.toUpperCase()} WON`}</b>
            <span>{f.opponent ? `VS ${f.opponent.name} · ` : ''}{dateText(f.scheduled_at.slice(0, 10))}</span>
          </div>
          <AwardRow label="PLAYER OF THE MATCH" award={f.player_of_match} />
          <AwardRow label="BEST BATTER" award={f.best_batter} />
          <AwardRow label="BEST BOWLER" award={f.best_bowler} />
          <footer><small>YOU PLAYED</small><strong>{f.participated ? 'YES' : 'NO'}</strong></footer>
        </div>
      ))}
    </>}

    {avatarSheetOpen && (
      <AvatarPicker
        close={() => setAvatarSheetOpen(false)}
        onChanged={(p) => { setProfile(p); setToast('Avatar updated'); }}
        toast={setToast}
      />
    )}
  </section>;
}
