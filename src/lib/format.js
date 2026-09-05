export const cn = (...names) => names.filter(Boolean).join(' ');
export const dateText = (value) => new Intl.DateTimeFormat('en-IN', { weekday: 'short', day: 'numeric', month: 'short' }).format(new Date(`${value}T12:00:00`));
export const timeText = (value) => new Intl.DateTimeFormat('en-IN', { hour: 'numeric', minute: '2-digit' }).format(new Date(`2000-01-01T${value}`));
export function playerStatus(item) { if (item?.payment?.status === 'SUBMITTED') return 'PAYMENT_SUBMITTED'; return item?.status; }
