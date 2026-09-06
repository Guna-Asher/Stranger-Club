import { cn } from '../lib/format';
import { DEFAULT_MATCH } from '../lib/constants';

export default function Brand({ light = false }) { return <a className={cn('brand', light && 'brand--light')} href={`/m/${DEFAULT_MATCH}`}><img className="brand-mark" src="/Logo.png" alt="Stranger Club" />STRANGER CLUB</a>; }
