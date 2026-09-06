import { useEffect, useState } from 'react';
import { ArrowRight, ArrowUpRight, ChevronRight, ShieldCheck, Users } from 'lucide-react';
import { Link } from 'react-router-dom';
import Brand from '../components/Brand';
import { api } from '../lib/api';
import { dateText, timeText } from '../lib/format';

export default function Landing() {
  const [events, setEvents] = useState();
  const [error, setError] = useState('');

  useEffect(() => {
    (async () => {
      try { setEvents(await api('/events')); } catch (err) { setError(err.message); }
    })();
  }, []);

  return <>
    <section className="match-hero landing-hero">
      <div className="grid" />
      <header className="public-header">
        <Brand light />
        <Link to="/profile">MY PROFILE <ArrowUpRight size={14} /></Link>
      </header>
      <div className="hero-copy landing-copy">
        <h1>Come alone.<br />Play cricket<br /><span className="lime">with strangers.</span></h1>
        <p className="sub">No team, no problem. Join a match, get<br />placed on a side, and actually play.</p>
        <p className="hero-meta"><Users size={16} /> Weekly matches <span className="meta-dot">•</span> Verified organizers</p>
        <a className="primary-button" href="#events">Find a Match <ArrowRight size={17} /></a>

        <p className="continue-divider">OR CONTINUE AS</p>

        <div className="continue-cards">
          <Link className="continue-card" to="/profile">
            <span className="icon"><Users size={20} /></span>
            <span className="info">
              <b>Player login</b>
              <span>Join games, view teams, matches &amp; history</span>
            </span>
            <ArrowRight className="arrow" size={18} />
          </Link>
          <a className="continue-card" href="/admin/login">
            <span className="icon"><ShieldCheck size={20} /></span>
            <span className="info">
              <b>Organizer / Admin login</b>
              <span>Create events, verify payments &amp; manage rosters</span>
            </span>
            <ArrowRight className="arrow" size={18} />
          </a>
        </div>

        <p className="signup-line">New to Strangers Club? <Link to="/profile">Create a player account</Link></p>
      </div>
    </section>
    <section className="match-body" id="events">
      <div className="section-title"><div><p className="eyebrow">OPEN NOW</p><h2>UPCOMING EVENTS</h2></div></div>
      {error && <p className="form-error">{error}</p>}
      {events && events.length === 0 && (
        <div className="empty"><Users /><h2>NO OPEN MATCHES</h2><p>Check back soon — new matches are added every week.</p></div>
      )}
      {(events || []).map((event) => (
        <Link className="match-row" to={`/events/${event.public_id}`} key={event.public_id}>
          <span>
            <small>{dateText(event.date)} · {event.status}</small>
            <b>{event.name}</b>
            <i>{event.venue} · {timeText(event.start_time)} · {event.available_slots > 0 ? `${event.available_slots} SPOTS LEFT` : 'WAITLIST OPEN'}</i>
          </span>
          <strong>{event.confirmed_count}<i>/{event.capacity}</i></strong>
          <ChevronRight />
        </Link>
      ))}
    </section>
  </>;
}
