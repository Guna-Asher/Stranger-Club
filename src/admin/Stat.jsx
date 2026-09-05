import { cn } from '../lib/format';

export default function Stat({ text, value, color }) { return <div className={cn('stat', color)}><small>{text}</small><b>{value}</b></div>; }
