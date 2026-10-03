"""Run inside the disposable running container. No Muse account/network calls."""
import json
import base64
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
        page.js("document.getElementById('media').click()")
        for _ in range(50):
            if page.js("document.getElementById('mediaMessage').textContent.includes('还没有生成文件')"):break
            time.sleep(.1)
        assert page.js("document.getElementById('mediaMessage').textContent.includes('还没有生成文件') && !document.getElementById('media').disabled")
        page.js("window.fetch=(url,options)=>url==='/admin/media'?Promise.resolve(new Response(JSON.stringify({detail:'fixture media failure'}),{status:503,headers:{'Content-Type':'application/json'}})):realFetch(url,options);document.getElementById('media').click()")
        for _ in range(50):
            if page.js("document.getElementById('mediaMessage').textContent.includes('fixture media failure')"):break
            time.sleep(.1)
        assert page.js("document.getElementById('mediaMessage').textContent.includes('fixture media failure') && !document.getElementById('media').disabled")
        page.js("window.fetch=realFetch;")
        print('PASS media loading feedback and inline failure reporting')
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
        # Exercise the workbench controls with deterministic generation replies.
        # Only generation/poll requests are mocked: media auth, image download,
        # decoding and the admin media listing use the real container service.
        fixture_path=Path('/app/data/media/workbench-fixture.png')
        fixture_path.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII='))
        try:
            page.js('''window.fixturePosts=[];window.fixtureRealFetch=window.fetch;
                window.fetch=async function(url,options){
                  const path=new URL(url,location.origin).pathname;
                  const reply=(body,status=200)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}});
                  if(options && options.method==='POST' && (path==='/v1/images/tasks'||path==='/v1/videos')){
                    const body=JSON.parse(options.body);fixturePosts.push({path,body});
                    return reply({id:'ui-task-'+fixturePosts.length,status:'queued'},202);
                  }
                  if(path.startsWith('/v1/images/tasks/ui-task-')){
                    return reply({status:'completed',result:{url:'/v1/media/workbench-fixture.png',size:68,kind:'image'}});
                  }
                  if(path.startsWith('/v1/videos/ui-task-')){
                    return reply({status:'failed',error:'fixture video failure'});
                  }
                  return fixtureRealFetch(url,options);
                };
                document.getElementById('modeImage').click();
                document.getElementById('prompt').value='fixture image description';
                document.querySelector('[data-size="1:1"]').click();
            ''')
            assert page.js("getComputedStyle(document.getElementById('durationField')).display==='none' && document.getElementById('genBtnText').textContent==='生成图片'")
            page.js("document.getElementById('genBtn').click()")
            for _ in range(100):
                if page.js("Boolean(document.querySelector('#taskArea img.result-image')?.naturalWidth)"):break
                time.sleep(.1)
            assert page.js("Boolean(document.querySelector('#taskArea img.result-image')?.naturalWidth)"), 'Generated image not decoded'
            first=json.loads(page.js('JSON.stringify(fixturePosts[0])'))
            assert first['path']=='/v1/images/tasks' and first['body']['size']=='1:1'
            assert first['body']['model']=='muse-image' and 'duration' not in first['body']
            assert 'reference_image' not in first['body']
            assert page.js("document.querySelector('#taskArea a[download]').textContent==='下载图片'")
            assert not page.js("document.querySelector('#taskArea .meta-txt').textContent.includes('5s')")
            # Reference image selection follows the same browser FileReader path
            # as a user selecting a file. No reference bytes enter history storage.
            page.js('''const transfer=new DataTransfer();transfer.items.add(new File(['fixture'],'reference.png',{type:'image/png'}));
                document.getElementById('file').files=transfer.files;document.getElementById('file').dispatchEvent(new Event('change'));
            ''')
            for _ in range(50):
                if page.js("document.getElementById('drop').classList.contains('has-img')"):break
                time.sleep(.1)
            page.js("document.getElementById('genBtn').click()")
            for _ in range(100):
                if page.js("fixturePosts.length===2 && !document.getElementById('genBtn').disabled"):break
                time.sleep(.1)
            second=json.loads(page.js('JSON.stringify(fixturePosts[1])'))
            assert second['body']['reference_image'].startswith('data:image/png;base64,')
            assert 'data:image' not in page.js("sessionStorage.getItem('muse_video_hist')"), 'History retained reference bytes'
            page.js("document.getElementById('modeVideo').click();document.getElementById('duration').value='10';document.getElementById('genBtn').click()")
            for _ in range(100):
                if page.js("fixturePosts.length===3 && document.getElementById('taskArea').textContent.includes('fixture video failure')"):break
                time.sleep(.1)
            third=json.loads(page.js('JSON.stringify(fixturePosts[2])'))
            assert third['path']=='/v1/videos' and third['body']['duration']==10
            assert not page.js("document.getElementById('durationField').hidden")
            # History retains each task's kind despite switching the creation mode.
            page.js("document.querySelectorAll('.hist-item')[1].click()")
            for _ in range(50):
                if page.js("Boolean(document.querySelector('#taskArea img.result-image')?.naturalWidth)"):break
                time.sleep(.1)
            assert page.js("Boolean(document.querySelector('#taskArea img.result-image')?.naturalWidth)")
            page.send('Page.reload')
            for _ in range(100):
                if page.js("document.querySelectorAll('.hist-item').length===3"):break
                time.sleep(.1)
            page.js("document.querySelectorAll('.hist-item')[1].click()")
            for _ in range(50):
                if page.js("Boolean(document.querySelector('#taskArea img.result-image')?.naturalWidth)"):break
                time.sleep(.1)
            assert page.js("Boolean(document.querySelector('#taskArea img.result-image')?.naturalWidth)"), 'Image history lost its kind after reload'
            page.send('Page.navigate',{'url':'http://127.0.0.1:18610/admin'})
            for _ in range(100):
                if page.js("Boolean(document.getElementById('media'))"):break
                time.sleep(.1)
            page.js("document.getElementById('media').click()")
            for _ in range(100):
                if page.js("document.getElementById('files').textContent.includes('workbench-fixture.png')"):break
                time.sleep(.1)
            assert page.js("document.getElementById('files').textContent.includes('workbench-fixture.png')"), 'Image missing in unified admin file list'
            print('PASS text/image generation controls, reference upload, image preview/download, mixed history, video regression and admin image list')
        finally:
            fixture_path.unlink(missing_ok=True)
    finally:engine.stop()
