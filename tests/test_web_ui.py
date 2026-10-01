"""Run inside the disposable running container. No Muse account/network calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, '/app')
from engine import MuseEngine

keys=json.loads(Path('/app/data/auth.json').read_text())
with tempfile.TemporaryDirectory(prefix='muse-ui-') as tmp:
    cfg=SimpleNamespace(chromium='/usr/bin/chromium', cdp_port=19211,
        home_dir=tmp,extra_path='',profile_dir=tmp+'/profile',data_dir=tmp,download_dir=tmp)
    engine=MuseEngine(cfg)
    try:
        engine.start();page=engine._open_page();engine.page=page
        page.send('Page.navigate',{'url':'http://127.0.0.1:18610/admin'})
        for _ in range(50):
            if page.js("Boolean(document.getElementById('connect'))"):break
            time.sleep(.1)
        page.js("document.getElementById('key').value="+json.dumps(keys['admin'])+";document.getElementById('connect').click()")
        for _ in range(50):
            if page.js("document.getElementById('message').textContent.includes('已连接')"):break
            time.sleep(.1)
        assert page.js("document.getElementById('apiKey').value") == keys['api'], 'Admin login failed'
        page.js("document.getElementById('rotate').click = document.getElementById('rotate').click.bind(document.getElementById('rotate')); window.confirm=()=>true; document.getElementById('rotate').click()")
        for _ in range(50):
            current=page.js("document.getElementById('apiKey').value")
            if current != keys['api']:break
            time.sleep(.1)
        assert current != keys['api'], 'API rotation did not update UI'
        assert page.js("document.getElementById('key').value") == keys['admin'], 'API rotation replaced admin key'
        assert json.loads(Path('/app/data/auth.json').read_text())['api'] == current
        # Reproduce the user's export format with fake cookies, never a live login.
        fixture='clipboard-label | '+ '; '.join(n+'=fixture' for n in ('hatch_sess','hatch_gw','hatch_vml','hatch_native_auth_device'))
        page.js("document.getElementById('cookies').value='hatch_sess=fixture';document.getElementById('add').click()")
        for _ in range(50):
            if page.js("document.getElementById('importMessage').textContent.includes('缺少必要')"):break
            time.sleep(.1)
        assert page.js("document.getElementById('importMessage').textContent.includes('hatch_gw')"), 'Missing-cookie feedback not inline'
        page.js("document.getElementById('cookies').value="+json.dumps(fixture)+";document.getElementById('add').click()")
        for _ in range(50):
            if page.js("document.getElementById('importMessage').textContent.includes('导入成功') && document.getElementById('accounts').textContent.includes('clipboard-label')"):break
            time.sleep(.1)
        assert page.js("document.getElementById('accounts').textContent.includes('clipboard-label')"), 'Prefixed cookie import failed'
        page.js("(async()=>{const p=await api('/admin/accounts');for(const a of p.accounts){if(a.label==='clipboard-label')await api('/admin/accounts/'+a.id,null,'DELETE');}})()",await_promise=True)
        # Simulate a stalled SSH forward and accelerate only its 15s deadline.
        page.js("window.realFetch=window.fetch;window.realTimeout=window.setTimeout;window.setTimeout=(fn,ms,...args)=>realTimeout(fn,ms===15000?100:ms,...args);window.fetch=(url,options)=>url==='/admin/accounts'&&options.method==='POST'?new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')))):realFetch(url,options);document.getElementById('cookies').value="+json.dumps(fixture)+";document.getElementById('add').click()")
        for _ in range(50):
            if page.js("document.getElementById('importMessage').textContent.includes('请求超时')"):break
            time.sleep(.1)
        assert page.js("document.getElementById('importMessage').textContent.includes('SSH') && !document.getElementById('add').disabled"), 'Stalled tunnel did not recover import button'
        page.js("window.fetch=realFetch;window.setTimeout=realTimeout;")
        print('PASS prefixed cookie import, inline validation and stalled-tunnel timeout')
        page.send('Page.navigate',{'url':'http://127.0.0.1:18610/'})
        for _ in range(50):
            if page.js("Boolean(document.getElementById('apiKey'))"):break
            time.sleep(.1)
        page.js("document.getElementById('apiKey').value="+json.dumps(current)+";document.getElementById('saveCfg').click()")
        for _ in range(50):
            if page.js("document.getElementById('statusText').textContent==='已连接'"):break
            time.sleep(.1)
        assert page.js("document.getElementById('statusText').textContent") == '已连接', 'Workbench auth failed'
        assert page.js("document.getElementById('baseUrl').value") == 'http://127.0.0.1:18610'
        assert not page.js("Object.values(localStorage).some(v=>v.includes('m2a_'))"), 'Persistent browser key storage'
        print('PASS browser admin login, persistent rotation and workbench authentication')
    finally:engine.stop()
