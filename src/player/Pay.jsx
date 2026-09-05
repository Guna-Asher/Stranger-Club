import { useEffect, useRef, useState } from 'react';
import { Check, ChevronRight, Copy, ImagePlus, LoaderCircle } from 'lucide-react';
import { QRCodeSVG } from 'qrcode.react';
import FlowHeader from '../components/FlowHeader';
import FormError from '../components/FormError';
import { api } from '../lib/api';

export default function Pay({ match, registration, back, done, toast }) {
  const [file, setFile] = useState(); const [preview, setPreview] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const input = useRef(); const uri = `upi://pay?pa=${encodeURIComponent(match.upi_id)}&pn=Stranger%20Club&am=${match.fee}&cu=INR`;
  useEffect(() => { if (!file) return; const link = URL.createObjectURL(file); setPreview(link); return () => URL.revokeObjectURL(link); }, [file]);
  const upload = async () => { if (!file) return; setBusy(true); const body = new FormData(); body.append('screenshot', file); try { done(await api(`/registrations/${registration.public_id}/payment`, { method: 'POST', body, authScope: 'player' })); } catch (err) { setError(err.message); } finally { setBusy(false); } };
  return <section className="flow"><FlowHeader step="02" label="PAYMENT" back={back} /><div className="pay-total"><small>MATCH FEE</small><b>₹{match.fee}</b><span>Pay with any UPI app</span></div><div className="qr-panel"><p>SCAN TO PAY</p><div className="qr"><QRCodeSVG value={uri} size={166} includeMargin /></div><button className="upi" onClick={async () => { await navigator.clipboard?.writeText(match.upi_id); toast('UPI ID copied'); }}><span><small>UPI ID</small>{match.upi_id}</span><Copy size={18} /></button></div><div className="proof"><p className="eyebrow">PAYMENT PROOF</p><h2>UPLOAD YOUR SCREENSHOT</h2><p>We’ll review it shortly and confirm your spot.</p>{preview ? <div className="preview"><img src={preview} alt="Payment proof selected" /><button onClick={() => input.current.click()}>CHANGE</button></div> : <button className="upload" onClick={() => input.current.click()}><ImagePlus /><b>UPLOAD SCREENSHOT</b><span>JPEG, PNG or WEBP · max 5 MB</span></button>}<input ref={input} className="hidden" type="file" accept="image/jpeg,image/png,image/webp" onChange={(e) => setFile(e.target.files?.[0])} />{file && <p className="ready"><Check /> Screenshot selected</p>}<FormError>{error}</FormError></div><button className="primary-button pay-submit" disabled={!file || busy} onClick={upload}>{busy ? <LoaderCircle className="spin" /> : 'SUBMIT PAYMENT PROOF'}<ChevronRight /></button></section>;
}
