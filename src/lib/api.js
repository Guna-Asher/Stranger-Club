let csrfToken = '';

export const setCsrfToken = (token) => { csrfToken = token; };

export const api = async (path, options = {}) => { const headers = new Headers(options.headers || {}); if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase()) && path !== '/auth/login') headers.set('X-CSRF-Token', csrfToken); const response = await fetch(`/api${path}`, { ...options, headers, credentials: 'same-origin' }); const body = await response.json().catch(() => ({})); if (!response.ok) throw new Error(body.error?.message || body.detail || 'Something went wrong. Please try again.'); return body; };
