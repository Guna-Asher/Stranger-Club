import { cn } from '../lib/format';

export default function Status({ status }) { const text = { PENDING: 'PAYMENT DUE', PAYMENT_SUBMITTED: 'IN REVIEW', CONFIRMED: 'CONFIRMED', REJECTED: 'ACTION NEEDED', WAITLISTED: 'WAITLISTED' }[status] || status; return <span className={cn('status', `status--${status?.toLowerCase()}`)}>{text}</span>; }
