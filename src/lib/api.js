let organizerCsrfToken = '';
let playerCsrfToken = '';

export const setCsrfToken = (token, scope = 'organizer') => {
  if (scope === 'player') playerCsrfToken = token;
  else organizerCsrfToken = token;
};

const NO_CSRF_PATHS = new Set(['/auth/login', '/player/otp/request', '/player/otp/verify']);

export const api = async (path, options = {}) => {
  const { authScope = 'organizer', ...fetchOptions } = options;
  const csrfToken = authScope === 'player' ? playerCsrfToken : organizerCsrfToken;
  const headers = new Headers(fetchOptions.headers || {});
  if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((fetchOptions.method || 'GET').toUpperCase()) && !NO_CSRF_PATHS.has(path)) headers.set('X-CSRF-Token', csrfToken);
  const response = await fetch(`/api${path}`, { ...fetchOptions, headers, credentials: 'same-origin' });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error?.message || body.detail || 'Something went wrong. Please try again.');
  return body;
};
