import { LoaderCircle, X } from 'lucide-react';

export default function CancelConfirm({ close, confirm, busy }) {
  return <div className="overlay"><section className="sheet"><button className="close" onClick={close}><X /></button><p className="eyebrow">CANCEL REGISTRATION</p><h2>ARE YOU SURE?</h2><p className="quiet">You’ll lose your spot in this match. You can register again later if space allows.</p><div className="review-actions"><button className="ghost-button" onClick={close} disabled={busy}>KEEP MY SPOT</button><button className="primary-button" onClick={confirm} disabled={busy}>{busy ? <LoaderCircle className="spin" /> : 'CANCEL REGISTRATION'}</button></div></section></div>;
}
