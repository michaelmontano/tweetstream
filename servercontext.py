import threading
import contextlib
import time
import os
import socket
import random
from functools import partial
from inspect import isclass
from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
from SimpleHTTPServer import SimpleHTTPRequestHandler
from SocketServer import BaseRequestHandler


class ServerError(Exception):
    pass


class ServerContext(object):
    """Context object with information about a running test server."""

    def __init__(self, address, port):
        self.address = address or "localhost"
        self.port = port

    @property
    def baseurl(self):
        return "http://%s:%s" % (self.address, self.port)

    def __str__(self):
        return "<ServerContext %s >" % self.baseurl

    __repr__ = __str__


class _SilentSimpleHTTPRequestHandler(SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        self.logging = kwargs.get("logging", False)
        SimpleHTTPRequestHandler.__init__(self, *args, **kwargs)

    def log_message(self, *args, **kwargs):
        if self.logging:
            SimpleHTTPRequestHandler.log_message(self, *args, **kwargs)


class _TestHandler(BaseHTTPRequestHandler):
    """RequestHandler class that handles requests that use a custom handler
    callable."""

    def __init__(self, handler, methods, *args, **kwargs):
        self._handler = handler
        self._methods = methods
        self._response_sent = False
        self._headers_sent = False
        self.logging = kwargs.get("logging", False)
        BaseHTTPRequestHandler.__init__(self, *args, **kwargs)

    def log_message(self, *args, **kwargs):
        if self.logging:
            BaseHTTPRequestHandler.log_message(self, *args, **kwargs)

    def send_response(self, *args, **kwargs):
        self._response_sent = True
        BaseHTTPRequestHandler.send_response(self, *args, **kwargs)

    def end_headers(self, *args, **kwargs):
        self._headers_sent = True
        BaseHTTPRequestHandler.end_headers(self, *args, **kwargs)

    def _do_whatever(self):
        """Called in place of do_METHOD"""
        data = self._handler(self)

        if hasattr(data, "next"):
            # assume it's something supporting generator protocol
            self._handle_with_iterator(data)
        else:
            # Nothing more to do then.
            pass


    def __getattr__(self, name):
        if name.startswith("do_") and name[3:].lower() in self._methods:
            return self._do_whatever
        else:
            # fixme instance or class?
            raise AttributeError(name)

    def _handle_with_iterator(self, iterator):
        self.connection.settimeout(0.1)
        for data in iterator:
            if not self.server.server_thread.running:
                return

            if not self._response_sent:
                self.send_response(200)
            if not self._headers_sent:
                self.end_headers()

            self.wfile.write(data)
            # flush immediatly. We may want to do trickling writes
            # or something else tha trequires bypassing normal caching
            self.wfile.flush()

class _TestServerThread(threading.Thread):
    """Thread class for a running test server"""

    def __init__(self, handler, methods, cwd, port, address):
        threading.Thread.__init__(self)
        self.startup_finished = threading.Event()
        self._methods = methods
        self._cwd = cwd
        self._orig_cwd = None
        self._handler = self._wrap_handler(handler, methods)
        self._setup()
        self.running = True
        self.serverloc = (address, port)
        self.error = None

    def _wrap_handler(self, handler, methods):
        if isclass(handler) and issubclass(handler, BaseRequestHandler):
            return handler # It's OK. user passed in a proper handler
        elif callable(handler):
            return partial(_TestHandler, handler, methods)
            # it's a callable, so wrap in a req handler
        else:
            raise ServerError("handler must be callable or RequestHandler")

    def _setup(self):
        if self._cwd != "./":
            self._orig_cwd = os.getcwd()
            os.chdir(self._cwd)

    def _init_server(self):
        """Hooks up the server socket"""
        try:
            if self.serverloc[1] == "random":
                retries = 10 # try getting an available port max this many times
                while True:
                    try:
                        self.serverloc = (self.serverloc[0],
                                          random.randint(1025, 49151))
                        self._server = HTTPServer(self.serverloc, self._handler)
                    except socket.error:
                        retries -= 1
                        if not retries: # not able to get a port.
                            raise
                    else:
                        break
            else: # use specific port. this might throw, that's expected
                self._server = HTTPServer(self.serverloc, self._handler)
        except socket.error, e:
            self.running = False
            self.error = e
            # set this here, since we'll never enter the serve loop where
            # it is usually set:
            self.startup_finished.set()
            return

        self._server.allow_reuse_address = True # lots of tests, same port
        self._server.timeout = 0.1
        self._server.server_thread = self


    def run(self):
        self._init_server()

        while self.running:
            self._server.handle_request() # blocks for self.timeout secs
            # First time this falls through, signal the parent thread that
            # the server is ready for incomming connections
            if not self.startup_finished.is_set():
                self.startup_finished.set()

        self._cleanup()

    def stop(self):
        """Stop the server and attempt to make the thread terminate.
        This happens async but the calling code can check periodically
        the isRunning flag on the thread object.
        """
        # actual stopping happens in the run method
        self.running = False

    def _cleanup(self):
        """Do some rudimentary cleanup."""
        if self._orig_cwd:
            os.chdir(self._orig_cwd)


@contextlib.contextmanager
def test_server(handler=_SilentSimpleHTTPRequestHandler, port=8514,
                address="", methods=("get", "head"), cwd="./"):
    """Context that makes available a web server in a separate thread"""
    thread = _TestServerThread(handler=handler, methods=methods, cwd=cwd,
                               port=port, address=address)
    thread.start()

    # fixme: should this be daemonized? If it isn't it will block the entire
    # app, but that should never happen anyway..
    thread.startup_finished.wait()

    if thread.error: # startup failed! Bail, throw whatever the server did
        raise thread.error

    exc = None
    try:
        yield ServerContext(*thread.serverloc)
    except Exception, exc:
        pass
    thread.stop()
    thread.join(5) # giving it a lot of leeway. should never happen

    if exc:
        raise exc

    # fixme: this takes second priorty after the internal exception but would
    # still be nice to signal back to calling code.

    if thread.isAlive():
        raise Warning("Test server could not be stopped")
