"""Only expose the paused browser page through a loopback-only human verification UI."""
import asyncio
import base64
import hmac
import json
import secrets
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HTML = '''<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<title>Binance 人工验证</title>
<style>body{font:16px system-ui;background:#181a20;color:#eee;margin:24px}img{max-width:100%;border:1px solid #666;cursor:pointer}p{max-width:1000px}input,button{font:inherit;padding:8px}</style>
<h2>在服务器浏览器中完成人工验证</h2>
<p>下方是暂停的服务器浏览器画面。请直接点击验证码。通过后监控自动恢复，本页面停止服务。</p>
<p id="status">连接中…</p><img id="screen" tabindex="0" alt="服务器浏览器画面">
<p><input id="text" maxlength="256" placeholder="需要文字时，在此输入"><button id="type">输入到浏览器</button><button id="enter">Enter</button><button id="tab">Tab</button></p>
<script>
const screen=document.querySelector('#screen'), status=document.querySelector('#status');
let pending=false;
async function refresh(){try{const r=await fetch('screen',{cache:'no-store'});if(!r.ok)throw Error();const u=URL.createObjectURL(await r.blob());const old=screen.src;screen.src=u;if(old.startsWith('blob:'))URL.revokeObjectURL(old);status.textContent='已连接，请操作下方验证码';setTimeout(refresh,1200)}catch(e){status.textContent='连接已结束或暂不可用；请查看监控日志确认是否恢复。'}}
async function action(data){if(pending)return;pending=true;try{const r=await fetch('action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});if(!r.ok)status.textContent='操作失败，请稍后重试'}finally{pending=false}}
screen.onclick=e=>{const r=screen.getBoundingClientRect();action({type:'click',x:(e.clientX-r.left)*screen.naturalWidth/r.width,y:(e.clientY-r.top)*screen.naturalHeight/r.height})};
screen.onwheel=e=>{e.preventDefault();action({type:'wheel',dy:Math.max(-700,Math.min(700,e.deltaY))})};
document.querySelector('#type').onclick=()=>action({type:'text',text:document.querySelector('#text').value});
document.querySelector('#enter').onclick=()=>action({type:'key',key:'Enter'});
document.querySelector('#tab').onclick=()=>action({type:'key',key:'Tab'});
refresh();
</script>'''


async def perform_action(page, data):
    kind = data.get('type')
    if kind == 'click':
        viewport = page.viewport_size
        x, y = float(data['x']), float(data['y'])
        if not viewport or not (0 <= x < viewport['width'] and 0 <= y < viewport['height']):
            raise ValueError('Invalid coordinates')
        await page.mouse.click(x, y)
    elif kind == 'wheel':
        dy = float(data['dy'])
        if not -700 <= dy <= 700:
            raise ValueError('Invalid scroll')
        await page.mouse.wheel(0, dy)
    elif kind == 'text' and isinstance(data.get('text'), str) and len(data['text']) <= 256:
        await page.keyboard.insert_text(data['text'])
    elif kind == 'key' and data.get('key') in ('Enter', 'Tab', 'Backspace', 'Escape'):
        await page.keyboard.press(data['key'])
    else:
        raise ValueError('Invalid action')


@asynccontextmanager
async def verification_portal(page, logger, port=8765):
    loop = asyncio.get_running_loop()
    token = secrets.token_urlsafe(24)
    operation_lock = asyncio.Lock()
    tasks = set()

    async def run_operation(kind, payload):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            async with operation_lock:
                if kind == 'screen':
                    # Capture the rendered surface without waiting for web fonts,
                    # which may never finish loading on a verification interstitial.
                    session = await page.context.new_cdp_session(page)
                    try:
                        result = await session.send('Page.captureScreenshot', {
                            'format': 'png', 'captureBeyondViewport': False,
                        })
                        return base64.b64decode(result['data'])
                    finally:
                        await session.detach()
                await perform_action(page, payload)
                return b'{}'
        finally:
            tasks.discard(task)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Avoid logging the access token or screenshot polling.

        def reply(self, status, body=b'', mime='text/plain; charset=utf-8'):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            host = self.headers.get('Host', '')
            if urlsplit('http://' + host).hostname not in ('127.0.0.1', 'localhost'):
                return False
            parts = urlsplit(self.path).path.split('/')
            if len(parts) != 3 or not hmac.compare_digest(parts[1], token):
                return False
            origin = self.headers.get('Origin')
            return origin is None or origin == 'http://' + host

        def operation(self, kind, payload=None):
            future = asyncio.run_coroutine_threadsafe(run_operation(kind, payload), loop)
            try:
                body = future.result(timeout=8)
                self.reply(200, body, 'image/png' if kind == 'screen' else 'application/json')
            except Exception as exc:
                future.cancel()
                logger.warning('人工验证接口操作失败: %s', type(exc).__name__)
                self.reply(400)

        def do_GET(self):
            if not self.authorized():
                self.reply(403)
                return
            endpoint = urlsplit(self.path).path.rsplit('/', 1)[1]
            if endpoint == '':
                self.reply(200, HTML.encode(), 'text/html; charset=utf-8')
            elif endpoint == 'screen':
                self.operation('screen')
            else:
                self.reply(404)

        def do_POST(self):
            if not self.authorized():
                self.reply(403)
                return
            if not self.path.endswith('/action'):
                self.reply(404)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 4096 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError()
            except (ValueError, TypeError):
                self.reply(400)
                return
            self.operation('action', data)

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

    class LocalServer(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            logger.warning('人工验证页面连接中断')

    server = LocalServer(('127.0.0.1', port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f'http://127.0.0.1:{server.server_port}/{token}/'
    logger.warning('人工验证入口（仅服务器本机，经 SSH 转发访问）: %s', address)
    try:
        yield address
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        for task in list(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)
