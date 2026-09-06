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
  const method = (fetchOptions.method || 'GET').toUpperCase();
  const headers = new Headers(fetchOptions.headers || {});
  if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method) && !NO_CSRF_PATHS.has(path)) headers.set('X-CSRF-Token', csrfToken);
  const response = await fetch(`/api${path}`, { ...fetchOptions, headers, credentials: 'same-origin' });
  const body = await response.json().catch(() => ({}));
  if (response.status === 401 && method !== 'GET') {
    // A session that was valid when the page loaded expired mid-workflow —
    // send the user to re-authenticate instead of leaving every mutating
    // action silently failing with no way to recover. GET requests are
    // deliberately excluded: several pages intentionally probe "am I logged
    // in yet?" with a GET and handle a 401 themselves (e.g. the initial
    // /player/me or /auth/me check) — redirecting on those would break that
    // existing, correct flow rather than fix anything.
    window.location.assign(authScope === 'player' ? '/' : '/admin/login');
  }
  if (!response.ok) throw new Error(body.error?.message || body.detail || 'Something went wrong. Please try again.');
  return body;
};
