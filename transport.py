import http.server
import json
import queue
import secrets
import threading
import urllib.parse
from .companion_access import AccessGate, AccessError

PORT = 49186
MAX_BODY = 32 * 1024 * 1024
pending = queue.Queue(maxsize=32)
server = None
token = None
access = None


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, data):
        encoded = json.dumps(data, allow_nan=False, separators=(',', ':')).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path != '/hello' or self.headers.get('Origin'):
            return self.reply(404, {'error': 'unknown request'})
        return self.reply(200, {'app': 'Eclipse Blender', 'protocol': 1, 'revision': 8, 'authorization': 1, 'token': token})

    def do_POST(self):
        if not token or self.headers.get('Origin') or not secrets.compare_digest(self.headers.get('X-Eclipse-Token', ''), token):
            return self.reply(403, {'error': 'connection expired'})
        if self.path not in {'/authorize', '/connect', '/poll', '/pair', '/unpair', '/mode', '/bake', '/take', '/disconnect', '/bones', '/refresh'}:
            return self.reply(404, {'error': 'unknown request'})
        if self.path != '/authorize' and (not access or not access.allowed(self.headers.get('X-Eclipse-Access', ''))):
            return self.reply(403, {'error': 'Update and activate the official Eclipse plugin, then reconnect.'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= MAX_BODY:
                raise ValueError('request is too large or empty')
            if self.path == '/authorize' and size > 2048:
                raise ValueError('authorization request too large')
            self.connection.settimeout(6)
            data = json.loads(self.rfile.read(size), parse_constant=lambda s: (_ for _ in ()).throw(ValueError('invalid number')))
            if not isinstance(data, dict):
                raise ValueError('invalid request')
            if self.path == '/authorize':
                try:
                    return self.reply(200, access.authorize(data))
                except AccessError as error:
                    return self.reply(403, {'error': str(error)})
            event, result = threading.Event(), {}
            pending.put_nowait((self.path, data, event, result))
            if not event.wait(12):
                result['cancelled'] = True
                return self.reply(503, {'error': 'Blender is busy; try again'})
            return self.reply(400 if 'error' in result else 200, result)
        except (ValueError, queue.Full, TimeoutError) as error:
            return self.reply(400, {'error': str(error)})


def start():
    global server, token, access
    if server:
        return
    token = secrets.token_urlsafe(32)
    access = AccessGate()
    server = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name='Eclipse Blender HTTP', daemon=True).start()


def stop():
    global server, token, access
    if server:
        server.shutdown()
        server.server_close()
    server = token = None
    if access:
        access.clear()
    access = None
    while not pending.empty():
        try:
            _, _, event, result = pending.get_nowait()
            result['error'] = 'disconnected'
            event.set()
        except queue.Empty:
            break
