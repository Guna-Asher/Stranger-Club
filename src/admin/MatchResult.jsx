import { useEffect, useState } from 'react';
import { LoaderCircle } from 'lucide-react';
import { api } from '../lib/api';

const RESULT_OPTIONS = (fixture) => [
  { value: 'TEAM_A_WIN', label: `${fixture.team_a.name.toUpperCase()} WIN` },
  { value: 'TEAM_B_WIN', label: `${fixture.team_b.name.toUpperCase()} WIN` },
  { value: 'DRAW', label: 'DRAW' },
  { value: 'NO_RESULT', label: 'NO RESULT' },
];

export default function MatchResult({ fixture, eventId, toast }) {
  const [rosters, setRosters] = useState();
  const [participantIds, setParticipantIds] = useState(new Set());
  const [resultType, setResultType] = useState('');
  const [mvpId, setMvpId] = useState('');
  const [bestBatterId, setBestBatterId] = useState('');
  const [bestBowlerId, setBestBowlerId] = useState('');
  const [notes, setNotes] = useState('');
  const [hasExistingResult, setHasExistingResult] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try {
      const teams = await api(`/admin/events/${eventId}/teams`);
      const teamA = teams.find((t) => t.id === fixture.team_a.id);
      const teamB = teams.find((t) => t.id === fixture.team_b.id);
      setRosters({ teamA, teamB });

      const participants = await api(`/admin/fixtures/${fixture.id}/participants`);
      setParticipantIds(new Set(participants.map((p) => p.registration_id)));

      try {
        const result = await api(`/admin/fixtures/${fixture.id}/result`);
        setHasExistingResult(true);
        setResultType(result.result_type);
        setMvpId(result.player_of_match ? String(result.player_of_match.registration_id) : '');
        setBestBatterId(result.best_batter ? String(result.best_batter.registration_id) : '');
        setBestBowlerId(result.best_bowler ? String(result.best_bowler.registration_id) : '');
        setNotes(result.notes || '');
      } catch {
        setHasExistingResult(false);
      }
    } catch (err) { toast(err.message); }
  };
  useEffect(() => { load(); }, [fixture.id]);

  if (!rosters) return null;

  const allMembers = [
    ...rosters.teamA.members.map((m) => ({ ...m, team_id: rosters.teamA.id, team_name: rosters.teamA.name })),
    ...rosters.teamB.members.map((m) => ({ ...m, team_id: rosters.teamB.id, team_name: rosters.teamB.name })),
  ];
  const participants = allMembers.filter((m) => participantIds.has(m.registration_id));

  const toggleParticipant = (registrationId) => {
    const next = new Set(participantIds);
    if (next.has(registrationId)) {
      next.delete(registrationId);
      // Unchecking someone currently selected for an award would otherwise
      // leave the dropdown holding a stale, no-longer-a-participant value —
      // the server correctly rejects that on save, but clearing it here
      // avoids a confusing round-trip just to find that out.
      const idString = String(registrationId);
      if (mvpId === idString) setMvpId('');
      if (bestBatterId === idString) setBestBatterId('');
      if (bestBowlerId === idString) setBestBowlerId('');
    } else {
      next.add(registrationId);
    }
    setParticipantIds(next);
  };

  const save = async () => {
    setBusy(true);
    try {
      const entries = allMembers
        .filter((m) => participantIds.has(m.registration_id))
        .map((m) => ({ registration_id: m.registration_id, team_id: m.team_id }));
      await api(`/admin/fixtures/${fixture.id}/participants`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(entries),
      });

      const payload = {
        result_type: resultType,
        winning_team_id: resultType === 'TEAM_A_WIN' ? rosters.teamA.id : resultType === 'TEAM_B_WIN' ? rosters.teamB.id : null,
        player_of_match_registration_id: mvpId ? +mvpId : null,
        best_batter_registration_id: bestBatterId ? +bestBatterId : null,
        best_bowler_registration_id: bestBowlerId ? +bestBowlerId : null,
        notes: notes || null,
      };
      await api(`/admin/fixtures/${fixture.id}/result`, {
        method: hasExistingResult ? 'PATCH' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
      setHasExistingResult(true);
      toast('Result saved');
      await load();
    } catch (err) { toast(err.message); } finally { setBusy(false); }
  };

  return (
    <div className="form">
      <p className="eyebrow">RESULT</p>
      <div className="share-buttons">
        {RESULT_OPTIONS(fixture).map((opt) => (
          <button key={opt.value} className={opt.value === resultType ? 'active' : ''} onClick={() => setResultType(opt.value)}>
            {opt.label}
          </button>
        ))}
      </div>

      <p className="eyebrow">PARTICIPATION</p>
      <section className="list"><div>
        {allMembers.map((member) => (
          <label className="player-row" key={member.registration_id}>
            <span className="avatar">{member.player_name[0]}</span>
            <span><b>{member.player_name}</b><small>{member.team_name}</small></span>
            <input type="checkbox" checked={participantIds.has(member.registration_id)} onChange={() => toggleParticipant(member.registration_id)} />
          </label>
        ))}
        {allMembers.length === 0 && <p className="quiet">No players assigned to either team yet.</p>}
      </div></section>

      <label className="field"><span>PLAYER OF THE MATCH</span>
        <select value={mvpId} onChange={(e) => setMvpId(e.target.value)}>
          <option value="">None</option>
          {participants.map((p) => <option key={p.registration_id} value={p.registration_id}>{p.player_name}</option>)}
        </select>
      </label>
      <label className="field"><span>BEST BATTER</span>
        <select value={bestBatterId} onChange={(e) => setBestBatterId(e.target.value)}>
          <option value="">None</option>
          {participants.map((p) => <option key={p.registration_id} value={p.registration_id}>{p.player_name}</option>)}
        </select>
      </label>
      <label className="field"><span>BEST BOWLER</span>
        <select value={bestBowlerId} onChange={(e) => setBestBowlerId(e.target.value)}>
          <option value="">None</option>
          {participants.map((p) => <option key={p.registration_id} value={p.registration_id}>{p.player_name}</option>)}
        </select>
      </label>
      <label className="field"><span>NOTES (OPTIONAL)</span><textarea value={notes} onChange={(e) => setNotes(e.target.value)} /></label>
      <button className="primary-button" disabled={busy || !resultType} onClick={save}>
        {busy ? <LoaderCircle className="spin" /> : 'SAVE RESULT'}
      </button>
    </div>
  );
}
