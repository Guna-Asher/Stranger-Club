import { useEffect, useState } from 'react';
import { LoaderCircle, Plus } from 'lucide-react';
import Status from '../components/Status';
import { api } from '../lib/api';
import { dateText, timeText } from '../lib/format';

const NEXT_STATUS = { SCHEDULED: 'IN_PROGRESS', IN_PROGRESS: 'COMPLETED' };
const NEXT_LABEL = { SCHEDULED: 'START MATCH', IN_PROGRESS: 'MARK COMPLETED' };
const EMPTY_FORM = { team_a_id: '', team_b_id: '', scheduled_at: '', venue_override: '' };

export default function Matches({ match, toast }) {
  const [teams, setTeams] = useState();
  const [fixtures, setFixtures] = useState();
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try {
      const [teamList, fixtureList] = await Promise.all([
        api(`/admin/events/${match.id}/teams`), api(`/admin/events/${match.id}/fixtures`),
      ]);
      setTeams(teamList); setFixtures(fixtureList);
    } catch (err) { toast(err.message); }
  };
  useEffect(() => { load(); }, [match.id]);

  if (!teams || !fixtures) return null;

  const create = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api(`/admin/events/${match.id}/fixtures`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          team_a_id: +form.team_a_id, team_b_id: +form.team_b_id,
          scheduled_at: `${form.scheduled_at}:00`, venue_override: form.venue_override || undefined,
        }),
      });
      setForm(EMPTY_FORM); setCreating(false); await load(); toast('Match scheduled');
    } catch (err) { toast(err.message); } finally { setBusy(false); }
  };

  const setStatus = async (fixture, status) => {
    try {
      await api(`/admin/fixtures/${fixture.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }) });
      await load(); toast('Match updated');
    } catch (err) { toast(err.message); }
  };

  return (
    <div>
      {fixtures.length === 0 && (
        <div className="empty">
          <h2>NO MATCHES YET</h2>
          <p>Schedule a match once at least two teams exist.</p>
        </div>
      )}
      {fixtures.map((fixture, index) => (
        <section className="match-card" key={fixture.id}>
          <div>
            <span><b>MATCH {fixture.sequence || index + 1} · {fixture.team_a.name} vs {fixture.team_b.name}</b>
              <small>{dateText(fixture.scheduled_at.slice(0, 10))} · {timeText(fixture.scheduled_at.slice(11, 16))}{fixture.venue_override ? ` · ${fixture.venue_override}` : ''}</small>
            </span>
            <Status status={fixture.status} />
          </div>
          {(fixture.status === 'SCHEDULED' || fixture.status === 'IN_PROGRESS') && (
            <div className="share-buttons">
              <button onClick={() => setStatus(fixture, NEXT_STATUS[fixture.status])}>{NEXT_LABEL[fixture.status]}</button>
              <button onClick={() => setStatus(fixture, 'CANCELLED')}>CANCEL MATCH</button>
            </div>
          )}
        </section>
      ))}
      {teams.length < 2 ? (
        <p className="quiet">Create at least two teams before scheduling a match.</p>
      ) : !creating ? (
        <button className="ghost-button" onClick={() => setCreating(true)}><Plus size={16} /> SCHEDULE MATCH</button>
      ) : (
        <form className="form" onSubmit={create}>
          <div className="pair">
            <label className="field"><span>TEAM A</span>
              <select value={form.team_a_id} onChange={(e) => setForm({ ...form, team_a_id: e.target.value })} required>
                <option value="" disabled>Choose team</option>
                {teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
            </label>
            <label className="field"><span>TEAM B</span>
              <select value={form.team_b_id} onChange={(e) => setForm({ ...form, team_b_id: e.target.value })} required>
                <option value="" disabled>Choose team</option>
                {teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
            </label>
          </div>
          <label className="field"><span>SCHEDULED TIME</span><input type="datetime-local" value={form.scheduled_at} onChange={(e) => setForm({ ...form, scheduled_at: e.target.value })} required /></label>
          <label className="field"><span>VENUE OVERRIDE (OPTIONAL)</span><input value={form.venue_override} onChange={(e) => setForm({ ...form, venue_override: e.target.value })} placeholder={match.venue} /></label>
          <button className="primary-button" disabled={busy}>{busy ? <LoaderCircle className="spin" /> : 'SCHEDULE MATCH'}</button>
        </form>
      )}
    </div>
  );
}
