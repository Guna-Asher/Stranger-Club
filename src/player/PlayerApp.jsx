import { useEffect, useState } from 'react';
import Toast from '../components/Toast';
import Loading from '../components/Loading';
import ErrorPage from '../components/ErrorPage';
import Match from './Match';
import Register from './Register';
import Pay from './Pay';
import PlayerStatus from './PlayerStatus';
import { api, setCsrfToken } from '../lib/api';

export default function PlayerApp({ publicId }) {
  const [match, setMatch] = useState(); const [registration, setRegistration] = useState(); const [page, setPage] = useState('match'); const [error, setError] = useState(''); const [toast, setToast] = useState(''); const [playerVerified, setPlayerVerified] = useState(false); const key = `sc-registration-${publicId}`;
  const load = async () => {
    try {
      setError(''); const item = await api(`/events/${publicId}`); setMatch(item);
      let verified = false;
      try { const auth = await api('/player/me', { authScope: 'player' }); setCsrfToken(auth.csrf_token, 'player'); verified = true; } catch { verified = false; }
      setPlayerVerified(verified);
      const registrationId = localStorage.getItem(key);
      if (registrationId && verified) {
        try {
          const player = await api(`/registrations/${registrationId}`, { authScope: 'player' }); setRegistration(player);
          // A Payment record always exists from the moment a Registration is
          // created (AWAITING_PROOF) — this replaces the old "payment
          // exists" check, which no longer distinguishes anything.
          if (player.status !== 'PENDING' || player.payment?.status !== 'AWAITING_PROOF') setPage('status');
        } catch { localStorage.removeItem(key); }
      } else if (registrationId) {
        // No valid session to recall this registration under ownership rules; drop the stale reference.
        localStorage.removeItem(key);
      }
    } catch (err) { setError(err.message); }
  };
  useEffect(() => { load(); }, [publicId]);
  useEffect(() => { const stream = new EventSource(`/api/events/${publicId}/stream`); stream.onmessage = () => load(); stream.addEventListener('summary', () => load()); return () => stream.close(); }, [publicId]);
  const save = (item) => { setRegistration(item); localStorage.setItem(key, item.public_id); };
  if (error) return <ErrorPage message={error} />;
  if (!match) return <Loading />;
  return <main className="app"><Toast text={toast} clear={() => setToast('')} />
    {page === 'match' && <Match match={match} join={() => setPage('form')} />}
    {page === 'form' && <Register match={match} back={() => setPage('match')} verified={playerVerified} onVerified={() => setPlayerVerified(true)} done={(item) => { save(item); setPage(item.status === 'WAITLISTED' ? 'status' : 'pay'); }} />}
    {page === 'pay' && <Pay registration={registration} back={() => setPage('form')} done={(item) => { save(item); setPage('status'); }} toast={setToast} />}
    {page === 'status' && <PlayerStatus match={match} registration={registration} back={() => setPage('match')} retry={() => setPage('pay')} rejoin={() => setPage('form')} onCancelled={save} refresh={async () => { save(await api(`/registrations/${registration.public_id}`, { authScope: 'player' })); setToast('Status updated'); }} toast={setToast} />}
  </main>;
}
