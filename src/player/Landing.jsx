import { useEffect, useState } from 'react';
import { ArrowRight, ArrowUpRight, ChevronRight, Users } from 'lucide-react';
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
    <section className="match-hero">
      <div className="grid" />
      <header className="public-header">
        <Brand light />
        <Link to="/profile">MY PROFILE <ArrowUpRight size={14} /></Link>
      </header>
      <div className="hero-copy landing-copy">
        <p className="eyebrow lime">PICK-UP CRICKET, EVERY WEEK</p>
        <h1>STRANGER CRICKET.<br />REAL PEOPLE.<br />ONE MATCH AT A TIME.</h1>
        <a className="primary-button" href="#events">FIND A MATCH <ArrowRight size={17} /></a>
      </div>
    </section>
    <section className="match-body" id="events">
      <div className="section-title"><div><p className="eyebrow">OPEN NOW</p><h2>UPCOMING MATCHES</h2></div></div>
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
