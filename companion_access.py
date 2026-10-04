import hashlib
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request

VERIFY_URL = 'https://eclipse-keys.riftex.workers.dev/verify'
LIFETIME = 1800


class AccessError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise AccessError('Activation verification redirected unexpectedly.')


def verify_receipt(token, user_id):
    body = json.dumps({'token': token, 'userId': user_id}).encode()
    request = urllib.request.Request(VERIFY_URL, data=body, headers={
        'Content-Type': 'application/json', 'User-Agent': 'Eclipse-Companion-Access/1'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=6) as response:
            if response.status != 200:
                raise AccessError('Eclipse activation could not be verified.')
            payload = response.read(16385)
            if len(payload) > 16384:
                raise AccessError('Invalid activation response.')
            result = json.loads(payload)
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise AccessError('Activate the official Eclipse plugin, then reconnect.')
    except AccessError:
        raise
    except (OSError, ValueError, urllib.error.URLError):
        raise AccessError('Could not verify Eclipse activation. Check your connection and retry.') from None


class AccessGate:
    def __init__(self, verify=verify_receipt, clock=time.monotonic):
        self._verify = verify
        self._clock = clock
        self._lock = threading.Lock()
        self._grants = {}
        self._attempts = []

    def _prune(self, now):
        self._grants = {ticket: row for ticket, row in self._grants.items() if row[0] > now}
        self._attempts = [at for at in self._attempts if now - at < 60]

    def authorize(self, data):
        receipt = data.get('receipt') if isinstance(data, dict) else None
        account = data.get('userId') if isinstance(data, dict) else None
        if not isinstance(receipt, str) or len(receipt) > 1024:
            raise AccessError('Update and activate the official Eclipse plugin, then reconnect.')
        match = re.fullmatch(r'([0-9a-fA-F]{1,512}):([0-9]{1,16}):[0-9]{1,16}', receipt)
        if not match or isinstance(account, bool) or not isinstance(account, int) or not 0 < account < 2**53 or account != int(match[2]):
            raise AccessError('Eclipse activation does not match this Roblox account.')
        fingerprint = hashlib.sha256(receipt.encode()).digest()
        with self._lock:
            now = self._clock()
            self._prune(now)
            for ticket, (expires, known) in self._grants.items():
                if secrets.compare_digest(known, fingerprint) and expires - now > 60:
                    return {'ticket': ticket, 'expiresIn': int(expires - now)}
            if len(self._attempts) >= 6:
                raise AccessError('Too many activation checks. Wait a minute and reconnect.')
            self._attempts.append(now)
            self._verify(match[1], account)
            now = self._clock()
            self._prune(now)
            if len(self._grants) >= 16:
                raise AccessError('Too many active Eclipse connections.')
            ticket = secrets.token_urlsafe(32)
            self._grants[ticket] = (now + LIFETIME, fingerprint)
            return {'ticket': ticket, 'expiresIn': LIFETIME}

    def allowed(self, ticket):
        if not isinstance(ticket, str) or len(ticket) > 128:
            return False
        row = self._grants.get(ticket)
        return row is not None and row[0] > self._clock()

    def clear(self):
        with self._lock:
            self._grants.clear()
            self._attempts.clear()
