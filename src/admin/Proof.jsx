import { AlertTriangle, Check, LoaderCircle, X } from 'lucide-react';

export default function Proof({ player, close, confirm, reject, busy }) {
  const payment = player.payment;
  const duplicates = payment.duplicate_of || [];
  return <div className="overlay"><section className="sheet">
    <button className="close" onClick={close}><X /></button>
    <p className="eyebrow">PAYMENT REVIEW</p>
    <h2>{player.name}</h2>
    <strong className="amount">₹{payment.amount_due}</strong>
    {payment.utr_reference && <p className="quiet">UTR / reference: {payment.utr_reference}</p>}
    <img src={payment.screenshot_url} alt="Payment proof" />
    <p className="quiet">Submitted {new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(payment.submitted_at))}{payment.proof_count > 1 && ` · proof ${payment.proof_count} for this payment`}</p>
    {duplicates.length > 0 && <p className="quiet duplicate-warning"><AlertTriangle size={16} /> Matches a screenshot already submitted for {duplicates.map((d) => d.player_name).join(', ')} — not automatically rejected, just worth a look.</p>}
    <div className="review-actions">
      <button className="ghost-button" onClick={reject} disabled={busy}>REJECT</button>
      <button className="primary-button" onClick={confirm} disabled={busy}>{busy ? <LoaderCircle className="spin" /> : 'CONFIRM PAYMENT'}<Check /></button>
    </div>
  </section></div>;
}
