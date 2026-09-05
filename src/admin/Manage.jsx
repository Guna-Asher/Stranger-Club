import { useState } from 'react';
import { ArrowLeft, Copy, ImagePlus, MapPin, Share2 } from 'lucide-react';
import Status from '../components/Status';
import List from './List';
import Matches from './Matches';
import PlayerRow from './PlayerRow';
import Proof from './Proof';
import Reject from './Reject';
import Teams from './Teams';
import { api } from '../lib/api';
import { cn, dateText, timeText } from '../lib/format';

const CANCELLABLE_REGISTRATION_STATUSES = new Set(['PENDING', 'CONFIRMED']);
const CANCELLABLE_EVENT_STATUSES = new Set(['DRAFT', 'OPEN', 'FULL']);

export default function Manage({ match, back, refresh, toast }) {
  const [tab, setTab] = useState('overview');
  const [proof, setProof] = useState(); const [reject, setReject] = useState(); const [reason, setReason] = useState(''); const [busy, setBusy] = useState(false); const pending = match.registrations.filter((p) => p.payment?.status === 'SUBMITTED'); const waitlist = match.registrations.filter((p) => p.status === 'WAITLISTED');
  const eventCancellable = CANCELLABLE_EVENT_STATUSES.has(match.status);
  const link = `${window.location.origin}/m/${match.public_id}`; const copy = async () => { await navigator.clipboard?.writeText(link); toast('Public match link copied'); }; const share = async () => { if (navigator.share) await navigator.share({ title: match.name, url: link }); else copy(); };
  const review = async (id, allow) => { setBusy(true); try { await api(`/admin/payments/${id}/${allow ? 'confirm' : 'reject'}`, { method: 'POST', headers: allow ? undefined : { 'Content-Type': 'application/json' }, body: allow ? undefined : JSON.stringify({ reason }) }); setProof(); setReject(); await refresh(); toast(allow ? 'Payment confirmed' : 'Payment rejected'); } catch (err) { toast(err.message); } finally { setBusy(false); } };
  const promote = async (id) => { try { await api(`/admin/registrations/${id}/promote`, { method: 'POST' }); await refresh(); toast('Player moved off the waitlist'); } catch (err) { toast(err.message); } };
  const cancelRegistration = async (id) => { try { await api(`/admin/registrations/${id}/cancel`, { method: 'POST' }); await refresh(); toast('Registration cancelled'); } catch (err) { toast(err.message); } };
  return <section className="manage"><button className="back" onClick={back}><ArrowLeft /> ALL MATCHES</button><div className="manage-top"><div><p className="eyebrow">{dateText(match.date)} · {timeText(match.start_time)}</p><h1>{match.name}</h1><span><MapPin />{match.venue}</span></div><Status status="CONFIRMED" /></div>
    <div className="tabs">
      <button className={cn(tab === 'overview' && 'active')} onClick={() => setTab('overview')}>OVERVIEW</button>
      <button className={cn(tab === 'teams' && 'active')} onClick={() => setTab('teams')}>TEAMS</button>
      <button className={cn(tab === 'matches' && 'active')} onClick={() => setTab('matches')}>MATCHES</button>
    </div>
    {tab === 'overview' && <><div className="metrics"><div><b>{match.confirmed_count}<i>/{match.capacity}</i></b><small>CONFIRMED</small></div><div><b>{match.pending_count}</b><small>TO REVIEW</small></div><div><b>{match.waitlist_count}</b><small>WAITLISTED</small></div></div><div className="share-buttons"><button onClick={copy}><Copy /> COPY LINK</button><button onClick={share}><Share2 /> SHARE</button></div><List title={`PAYMENTS TO REVIEW · ${pending.length}`}>{pending.length ? pending.map((p) => <button className="review" key={p.id} onClick={() => setProof(p)}><span className="avatar">{p.name[0]}</span><span><b>{p.name}</b><small>₹{p.payment.amount_due} · submitted {new Intl.DateTimeFormat('en-IN', { hour: 'numeric', minute: '2-digit' }).format(new Date(p.payment.submitted_at))}</small></span><i><ImagePlus /> VIEW</i></button>) : <p className="quiet">Nothing waiting for you. Nice.</p>}</List><List title={`SQUAD · ${match.registrations.length}`}>{match.registrations.filter((p) => p.status !== 'WAITLISTED').map((p) => <PlayerRow key={p.id} item={p} open={() => p.payment && setProof(p)} action={eventCancellable && CANCELLABLE_REGISTRATION_STATUSES.has(p.status) ? 'CANCEL' : undefined} act={() => cancelRegistration(p.id)} />)}</List>{waitlist.length > 0 && <List title={`WAITLIST · ${waitlist.length}`}>{waitlist.map((p) => <PlayerRow key={p.id} item={p} action="PROMOTE" act={() => promote(p.id)} />)}</List>}{proof && <Proof player={proof} close={() => setProof()} confirm={() => review(proof.payment.id, true)} reject={() => { setProof(); setReject(proof); }} busy={busy} />}{reject && <Reject reason={reason} setReason={setReason} close={() => setReject()} submit={() => review(reject.payment.id, false)} busy={busy} />}</>}
    {tab === 'teams' && <Teams match={match} toast={toast} />}
    {tab === 'matches' && <Matches match={match} toast={toast} />}
  </section>;
}
