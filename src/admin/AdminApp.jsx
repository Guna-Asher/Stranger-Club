import { useEffect, useState } from 'react';
import { ArrowUpRight } from 'lucide-react';
import Brand from '../components/Brand';
import Toast from '../components/Toast';
import Loading from '../components/Loading';
import ErrorPage from '../components/ErrorPage';
import Dashboard from './Dashboard';
import Create from './Create';
import Manage from './Manage';
import { api, setCsrfToken } from '../lib/api';

export default function AdminApp() {
  const [matches, setMatches] = useState(); const [view, setView] = useState('dashboard'); const [selected, setSelected] = useState(); const [error, setError] = useState(''); const [toast, setToast] = useState('');
  const load = async () => { try { const auth = await api('/auth/me'); setCsrfToken(auth.csrf_token); setMatches(await api('/admin/events')); } catch (err) { if (err.message === 'Authentication required') window.location.assign('/admin/login'); else setError(err.message); } }; useEffect(() => { load(); }, []);
  const choose = async (id) => { try { setSelected(await api(`/admin/matches/${id}`)); setView('manage'); } catch (err) { setError(err.message); } };
  if (error) return <ErrorPage message={error} />; if (!matches) return <Loading />;
  return <main className="admin"><Toast text={toast} clear={() => setToast('')} /><header className="admin-header"><Brand /><span><a href="/">PUBLIC SITE <ArrowUpRight size={14} /></a><button className="logout" onClick={async () => { await api('/auth/logout', { method: 'POST' }); setCsrfToken(''); window.location.assign('/admin/login'); }}>LOG OUT</button></span></header>{view === 'dashboard' && <Dashboard matches={matches} choose={choose} create={() => setView('create')} />}{view === 'create' && <Create back={() => setView('dashboard')} created={async (item) => { await load(); await choose(item.id); setToast('Match is live and ready to share'); }} />}{view === 'manage' && <Manage match={selected} back={() => setView('dashboard')} refresh={() => choose(selected.id)} toast={setToast} />}</main>;
}
