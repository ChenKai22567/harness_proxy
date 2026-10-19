import json
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading
import unittest

from core.config_manager import ConfigManager
from core.launcher_engine import LauncherEngine


class LoopbackBypassTests(unittest.TestCase):
    def test_custom_bypass_keeps_login_callbacks_local(self):
        with tempfile.TemporaryDirectory() as directory:
            config = ConfigManager(os.path.join(directory, "config.json"))
            config.config["proxy"]["bypass"] = "intranet.test;127.0.0.1"
            self.assertEqual(config.get_proxy_bypass_for_env(), "intranet.test,127.0.0.1,localhost,::1")
            self.assertIn("[::1]", config.get_proxy_bypass_for_chromium())

    def test_empty_bypass_keeps_login_callbacks_local(self):
        with tempfile.TemporaryDirectory() as directory:
            config = ConfigManager(os.path.join(directory, "config.json"))
            config.config["proxy"]["bypass"] = ""
            self.assertEqual(config.get_proxy_bypass_for_env(), "localhost,127.0.0.1,::1")


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for the routing integration test")
class NodeProxyRoutingTests(unittest.TestCase):
    def test_http_https_fetch_use_proxy_while_loopback_stays_direct(self):
        version = subprocess.check_output([shutil.which("node"), "--version"], text=True).strip()
        major, minor = (int(part) for part in version.lstrip("v").split(".")[:2])
        if not ((major == 22 and minor >= 21) or (major == 24 and minor >= 5) or major > 24):
            self.skipTest("Node.js with native environment proxy support is required")
        seen = []

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(5)
                data = b""
                while b"\r\n\r\n" not in data and len(data) < 8192:
                    chunk = self.request.recv(4096)
                    if not chunk:
                        return
                    data += chunk
                line = data.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
                seen.append(line)
                if line.startswith("CONNECT ") and ":443 " in line:
                    self.request.sendall(b"HTTP/1.1 502 Probe tunnel reached\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    return
                if line.startswith("CONNECT "):
                    # Undici also tunnels plain HTTP requests through CONNECT.
                    self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    data = b""
                    while b"\r\n\r\n" not in data and len(data) < 8192:
                        chunk = self.request.recv(4096)
                        if not chunk:
                            return
                        data += chunk
                    seen.append(data.split(b"\r\n", 1)[0].decode("ascii", errors="replace"))
                self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with tempfile.TemporaryDirectory() as directory:
                config = ConfigManager(os.path.join(directory, "config.json"))
                config.config["proxy"]["port"] = port
                config.config["proxy"]["bypass"] = ""
                engine = LauncherEngine(config, allow_process_control=False,
                                        log_file=os.path.join(directory, "launcher.log"))
                env, _, _, _ = engine._build_proxy_environment()
                # An inherited preload must not influence this standalone Node probe.
                env.pop("NODE_OPTIONS", None)
                script = r"""
const http = require('node:http'), https = require('node:https');
function request(mod, url) {
  return new Promise(resolve => {
    const r = mod.get(url, res => {res.resume(); resolve(res.statusCode);});
    r.on('error', e => resolve(e.code || e.message));
    r.setTimeout(3000, () => r.destroy(new Error('timeout')));
  });
}
Promise.all([
  request(http, 'http://proxy-probe.invalid/check'),
  request(https, 'https://proxy-probe.invalid/check'),
  request(http, 'http://127.0.0.1:' + process.argv[1] + '/callback'),
  fetch('http://proxy-probe.invalid/fetch', {signal: AbortSignal.timeout(4000)}).then(r => r.status)
]).then(result => console.log(JSON.stringify(result))).catch(error => {console.error(error.message); process.exit(1);});
"""
                result = subprocess.run([shutil.which("node"), "-e", script, str(port)], env=env,
                                        capture_output=True, text=True, timeout=12,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(result.returncode, 0, result.stderr + repr(seen))
                statuses = json.loads(result.stdout)
                self.assertEqual(statuses[0], 200)
                self.assertEqual(statuses[2:], [200, 200])
                self.assertIn("GET http://proxy-probe.invalid/check HTTP/1.1", seen)
                self.assertIn("CONNECT proxy-probe.invalid:443 HTTP/1.1", seen)
                self.assertIn("GET /callback HTTP/1.1", seen)
                self.assertNotIn("GET http://127.0.0.1:" + str(port) + "/callback HTTP/1.1", seen)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
