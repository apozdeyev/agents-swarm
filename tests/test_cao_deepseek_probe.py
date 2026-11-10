"""The one thing bin/cao-deepseek-probe is for: refusing an API that accepts and never answers.

    python3 -m unittest discover -s tests

Loopback only. A queued DeepSeek request gets its 200 at once and then a blank
keep-alive line every few seconds, so a per-read socket timeout never fires; the probe
passed through the whole queue once (PR#11 review, finding 2). The server here does
exactly that, and the probe has to give up on wall-clock time. The budget is overridden
down to seconds so the suite does not pay the real thirty.
"""
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

    def _probe(self, path):
        env = dict(os.environ, DEEPSEEK_API_KEY="test",
                   CAO_DEEPSEEK_URL="http://127.0.0.1:%d%s" % (self.port, path),
                   CAO_DEEPSEEK_WAIT_S=str(WAIT_S))
        start = time.monotonic()
        done = subprocess.run([sys.executable, PROBE], env=env, capture_output=True,
                              text=True, timeout=WAIT_S * 5)
        return done, time.monotonic() - start

    def test_a_request_that_is_accepted_and_never_answered_fails_on_the_clock(self):
        done, took = self._probe("/queue")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("did not answer it in %ds" % WAIT_S, done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertLess(took, WAIT_S + 2)

    def test_an_error_whose_body_never_arrives_still_gets_the_one_line_diagnostic(self):
        """The error-body read is a socket read too, inside an except clause, where the
        deadline is not caught by that clause's siblings."""
        done, took = self._probe("/error")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertTrue(done.stderr.startswith("deepseek: "), done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertLess(took, WAIT_S + 2)

    def test_an_answer_passes_and_disarms_the_alarm(self):
        done, _ = self._probe("/answer")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("answered in", done.stdout)


if __name__ == "__main__":
    unittest.main()
