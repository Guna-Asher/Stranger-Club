import { ChevronRight, Plus, Users } from 'lucide-react';
import Stat from './Stat';
import { dateText, timeText } from '../lib/format';

export default function Dashboard({ matches, choose, create }) {
  // Classification is by the event's own status alone — never by date, and
  // never by whether any one of its matches happened to finish (a Fixture
  // completing is never allowed to complete the Event itself; see
  // services.VALID_EVENT_TRANSITIONS). An event only ever leaves Upcoming
  // once an organizer explicitly marks the event COMPLETED or CANCELLED.
  const CONCLUDED_STATUSES = new Set(['COMPLETED', 'CANCELLED']);
  const total = matches.reduce((a, m) => ({ pending: a.pending + m.payment_submitted_count, confirmed: a.confirmed + m.confirmed_count, waitlist: a.waitlist + m.waitlist_count, revenue: a.revenue + m.collected_amount }), { pending: 0, confirmed: 0, waitlist: 0, revenue: 0 }); const upcoming = matches.filter((item) => !CONCLUDED_STATUSES.has(item.status)); const historical = matches.filter((item) => CONCLUDED_STATUSES.has(item.status));
  const EventRows = ({ items, empty }) => items.length ? items.map((match) => <button className="match-row" key={match.id} onClick={() => choose(match.id)}><span><small>{dateText(match.date)} · {match.status}</small><b>{match.name}</b><i>{match.venue} · {timeText(match.start_time)} · {match.payment_submitted_count} TO REVIEW</i></span><strong>{match.confirmed_count}<i>/{match.capacity}</i><small>PLAYERS</small></strong><ChevronRight /></button>) : <div className="empty"><Users /><h2>{empty}</h2><p>Create an event when the next game is confirmed.</p></div>;
  return <><section className="admin-hero"><p className="eyebrow lime">ORGANIZER HQ</p><h1>THIS WEEK’S<br />PLAYBOOK.</h1><p>Everything that needs your attention, in one place.</p></section><section className="admin-body"><div className="ops"><Stat text="TO REVIEW" value={total.pending} color="orange" /><Stat text="CONFIRMED" value={total.confirmed} color="green" /><Stat text="WAITLIST" value={total.waitlist} color="blue" /><Stat text="COLLECTED" value={`₹${total.revenue}`} color="black" /></div><div className="section-title"><div><p className="eyebrow">UPCOMING</p><h2>EVENTS</h2></div><button className="round-button" onClick={create}><Plus /></button></div><EventRows items={upcoming} empty="NO UPCOMING EVENTS" />{historical.length > 0 && <><div className="section-title"><div><p className="eyebrow">HISTORY</p><h2>PAST EVENTS</h2></div></div><EventRows items={historical} empty="NO PAST EVENTS" /></>}</section></>;
}
