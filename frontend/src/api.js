// Shared page API client. The browser session lives in an HttpOnly cookie, so
// this module never stores credentials: only the selected domain id is kept in
// sessionStorage (§11) and sent as `x-domain-id`, which the server validates as
// a selector on every request (§7.1).
const DOMAIN_KEY = 'agent-base-domain';

export function currentDomain() {
  try {
    return sessionStorage.getItem(DOMAIN_KEY) || '';
  } catch {
    return '';
  }
}

export function selectDomain(id) {
  try {
    if (id) sessionStorage.setItem(DOMAIN_KEY, id);
    else sessionStorage.removeItem(DOMAIN_KEY);
  } catch {
    // Storage can be unavailable in hardened browsers; requests then fall back
    // to the server-side home domain, which is still validated.
  }
}

export function domainHeaders(extra = {}) {
  const domain = currentDomain();
  return {...(domain ? {'x-domain-id': domain} : {}), ...extra};
}

export async function api(path, {token = '', method = 'GET', body, signal, headers = {}} = {}) {
  const response = await fetch('/api/' + path, {
    method, signal, credentials: 'same-origin', cache: 'no-store',
    headers: {'Content-Type': 'application/json', ...domainHeaders(), ...headers,
      ...(token ? {Authorization: 'Bearer ' + token} : {})},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let payload;
    try {
      payload = await response.json();
    } catch {
      payload = undefined;
    }
    const detail = payload?.detail;
    const error = new Error(typeof detail === 'string' ? detail
      : detail === undefined ? '服务暂时不可用' : JSON.stringify(detail));
    error.status = response.status;
    error.code = typeof payload?.code === 'string' ? payload.code : '';
    // §6.4: a lost session always clears local state and returns to login. The
    // identity probe and the auth endpoints handle their own 401s, otherwise a
    // stale probe would restart the redirect loop.
    if (response.status === 401 && path !== 'v1/me' && !path.startsWith('auth/')) {
      dispatchEvent(new CustomEvent('app:auth-required', {detail: {status: 401, code: error.code}}));
    }
    // §7.2: a rejected domain selector is cleared and re-resolved from /me.
    if (error.code === 'domain_forbidden' || error.code === 'domain_required') {
      dispatchEvent(new CustomEvent('app:domain-invalid',
        {detail: {status: response.status, code: error.code, detail: error.message}}));
    }
    throw error;
  }
  return response.json();
}
