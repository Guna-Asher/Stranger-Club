import { useEffect, useState } from 'react';
import Toast from '../components/Toast';
import Loading from '../components/Loading';
import ErrorPage from '../components/ErrorPage';
import Match from './Match';
import Register from './Register';
import Pay from './Pay';
import PlayerStatus from './PlayerStatus';
import { api } from '../lib/api';

export default function PlayerApp({ publicId }) {
  const [match, setMatch] = useState(); const [registration, setRegistration] = useState(); const [page, setPage] = useState('match'); const [error, setError] = useState(''); const [toast, setToast] = useState(''); const key = `sc-registration-${publicId}`;
  const load = async () => { try { setError(''); const item = await api(`/events/${publicId}`); setMatch(item); const registrationId = localStorage.getItem(key); if (registrationId) { try { const player = await api(`/registrations/${registrationId}`); setRegistration(player); if (player.status !== 'PENDING' || player.payment) setPage('status'); } catch { localStorage.removeItem(key); } } } catch (err) { setError(err.message); } };
  useEffect(() => { load(); }, [publicId]);
  useEffect(() => { const stream = new EventSource(`/api/events/${publicId}/stream`); stream.onmessage = () => load(); stream.addEventListener('summary', () => load()); return () => stream.close(); }, [publicId]);
  const save = (item) => { setRegistration(item); localStorage.setItem(key, item.public_id); };
  if (error) return <ErrorPage message={error} />;
  if (!match) return <Loading />;
  return <main className="app"><Toast text={toast} clear={() => setToast('')} />
    {page === 'match' && <Match match={match} join={() => setPage('form')} />}
    {page === 'form' && <Register match={match} back={() => setPage('match')} done={(item) => { save(item); setPage(item.status === 'WAITLISTED' ? 'status' : 'pay'); }} />}
    {page === 'pay' && <Pay match={match} registration={registration} back={() => setPage('form')} done={(item) => { save(item); setPage('status'); }} toast={setToast} />}
    {page === 'status' && <PlayerStatus match={match} registration={registration} back={() => setPage('match')} retry={() => setPage('pay')} refresh={async () => { save(await api(`/registrations/${registration.public_id}`)); setToast('Status updated'); }} toast={setToast} />}
  </main>;
}
