import { useEffect, useRef, useState } from 'react';
import { Check, ChevronRight, Copy, ImagePlus, LoaderCircle } from 'lucide-react';
import { QRCodeSVG } from 'qrcode.react';
import Field from '../components/Field';
import FlowHeader from '../components/FlowHeader';
import FormError from '../components/FormError';
import { api } from '../lib/api';

export default function Pay({ registration, back, done, toast }) {
  const [file, setFile] = useState(); const [preview, setPreview] = useState(''); const [utr, setUtr] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const input = useRef();
  // Everything shown here — amount, payee, QR — comes from the server's own
  // Payment record, never from the (mutable, organizer-editable) event
  // itself: this is what an admin config edit can never silently change out
  // from under a registration already in progress.
  const payment = registration.payment;
  useEffect(() => { if (!file) return; const link = URL.createObjectURL(file); setPreview(link); return () => URL.revokeObjectURL(link); }, [file]);
  const upload = async () => {
    if (!file) return; setBusy(true);
    const body = new FormData(); body.append('screenshot', file); if (utr.trim()) body.append('utr', utr.trim());
    try { done(await api(`/registrations/${registration.public_id}/payment`, { method: 'POST', body, authScope: 'player' })); } catch (err) { setError(err.message); } finally { setBusy(false); }
  };
  return <section className="flow">
    <FlowHeader step="02" label="PAYMENT" back={back} />
    <div className="pay-total"><small>ENTRY FEE</small><b>₹{payment.amount_due}</b><span>Pay with any UPI app</span></div>
    <div className="qr-panel">
      <p>SCAN TO PAY</p>
      <div className="qr">{payment.qr_source_snapshot === 'UPLOADED'
        ? <img src={payment.qr_image_url} alt="Payment QR code" width={166} height={166} />
        : <QRCodeSVG value={payment.upi_uri} size={166} includeMargin />}</div>
      <button className="upi" onClick={async () => { await navigator.clipboard?.writeText(payment.payee_upi_id_snapshot); toast('UPI ID copied'); }}>
        <span><small>UPI ID</small>{payment.payee_upi_id_snapshot}</span><Copy size={18} />
      </button>
      <p className="quiet">Opening your UPI app doesn’t confirm the payment by itself.</p>
    </div>
    <div className="proof">
      <p className="eyebrow">PAYMENT PROOF</p>
      <h2>UPLOAD YOUR SCREENSHOT</h2>
      <p>We’ll review it shortly and confirm your spot. Uploading a screenshot doesn’t confirm payment on its own.</p>
      {preview
        ? <div className="preview"><img src={preview} alt="Payment proof selected" /><button onClick={() => input.current.click()}>CHANGE</button></div>
        : <button className="upload" onClick={() => input.current.click()}><ImagePlus /><b>UPLOAD SCREENSHOT</b><span>JPEG, PNG or WEBP · max 5 MB</span></button>}
      <input ref={input} className="hidden" type="file" accept="image/jpeg,image/png,image/webp" onChange={(e) => setFile(e.target.files?.[0])} />
      {file && <p className="ready"><Check /> Screenshot selected</p>}
      <Field label="UTR / REFERENCE (OPTIONAL)" value={utr} set={setUtr} />
      <FormError>{error}</FormError>
    </div>
    <button className="primary-button pay-submit" disabled={!file || busy} onClick={upload}>{busy ? <LoaderCircle className="spin" /> : 'SUBMIT PAYMENT PROOF'}<ChevronRight /></button>
  </section>;
}
