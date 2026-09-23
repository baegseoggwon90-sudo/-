"""웹 서버 API 테스트 (실제 서버를 띄워서 요청)."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from kbroll import web


def test_safe_name_strips_paths():
    assert web.safe_name("../../etc/passwd") == "passwd"
    assert web.safe_name("C:\\x\\영상.mp4") == "영상.mp4"
    assert web.safe_name("...") == "file"
    assert web.safe_name('a<b>:c.mp4') == "a_b__c.mp4"


@pytest.fixture
def server(tmp_path):
    project = web.Project(str(tmp_path / "wk"))
    handler = type("H", (web.Handler,), {"project": project})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", project
    httpd.shutdown()
    httpd.server_close()


def request(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, dict(res.headers), res.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def test_index_and_state(server):
    base, _ = server
    status, _, body = request(base + "/")
    assert status == 200 and "한국 영상 교체기".encode() in body
    status, _, body = request(base + "/api/state")
    state = json.loads(body)
    assert state["source"] is None and state["clips"] == []


def test_file_access_is_limited_to_workdir(server):
    base, project = server
    with open(project.path("output", "a.mp4"), "wb") as f:
        f.write(bytes(range(256)))
    assert request(base + "/files/../state.json")[0] == 404
    assert request(base + "/files/%2e%2e/%2e%2e/etc/passwd")[0] == 404
    status, headers, body = request(base + "/files/output/a.mp4", headers={"Range": "bytes=10-19"})
    assert status == 206 and body == bytes(range(10, 20))
    assert headers["Content-Range"] == "bytes 10-19/256"
    assert request(base + "/files/output/a.mp4", headers={"Range": "bytes=999-"})[0] == 416


def test_rejects_cross_site_and_bad_uploads(server):
    base, _ = server
    status, _, _ = request(base + "/api/scan", "POST", b"{}", {"Origin": "http://evil.example"})
    assert status == 403
    status, _, body = request(base + "/api/upload?kind=clip&name=x.exe", "PUT", b"abc")
    assert status == 400
    status, _, body = request(base + "/api/render", "POST", b'{"segments": []}')
    assert status == 400 and "원본" in json.loads(body)["error"]
