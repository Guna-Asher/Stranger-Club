import { useEffect, useState } from 'react';
import { ArrowUpRight, Plus, X } from 'lucide-react';
import { api } from '../lib/api';

export default function Teams({ match, toast }) {
  const [teams, setTeams] = useState();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [assigning, setAssigning] = useState();
  const [busy, setBusy] = useState(false);

  const load = async () => { try { setTeams(await api(`/admin/events/${match.id}/teams`)); } catch (err) { toast(err.message); } };
  useEffect(() => { load(); }, [match.id]);

  if (!teams) return null;

  const assignedRegistrationIds = new Set(teams.flatMap((t) => t.members.map((m) => m.registration_id)));
  const eligible = match.registrations.filter((r) => r.status === 'CONFIRMED' && !assignedRegistrationIds.has(r.id));

  const createTeam = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api(`/admin/events/${match.id}/teams`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
      setName(''); setCreating(false); await load(); toast('Team created');
    } catch (err) { toast(err.message); } finally { setBusy(false); }
  };

  const removeTeam = async (team) => {
    try { await api(`/admin/teams/${team.id}`, { method: 'DELETE' }); await load(); toast('Team removed'); }
    catch (err) { toast(err.message); }
  };

  const assign = async (registrationId) => {
    try {
      await api(`/admin/teams/${assigning.id}/members`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ registration_id: registrationId }) });
      setAssigning(); await load(); toast('Player assigned');
    } catch (err) { toast(err.message); }
  };

  const removeMember = async (member) => {
    try { await api(`/admin/team-members/${member.id}`, { method: 'DELETE' }); await load(); toast('Player removed from team'); }
    catch (err) { toast(err.message); }
  };

  return (
    <div>
      {teams.length === 0 && !creating && (
        <div className="empty">
          <h2>NO TEAMS YET</h2>
          <p>Create teams to start assigning confirmed players.</p>
          <button className="primary-button" onClick={() => setCreating(true)}><Plus size={16} /> CREATE TEAM</button>
        </div>
      )}
      {teams.map((team) => (
        <section className="list" key={team.id}>
          <h2>{team.name.toUpperCase()} · {team.member_count}{team.max_size ? `/${team.max_size}` : ''}</h2>
          <div>
            {team.members.map((member) => (
              <div className="player-row" key={member.id}>
                <span className="avatar">{member.player_name[0]}</span>
                <span><b>{member.player_name}</b><small>{(member.assigned_position || member.preferred_position || 'NO_PREFERENCE').replaceAll('_', ' ')}</small></span>
                <button onClick={() => removeMember(member)}><X size={15} /></button>
              </div>
            ))}
            {team.members.length === 0 && <p className="quiet">No players yet.</p>}
          </div>
          <div className="share-buttons">
            <button onClick={() => setAssigning(team)}>ASSIGN PLAYER</button>
            <button onClick={() => removeTeam(team)}>REMOVE TEAM</button>
          </div>
        </section>
      ))}
      {teams.length > 0 && !creating && <button className="ghost-button" onClick={() => setCreating(true)}><Plus size={16} /> ADD ANOTHER TEAM</button>}
      {creating && (
        <form className="form" onSubmit={createTeam}>
          <label className="field"><span>TEAM NAME</span><input value={name} onChange={(e) => setName(e.target.value)} required autoFocus /></label>
          <button className="primary-button" disabled={busy || !name.trim()}>{busy ? 'CREATING…' : 'CREATE TEAM'}</button>
        </form>
      )}
      {assigning && (
        <div className="overlay">
          <section className="sheet">
            <button className="close" onClick={() => setAssigning()}><X /></button>
            <p className="eyebrow">ASSIGN TO {assigning.name.toUpperCase()}</p>
            <h2>CONFIRMED PLAYERS</h2>
            <div className="list"><div>
              {eligible.length === 0 && <p className="quiet">Every confirmed player already has a team.</p>}
              {eligible.map((r) => (
                <button className="player-row" key={r.id} onClick={() => assign(r.id)}>
                  <span className="avatar">{r.name[0]}</span>
                  <span><b>{r.name}</b><small>{(r.preferred_position || 'NO_PREFERENCE').replaceAll('_', ' ')}</small></span>
                  <ArrowUpRight size={16} />
                </button>
              ))}
            </div></div>
          </section>
        </div>
      )}
    </div>
  );
}
