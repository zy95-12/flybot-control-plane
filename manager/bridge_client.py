"""Private persistent stdin/stdout bridge; never expose or log its payloads."""
from __future__ import annotations
import json
import os
import selectors
import subprocess
import time

SCRIPT = '''import json,sys
from app.manager_bridge import handle
for line in sys.stdin.buffer:
    try:
        if len(line)>65536: raise ValueError()
        response=handle(json.loads(line))
    except Exception:
        response={'bridge_error':'operation_failed'}
    print(json.dumps(response),flush=True)
'''


class BridgeError(Exception):
    pass


class BridgeClient:
    def __init__(self, container):
        self.container=container
        self.process=None
        self.buffer=b''

    def close(self):
        process=self.process;self.process=None;self.buffer=b''
        if process:
            try:
                process.stdin.close()
                process.wait(timeout=0.5)
            except Exception:
                process.terminate()
                try:process.wait(timeout=1)
                except Exception:process.kill();process.wait(timeout=1)
            finally:
                process.stdout.close()

    def request(self, data):
        payload=json.dumps(data).encode()+b'\n'
        if len(payload)>65536:raise BridgeError('payload_too_large')
        if self.process is None or self.process.poll() is not None:
            self.close()
            self.process=subprocess.Popen(['docker','exec','-i',self.container,'python','-u','-c',SCRIPT],
                                          stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0)
        try:
            self.process.stdin.write(payload)
            deadline=time.monotonic()+30
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout,selectors.EVENT_READ)
                while b'\n' not in self.buffer:
                    remaining=deadline-time.monotonic()
                    if remaining<=0 or not selector.select(remaining):raise BridgeError('bridge_timeout')
                    chunk=os.read(self.process.stdout.fileno(),65537)
                    if not chunk:raise BridgeError('bridge_closed')
                    self.buffer+=chunk
                    if len(self.buffer)>65536:raise BridgeError('response_too_large')
            line,self.buffer=self.buffer.split(b'\n',1)
            result=json.loads(line)
            if 'bridge_error' in result:raise BridgeError('operation_failed')
            return result
        except Exception:
            self.close()
            raise BridgeError('private_bridge_unavailable') from None

    def __del__(self):
        try:self.close()
        except Exception:pass
