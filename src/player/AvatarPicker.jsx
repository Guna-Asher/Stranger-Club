import { useEffect, useState } from 'react';
import { LoaderCircle, Shuffle, X } from 'lucide-react';
import Avatar from '../components/Avatar';
import { api } from '../lib/api';

export default function AvatarPicker({ close, onChanged, toast }) {
  const [options, setOptions] = useState();
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try { setOptions(await api('/player/avatar/options', { authScope: 'player' })); } catch (err) { toast(err.message); }
  };
  useEffect(() => { load(); }, []);

  const generate = async () => {
    setBusy(true);
    try {
      const profile = await api('/player/avatar/generate', { method: 'POST', authScope: 'player' });
      onChanged(profile); toast('New avatar generated'); close();
    } catch (err) { toast(err.message); } finally { setBusy(false); }
  };

  const choose = async (designId) => {
    setBusy(true);
    try {
      const profile = await api('/player/avatar', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ design_id: designId }), authScope: 'player',
      });
      onChanged(profile); toast('Avatar updated'); close();
    } catch (err) { toast(err.message); await load(); } finally { setBusy(false); }
  };

  return (
    <div className="overlay">
      <section className="sheet avatar-sheet">
        <button className="close" onClick={close}><X /></button>
        <p className="eyebrow">YOUR AVATAR</p>
        <h2>CHOOSE OR GENERATE</h2>
        <button className="primary-button" disabled={busy} onClick={generate}>
          {busy ? <LoaderCircle className="spin" /> : <><Shuffle size={16} /> GENERATE NEW</>}
        </button>
        {!options && <p className="quiet">Loading avatars…</p>}
        {options && (
          <div className="avatar-grid">
            {Array.from({ length: options.catalog_size }, (_, id) => id).map((id) => {
              const isCurrent = options.current === id;
              const isTaken = options.taken.includes(id) && !isCurrent;
              return (
                <button
                  key={id} className={isCurrent ? 'avatar-swatch current' : 'avatar-swatch'}
                  disabled={isTaken || busy} onClick={() => choose(id)}
                  title={isTaken ? 'Already taken' : `Avatar ${id}`}
                >
                  <Avatar designId={id} size={40} />
                </button>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
