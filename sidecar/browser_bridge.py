"""App-owned loopback bridge, running inside the same process as native IPC."""
import atexit
import os
import threading
import uuid

_state = {"ready": False, "code": "stopped", "message": "Browser engine has not started."}
_server = None
_session = uuid.uuid4().hex


def status():
    return {**_state, "session": _session}


def start(port=None):
    global _server, _state
    if _server is not None:
        return status()
    from http_server import HOST, PanelLensRequestHandler, ThreadingHTTPServer
    try:
        _server = ThreadingHTTPServer((HOST, int(os.environ.get("PANELLENS_HTTP_PORT", "8765")) if port is None else port), PanelLensRequestHandler)
    except OSError:
        _state = {"ready": False, "code": "bridge_port_in_use", "message": "Browser connection unavailable. Close any separately started PanelLens engine, then restart the app."}
        return status()
    threading.Thread(target=_server.serve_forever, name="browser-bridge", daemon=True).start()
    _state = {"ready": True, "code": "ready", "message": "Browser engine is running. Connect the extension with your connection key.", "port": _server.server_address[1]}
    return status()


def stop():
    global _server, _state
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None
    _state = {"ready": False, "code": "stopped", "message": "Browser engine stopped."}


atexit.register(stop)
