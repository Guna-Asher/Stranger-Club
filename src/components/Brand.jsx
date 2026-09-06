import { cn } from '../lib/format';

export default function Brand({ light = false }) { return <a className={cn('brand', light && 'brand--light')} href="/"><img className="brand-mark" src="/Logo.png" alt="Stranger Club" />STRANGER CLUB</a>; }
