"""Offline checks of per-URL retries in locked artifact downloads."""
import hashlib
import http.client
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from h7607a import artifacts
from h7607a.artifacts import ArtifactError, fetch

DATA = b"locked artifact\n" * 64
A = "https://a.example/x.pkg.tar.xz"
B = "https://b.example/x.pkg.tar.xz"


class Response:
    def __init__(self, data, url):
        self.data, self.url, self.reads = data, url, []

    def read(self, size):
        self.reads.append(size)
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Server:
    """Replays one scripted outcome per request: bytes, (bytes, final URL) or an exception."""

    def __init__(self, script):
        self.script = {url: list(outcomes) for url, outcomes in script.items()}
        self.calls, self.responses = [], []

    def urlopen(self, request, timeout):
        url = request.full_url
        self.calls.append(url)
        outcome = self.script[url].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        data, final = outcome if isinstance(outcome, tuple) else (outcome, url)
        self.responses.append(Response(data, final))
        return self.responses[-1]


def http_error(url, code):
    return urllib.error.HTTPError(url, code, "scripted", {}, None)


class RetryTest(unittest.TestCase):
    def fetch(self, script, urls=(A, B)):
        server = Server(script)
        row = {"path": "cache/x.pkg.tar.xz", "bytes": len(DATA),
               "sha256": hashlib.sha256(DATA).hexdigest(), "urls": list(urls)}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(artifacts.urllib.request, "urlopen", server.urlopen), \
                mock.patch.object(artifacts.time, "sleep") as sleep:
            root = Path(tmp)
            try:
                result = fetch(root, row).read_bytes()
            except ArtifactError as exc:
                result = exc
            leftovers = sorted(p.name for p in (root / "cache").iterdir() if p.name != "x.pkg.tar.xz")
        self.assertEqual(leftovers, [])
        return result, server, [c.args[0] for c in sleep.call_args_list]

    def test_connection_error_retries_same_url(self):
        result, server, sleeps = self.fetch({A: [urllib.error.URLError("reset"), DATA]})
        self.assertEqual(result, DATA)
        self.assertEqual(server.calls, [A, A])
        self.assertEqual(sleeps, [1.0])

    def test_three_server_errors_fall_back_to_next_url(self):
        result, server, sleeps = self.fetch({A: [http_error(A, 503)] * 3, B: [DATA]})
        self.assertEqual(result, DATA)
        self.assertEqual(server.calls, [A, A, A, B])
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_not_found_skips_retries(self):
        result, server, sleeps = self.fetch({A: [http_error(A, 404)], B: [DATA]})
        self.assertEqual(result, DATA)
        self.assertEqual(server.calls, [A, B])
        self.assertEqual(sleeps, [])

    def test_rate_limit_is_retried(self):
        result, server, _ = self.fetch({A: [http_error(A, 429), DATA]})
        self.assertEqual(result, DATA)
        self.assertEqual(server.calls, [A, A])

    def test_truncated_and_incomplete_bodies_are_retried(self):
        script = {A: [DATA[:-1], http.client.IncompleteRead(DATA[:5]), DATA]}
        result, server, sleeps = self.fetch(script)
        self.assertEqual(result, DATA)
        self.assertEqual(server.calls, [A, A, A])
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_oversized_body_is_capped_and_not_retried(self):
        result, server, _ = self.fetch({A: [DATA + b"extra"], B: [DATA]})
        self.assertEqual(result, DATA)
        self.assertEqual(server.calls, [A, B])
        self.assertLessEqual(max(server.responses[0].reads), len(DATA) + 1)

    def test_digest_mismatch_moves_to_next_url(self):
        result, server, _ = self.fetch({A: [bytes(len(DATA))], B: [DATA]})
        self.assertEqual(result, DATA)
        self.assertEqual(server.calls, [A, B])

    def test_insecure_redirect_is_refused(self):
        result, server, _ = self.fetch({A: [(DATA, "http://a.example/x.pkg.tar.xz")]}, urls=(A,))
        self.assertIsInstance(result, ArtifactError)
        self.assertEqual(server.calls, [A])

    def test_exhausted_urls_report_every_source(self):
        script = {A: [urllib.error.URLError("down")] * 3, B: [http_error(B, 404)]}
        result, server, sleeps = self.fetch(script)
        self.assertIsInstance(result, ArtifactError)
        self.assertIn(A, str(result))
        self.assertIn(B, str(result))
        self.assertEqual(server.calls, [A, A, A, B])
        self.assertEqual(sleeps, [1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
