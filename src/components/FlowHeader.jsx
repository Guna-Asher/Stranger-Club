import { ArrowLeft } from 'lucide-react';
import Brand from './Brand';

export default function FlowHeader({ step, label, back }) { return <header className="flow-header"><button onClick={back}><ArrowLeft /></button><span>{step} / 02 · {label}</span><Brand /></header>; }
