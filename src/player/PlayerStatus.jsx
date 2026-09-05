import { useState } from 'react';
import { ArrowLeft, CalendarPlus, CheckCircle2, ChevronRight, Share2 } from 'lucide-react';
import Status from '../components/Status';
import CancelConfirm from './CancelConfirm';
import { api } from '../lib/api';
import { cn, dateText, playerStatus, timeText } from '../lib/format';

const CANCELLABLE_REGISTRATION_STATUSES = new Set(['PENDING', 'WAITLISTED', 'CONFIRMED']);
const CANCELLABLE_EVENT_STATUSES = new Set(['DRAFT', 'OPEN', 'FULL']);

export default function PlayerStatus({ match, registration, back, retry, refresh, rejoin, onCancelled, toast }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const confirmed = registration.status === 'CONFIRMED'; const rejected = registration.status === 'REJECTED'; const waitlisted = registration.status === 'WAITLISTED'; const cancelled = registration.status === 'CANCELLED'; const submitted = !cancelled && registration.payment?.status === 'SUBMITTED';
  const content = cancelled ? ['REGISTRATION CANCELLED', 'You’re no longer in this match. You can join again if there’s space.'] : confirmed ? ['YOU’RE IN!', 'Your payment is confirmed. Arrive 15 minutes early and bring your game.'] : submitted ? ['PAYMENT IN REVIEW', 'We have your screenshot. The organiser will confirm your spot shortly.'] : waitlisted ? ['YOU’RE WAITLISTED', 'The squad is full. We’ll notify you if a spot opens up.'] : ['PAYMENT NEEDS ATTENTION', registration.payment?.rejection_reason || 'Please submit a new payment screenshot.'];
  const cancellable = CANCELLABLE_REGISTRATION_STATUSES.has(registration.status) && CANCELLABLE_EVENT_STATUSES.has(match.status);
  const share = async () => { if (navigator.share) await navigator.share({ title: match.name, url: window.location.href }); else { await navigator.clipboard?.writeText(window.location.href); toast('Match link copied'); } };
  const calendar = () => { const start = `${match.date.replaceAll('-', '')}T${match.start_time.replaceAll(':', '')}00`; const end = `${match.date.replaceAll('-', '')}T${match.end_time.replaceAll(':', '')}00`; const file = new Blob([`BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nDTSTART;TZID=Asia/Kolkata:${start}\nDTEND;TZID=Asia/Kolkata:${end}\nSUMMARY:${match.name}\nLOCATION:${match.venue}\nEND:VEVENT\nEND:VCALENDAR`], { type: 'text/calendar' }); const a = document.createElement('a'); a.href = URL.createObjectURL(file); a.download = 'stranger-club-match.ics'; a.click(); };
  const cancelRegistration = async () => { setBusy(true); try { const updated = await api(`/registrations/${registration.public_id}/cancel`, { method: 'POST', authScope: 'player' }); onCancelled(updated); setConfirming(false); toast('Registration cancelled'); } catch (err) { toast(err.message); } finally { setBusy(false); } };
  return <section className="status-page"><button className="back" onClick={back}><ArrowLeft /> MATCH DETAILS</button><div className={cn('status-icon', confirmed && 'success', rejected && 'danger')}><CheckCircle2 /></div><p className="eyebrow">{dateText(match.date)}</p><h1>{content[0]}</h1><p>{content[1]}</p><div className="ticket"><div><b>{match.name}</b><span>{timeText(match.start_time)} · {match.venue}</span></div><Status status={playerStatus(registration)} /><footer><small>PAYMENT</small><strong>{confirmed ? `₹${registration.payment?.amount_due}` : submitted ? 'VERIFYING' : waitlisted ? 'WAITLISTED' : cancelled ? '—' : 'REUPLOAD'}</strong></footer></div>{confirmed && <button className="primary-button" onClick={calendar}><CalendarPlus /> ADD TO CALENDAR</button>}{submitted && <button className="primary-button" onClick={refresh}>REFRESH STATUS</button>}{rejected && <button className="primary-button" onClick={retry}>UPLOAD NEW PROOF</button>}{cancelled && <button className="primary-button" onClick={rejoin}>JOIN AGAIN<ChevronRight /></button>}{cancellable && <button className="ghost-button" onClick={() => setConfirming(true)}>CANCEL REGISTRATION</button>}<button className="ghost-button" onClick={share}><Share2 /> SHARE MATCH</button>{confirming && <CancelConfirm close={() => setConfirming(false)} confirm={cancelRegistration} busy={busy} />}</section>;
}
