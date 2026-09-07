import asyncio
import http.client
import json
import unittest
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import AsyncMock, MagicMock
from manual_verification import verification_portal


def request(url, method='GET', body=None, headers=None):
    parsed = urlsplit(url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    try:
        connection.request(method, parsed.path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


class PortalTests(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_portal_requires_token_and_forwards_only_user_input(self):
        page = SimpleNamespace(
            viewport_size={'width': 1280, 'height': 900},
            screenshot=AsyncMock(return_value=b'PNG'),
            mouse=SimpleNamespace(click=AsyncMock(), wheel=AsyncMock()),
            keyboard=SimpleNamespace(insert_text=AsyncMock(), press=AsyncMock()),
        )
        session = SimpleNamespace(send=AsyncMock(return_value={'data':'UE5H'}), detach=AsyncMock())
        page.context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=session))
        async with verification_portal(page, MagicMock(), port=0) as url:
            status, html = await asyncio.to_thread(request, url)
            self.assertEqual(status, 200)
            self.assertIn('人工验证'.encode(), html)
            status, content = await asyncio.to_thread(request, url+'screen')
            self.assertEqual((status, content), (200, b'PNG'))
            invalid = urlsplit(url)._replace(path='/wrong/screen').geturl()
            status, _ = await asyncio.to_thread(request, invalid)
            self.assertEqual(status, 403)
            data = json.dumps({'type':'click', 'x':50, 'y':40})
            headers = {'Content-Type':'application/json'}
            status, _ = await asyncio.to_thread(request, url+'action', 'POST', data, headers)
            self.assertEqual(status, 200)
            page.mouse.click.assert_awaited_once_with(50, 40)
            headers['Origin'] = 'https://untrusted.example'
            status, _ = await asyncio.to_thread(request, url+'action', 'POST', data, headers)
            self.assertEqual(status, 403)
            self.assertEqual(page.mouse.click.await_count, 1)
            status, _ = await asyncio.to_thread(request, url+'action', 'POST',
                json.dumps({'type':'key','key':'Control+L'}), {'Content-Type':'application/json'})
            self.assertEqual(status, 400)
            page.keyboard.press.assert_not_awaited()
        with self.assertRaises(OSError):
            await asyncio.to_thread(request, url)


if __name__ == '__main__':
    unittest.main()
