// WebAuthn (passkey) client. Talks to /api/auth/* and uses navigator.credentials.

function b64urlEncode(buf) {
  const bytes = new Uint8Array(buf);
  let bin = '';
  for (let i = 0; i < bytes.byteLength; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function b64urlDecode(str) {
  const pad = '='.repeat((4 - (str.length % 4)) % 4);
  const base64 = (str + pad).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const buf = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) buf[i] = raw.charCodeAt(i);
  return buf.buffer;
}

function decodeCreationOptions(opts) {
  return {
    ...opts,
    challenge: b64urlDecode(opts.challenge),
    user: { ...opts.user, id: b64urlDecode(opts.user.id) },
    excludeCredentials: (opts.excludeCredentials || []).map((c) => ({
      ...c,
      id: b64urlDecode(c.id),
    })),
  };
}

function decodeRequestOptions(opts) {
  return {
    ...opts,
    challenge: b64urlDecode(opts.challenge),
    allowCredentials: (opts.allowCredentials || []).map((c) => ({
      ...c,
      id: b64urlDecode(c.id),
    })),
  };
}

function encodeRegistrationCredential(cred) {
  return {
    id: cred.id,
    rawId: b64urlEncode(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: b64urlEncode(cred.response.clientDataJSON),
      attestationObject: b64urlEncode(cred.response.attestationObject),
      transports: cred.response.getTransports ? cred.response.getTransports() : [],
    },
    clientExtensionResults: cred.getClientExtensionResults?.() || {},
    authenticatorAttachment: cred.authenticatorAttachment || null,
  };
}

function encodeAuthenticationCredential(cred) {
  return {
    id: cred.id,
    rawId: b64urlEncode(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: b64urlEncode(cred.response.clientDataJSON),
      authenticatorData: b64urlEncode(cred.response.authenticatorData),
      signature: b64urlEncode(cred.response.signature),
      userHandle: cred.response.userHandle
        ? b64urlEncode(cred.response.userHandle)
        : null,
    },
    clientExtensionResults: cred.getClientExtensionResults?.() || {},
    authenticatorAttachment: cred.authenticatorAttachment || null,
  };
}

export function isWebAuthnSupported() {
  return (
    typeof window !== 'undefined' &&
    window.PublicKeyCredential != null &&
    typeof navigator.credentials?.create === 'function'
  );
}

export async function status() {
  const r = await fetch('/api/auth/status', { credentials: 'same-origin' });
  if (!r.ok) throw new Error(`status http ${r.status}`);
  return r.json();
}

export async function logout() {
  await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
}

export async function register(nickname) {
  if (!isWebAuthnSupported()) {
    throw new Error('Passkeys not supported on this browser/device.');
  }

  const beginRes = await fetch('/api/auth/register/begin', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ nickname: nickname || 'Unnamed device' }),
    credentials: 'same-origin',
  });
  if (!beginRes.ok) throw new Error(await readError(beginRes, 'register/begin'));
  const { challenge_id, options } = await beginRes.json();

  let credential;
  try {
    credential = await navigator.credentials.create({
      publicKey: decodeCreationOptions(options),
    });
  } catch (err) {
    if (err.name === 'NotAllowedError') {
      throw new Error('Registration cancelled.');
    }
    throw err;
  }
  if (!credential) throw new Error('Browser returned no credential.');

  const completeRes = await fetch('/api/auth/register/complete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      challenge_id,
      credential: encodeRegistrationCredential(credential),
    }),
    credentials: 'same-origin',
  });
  if (!completeRes.ok) throw new Error(await readError(completeRes, 'register/complete'));
  return completeRes.json();
}

export async function login() {
  if (!isWebAuthnSupported()) {
    throw new Error('Passkeys not supported on this browser/device.');
  }

  const beginRes = await fetch('/api/auth/login/begin', {
    method: 'POST',
    credentials: 'same-origin',
  });
  if (!beginRes.ok) throw new Error(await readError(beginRes, 'login/begin'));
  const { challenge_id, options } = await beginRes.json();

  let credential;
  try {
    credential = await navigator.credentials.get({
      publicKey: decodeRequestOptions(options),
    });
  } catch (err) {
    if (err.name === 'NotAllowedError') {
      throw new Error('Sign-in cancelled.');
    }
    throw err;
  }
  if (!credential) throw new Error('Browser returned no credential.');

  const completeRes = await fetch('/api/auth/login/complete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      challenge_id,
      credential: encodeAuthenticationCredential(credential),
    }),
    credentials: 'same-origin',
  });
  if (!completeRes.ok) throw new Error(await readError(completeRes, 'login/complete'));
  return completeRes.json();
}

export async function listPasskeys() {
  const r = await fetch('/api/auth/passkeys', { credentials: 'same-origin' });
  if (!r.ok) throw new Error(await readError(r, 'passkeys list'));
  return r.json();
}

export async function revokePasskey(credentialId) {
  const r = await fetch(`/api/auth/passkeys/${encodeURIComponent(credentialId)}`, {
    method: 'DELETE',
    credentials: 'same-origin',
  });
  if (!r.ok) throw new Error(await readError(r, 'passkey revoke'));
  return r.json();
}

async function readError(res, label) {
  try {
    const body = await res.json();
    return `${label}: ${body.detail || res.status}`;
  } catch {
    return `${label}: HTTP ${res.status}`;
  }
}
