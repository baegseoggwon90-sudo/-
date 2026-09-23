import os
import stat
import sys

import pytest

from kbroll import userconfig
from kbroll.tunnel import Tunnel, TunnelError

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="가짜 cloudflared 가 셸 스크립트")


def fake_cloudflared(tmp_path, body):
    exe = tmp_path / "cloudflared"
    exe.write_text("#!/bin/sh\n" + body)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return str(exe)


def test_tunnel_reads_url(tmp_path):
    exe = fake_cloudflared(tmp_path, """
echo "INF Requesting new quick Tunnel on trycloudflare.com..." >&2
echo "INF +----------------------------------------------+" >&2
echo "INF |  https://brave-mango-river.trycloudflare.com  |" >&2
sleep 30
""")
    tun = Tunnel(8765, exe=exe)
    try:
        assert tun.start(log=lambda _: None, timeout=10) == "https://brave-mango-river.trycloudflare.com"
    finally:
        tun.stop()
    assert tun.proc.poll() is not None


def test_tunnel_fails_cleanly(tmp_path):
    exe = fake_cloudflared(tmp_path, 'echo "ERR failed to connect" >&2\nexit 1\n')
    with pytest.raises(TunnelError):
        Tunnel(8765, exe=exe).start(log=lambda _: None, timeout=10)


def test_password_hash_roundtrip():
    stored = userconfig.hash_password("secret-123")
    assert "secret" not in stored
    assert userconfig.check_password("secret-123", stored)
    assert not userconfig.check_password("secret-124", stored)
    assert not userconfig.check_password("x", "garbage")
