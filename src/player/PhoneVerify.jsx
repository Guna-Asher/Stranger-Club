import { useState } from 'react';
import { ChevronRight, LoaderCircle, ShieldCheck } from 'lucide-react';
import Field from '../components/Field';
import FlowHeader from '../components/FlowHeader';
import FormError from '../components/FormError';
import { api, setCsrfToken } from '../lib/api';

export default function PhoneVerify({ back, onVerified }) {
  const [step, setStep] = useState('phone');
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const requestCode = async (e) => {
    e.preventDefault(); setBusy(true); setError('');
    try {
      await api('/player/otp/request', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ phone }) });
      setStep('code');
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  };

  const verifyCode = async (e) => {
    e.preventDefault(); setBusy(true); setError('');
    try {
      const auth = await api('/player/otp/verify', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ phone, code }) });
      setCsrfToken(auth.csrf_token, 'player');
      onVerified();
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  };

  if (step === 'code') {
    return <section className="flow"><FlowHeader step="01" label="VERIFY YOUR NUMBER" back={() => setStep('phone')} /><div className="intro"><p className="eyebrow">SUNDAY CRICKET</p><h1>ENTER THE CODE</h1><p>We sent a 6-digit code to {phone}.</p></div><form onSubmit={verifyCode} className="form"><Field label="VERIFICATION CODE" placeholder="6-digit code" type="tel" inputMode="numeric" value={code} set={setCode} autoFocus required /><FormError>{error}</FormError><button className="primary-button" disabled={busy || code.length !== 6}>{busy ? <LoaderCircle className="spin" /> : 'VERIFY'}<ChevronRight /></button></form></section>;
  }
  return <section className="flow"><FlowHeader step="01" label="YOUR DETAILS" back={back} /><div className="intro"><p className="eyebrow">SUNDAY CRICKET</p><h1>WHO’S PLAYING?</h1><p>We’ll text you a code to confirm it’s you.</p></div><form onSubmit={requestCode} className="form"><Field label="MOBILE NUMBER" placeholder="10-digit number" type="tel" inputMode="numeric" value={phone} set={setPhone} autoFocus required /><FormError>{error}</FormError><button className="primary-button" disabled={busy || phone.length < 10}>{busy ? <LoaderCircle className="spin" /> : 'SEND CODE'}<ChevronRight /></button></form><p className="privacy"><ShieldCheck size={16} /> Your details are only used to organise this match.</p></section>;
}
