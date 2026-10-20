import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

import upscale


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = upscale.AppServer(("127.0.0.1", 0))
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(method, path, body=json.dumps(data) if data is not None else None,
                           headers=headers or {})
        response = connection.getresponse()
        code, result_headers, body = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return code, result_headers, body

    def test_status_token_and_assets(self):
        code, _, body = self.request("GET", "/api/status")
        self.assertEqual(code, 200)
        status = json.loads(body)
        self.assertTrue(status["token"])
        self.assertIn("ffmpeg", status["engines"])
        for path in ("/", "/style.css", "/app.js"):
            code, headers, body = self.request("GET", path)
            self.assertEqual(code, 200)
            self.assertTrue(body)
            self.assertIn("Content-Security-Policy", headers)

    def test_reject_untrusted_actions_then_validate_input(self):
        self.assertEqual(self.request("POST", "/api/probe", {})[0], 403)
        headers = {"X-App-Token": self.server.token, "Origin": "http://malicious.invalid"}
        self.assertEqual(self.request("POST", "/api/probe", {}, headers)[0], 403)
        headers.pop("Origin")
        code, _, body = self.request("POST", "/api/probe", {}, headers)
        self.assertEqual(code, 400)
        self.assertIn("Choose", json.loads(body)["error"])
        self.assertEqual(self.request("POST", "/api/jobs", [], headers)[0], 400)

    def test_reject_rebinding_and_path_traversal(self):
        self.assertEqual(self.request("GET", "/api/status", headers={"Host": "evil.invalid"})[0], 403)
        self.assertEqual(self.request("GET", "/../upscale.py")[0], 404)
        self.assertEqual(self.request("GET", "/media/missing/enhanced")[0], 404)

    def test_range_streaming(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"0123456789")
            job = upscale.Job({}, {}, True)
            job.files["enhanced"] = path
            job.update(status="done")
            self.server.jobs[job.id] = job
            try:
                route = f"/media/{job.id}/enhanced"
                code, headers, body = self.request("GET", route, headers={"Range": "bytes=2-5"})
                self.assertEqual((code, body), (206, b"2345"))
                self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
                self.assertEqual(self.request("GET", route, headers={"Range": "bytes=-3"})[2], b"789")
                self.assertEqual(self.request("GET", route, headers={"Range": "bytes=99-"})[0], 416)
            finally:
                del self.server.jobs[job.id]


if __name__ == "__main__":
    unittest.main()
