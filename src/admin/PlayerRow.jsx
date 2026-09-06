import { ArrowUpRight } from 'lucide-react';
import Avatar from '../components/Avatar';
import Status from '../components/Status';
import { playerStatus } from '../lib/format';

export default function PlayerRow({ item, open, action, act }) { return <div className="player-row"><Avatar designId={item.avatar_design_id} name={item.name} /><span><b>{item.name}</b><small>{item.phone} · {item.preferred_position.replaceAll('_', ' ')}</small></span><Status status={playerStatus(item)} />{action && <button onClick={act}>{action}</button>}{open && <button onClick={open}><ArrowUpRight size={17} /></button>}</div>; }
