"""MU-14 executable characterization, not a production policy decision.

A token minted on worker A is rejected on worker B and after restarting A.
Only synthetic credentials exist; tokens never leave process memory.
"""
import os, socket, subprocess, sys, time
import httpx
import signal
def _cancel(signum, frame):
    raise KeyboardInterrupt
signal.signal(signal.SIGTERM, _cancel)

def start():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    child=subprocess.Popen([sys.executable,'-m','uvicorn','agent_base.mocks.oss:create_app','--factory','--port',str(port),'--log-level','critical'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    url=f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            assert child.poll() is None
            try:
                if httpx.get(url+'/health',timeout=.2).status_code==200: return child,url
            except httpx.RequestError: pass
            time.sleep(.1)
        raise TimeoutError('mock readiness')
    except BaseException: stop(child); raise

def stop(child):
    child.terminate()
    try: child.wait(timeout=5)
    except subprocess.TimeoutExpired: child.kill(); child.wait()

children=[]
try:
    a,url_a=start(); children.append(a)
    b,url_b=start(); children.append(b)
    response=httpx.post(url_a+'/auth/login',json={'account':'w000001','password':'demo-pass-1'})
    assert response.status_code==200
    headers={'X-Auth-Token':response.json()['token']}
    assert httpx.post(url_a+'/auth/getUserDetail',headers=headers).status_code==200
    assert httpx.post(url_b+'/auth/getUserDetail',headers=headers).status_code==401
    stop(a); children.remove(a)
    restarted,url=start(); children.append(restarted)
    assert httpx.post(url+'/auth/getUserDetail',headers=headers).status_code==401
    print('MU-14 characterization PASS: other worker=401, restart=401; policy unresolved')
finally:
    for child in children: stop(child)
