"""The one thing bin/cao-deepseek-probe is for: refusing an API that accepts and never answers.

    python3 -m unittest discover -s tests

Loopback only. A queued DeepSeek request gets its 200 at once and then a blank
keep-alive line every few seconds, so a per-read socket timeout never fires; the probe
passed through the whole queue once (PR#11 review, finding 2). The server here does
exactly that, and the probe has to give up on wall-clock time. The budget is overridden
down to seconds so the suite does not pay the real thirty.
"""
import ast
import http.server
import os
import socketserver
import subprocess
import sys
import threading
import time
import unittest

PROBE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "bin", "cao-deepseek-probe")
WAIT_S = 2


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Handler(http.server.BaseHTTPRequestHandler):
    """A status at once, then either an answer or blank chunks forever, per the path."""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(500 if self.path.endswith("/error") else 200)
        self.send_header("Content-Type", "application/json")
        if self.path.endswith("/answer"):
            body = b'{"choices": []}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                self.wfile.write(b"1\r\n\n\r\n")
                self.wfile.flush()
                time.sleep(0.2)
        except OSError:
            return  # the probe hung up, which is the point

    def log_message(self, format, *args):
        pass


class Deadline(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = _Server(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _probe(self, path, wait=str(WAIT_S), argv=(PROBE,)):
        env = dict(os.environ, DEEPSEEK_API_KEY="test",
                   CAO_DEEPSEEK_URL="http://127.0.0.1:%d%s" % (self.port, path),
                   CAO_DEEPSEEK_WAIT_S=wait)
        start = time.monotonic()
        done = subprocess.run([sys.executable, *argv], env=env, capture_output=True,
                              text=True, timeout=WAIT_S * 5)
        return done, time.monotonic() - start

    def test_a_request_that_is_accepted_and_never_answered_fails_on_the_clock(self):
        done, took = self._probe("/queue")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("did not answer it in %ds" % WAIT_S, done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertLess(took, WAIT_S + 2)

    def test_an_error_whose_body_never_arrives_is_still_reported_as_that_error(self):
        """The error-body read is a socket read too, inside an except clause, where the
        deadline is not caught by that clause's siblings -- and the status it already
        has is the diagnostic, not the queueing message."""
        done, took = self._probe("/error")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("HTTP 500", done.stderr)
        self.assertNotIn("queueing", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertLess(took, WAIT_S + 2)

    def test_a_connect_that_never_completes_is_not_called_queueing(self):
        """The deadline in the connect phase, where no request has been sent. The probe
        is run under a socket.create_connection that sleeps past the budget."""
        stall = ("import runpy, socket, sys, time\n"
                 "socket.create_connection = lambda *a, **k: time.sleep(%d)\n"
                 "runpy.run_path(sys.argv[1], run_name='__main__')" % (WAIT_S * 4))
        done, took = self._probe("/answer", argv=("-c", stall, PROBE))
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("no response in %ds" % WAIT_S, done.stderr)
        self.assertNotIn("queueing", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertLess(took, WAIT_S + 2)

    def test_an_answer_passes_and_disarms_the_alarm(self):
        done, _ = self._probe("/answer")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("answered in", done.stdout)

    def test_a_bad_budget_is_refused_in_one_line(self):
        """int() at module level was a traceback, and 0 disarmed the alarm. "²" passes
        isdigit() and fails int(); 99999999999 overflows signal.alarm's C int."""
        for bad in ("", "abc", "1.5", "0", "-5", "²", "99999999999"):
            with self.subTest(bad=bad):
                done, _ = self._probe("/answer", wait=bad)
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertIn("CAO_DEEPSEEK_WAIT_S", done.stderr)
                self.assertNotIn("Traceback", done.stderr)


class DeadlineClass(unittest.TestCase):

    def test_the_deadline_is_not_an_os_error(self):
        """socket.create_connection catches OSError around each address it tries and
        moves on, which would eat the alarm's exception mid-connect and spend the
        one-shot alarm. Loopback never blocks in connect, so no run-time test sees
        this; the class's bases are read instead. `_Deadline(TimeoutError)` would pass
        every other test here and bring the bug back."""
        with open(PROBE) as src:
            tree = ast.parse(src.read())
        classes = [n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == "_Deadline"]
        self.assertEqual(len(classes), 1)
        self.assertEqual([getattr(b, "id", None) for b in classes[0].bases], ["Exception"])


if __name__ == "__main__":
    unittest.main()
