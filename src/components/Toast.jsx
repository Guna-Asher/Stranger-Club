import { useEffect } from 'react';
import { CheckCircle2 } from 'lucide-react';

export default function Toast({ text, clear }) { useEffect(() => { if (text) { const timer = setTimeout(clear, 3000); return () => clearTimeout(timer); } }, [text, clear]); return text ? <div className="toast"><CheckCircle2 size={16} />{text}</div> : null; }
