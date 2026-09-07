import asyncio
import re
import time
import traceback
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Error as PlaywrightError,
    Page,
)
import requests
import json
from datetime import datetime
from dotenv import load_dotenv
import os

class TimestampFormatter(logging.Formatter):
    """每一行（包括 Playwright 多行异常）都附带同一条日志的时间。"""

    def format(self, record):
        message = super().format(record)
        prefix = f'[{self.formatTime(record, self.datefmt)}] [{record.levelname}] '
        return '\n'.join(prefix + line for line in (message.splitlines() or ['']))


logger = logging.getLogger('coin_news')
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(TimestampFormatter('%(message)s', datefmt='%Y-%m-%d %H:%M:%S %z'))
    logger.addHandler(handler)


# 加载 .env 文件
load_dotenv()  # 默认加载项目根目录的 .env 文件

has_sends_url = {} # 记录已发送通知的新闻链接,避免重复发送，最多存储1000条

dingding_token = os.getenv("dingding_token")
binance_accounts_str = os.getenv("binance_accounts", "")
binance_accounts = binance_accounts_str.split(",") if binance_accounts_str else []
effective_time = int(os.getenv("effective_time", "10"))
semaphore_limit = int(os.getenv("semaphore_limit", "1"))
keywords_str = os.getenv("keywords", "")
keywords = [k.strip() for k in keywords_str.split(",") if k.strip()]

sem = asyncio.Semaphore(semaphore_limit)  # 最多 n 个并发

BROWSER_DATA_DIR = Path(__file__).resolve().parent / '.browser-data'
NAVIGATION_INTERVAL = 3.0
CHALLENGE_TIMEOUT = 45_000

# 错误通知节流：同一类运行异常最多每 10 分钟推送一次钉钉，避免刷屏
ERROR_NOTIFY_INTERVAL = 10 * 60
last_error_notify_time = 0.0

# Binance Square 偶尔会返回风控页或出现前端资源加载超时。导航失败时有限重试，
# 并在最终异常中保留 HTTP 状态、最终 URL、标题和页面文本，方便区分网络/风控/DOM 改版。
FEED_ROOT_SELECTOR = '.feed-layout-main'
PAGE_LOAD_TIMEOUT = 30_000
FEED_VISIBLE_TIMEOUT = 20_000
PAGE_LOAD_ATTEMPTS = 3
ARTICLE_TEXT_TIMEOUT = 10_000
ARTICLE_TEXT_SELECTORS = (
    '.feed-layout-main .richtext-container',
    '.feed-layout-main .article-body',
)


class FeedPageLoadError(RuntimeError):
    """页面经有限重试仍不可用，调用方可隔离单篇故障。"""


class WafChallengeError(FeedPageLoadError):
    """会话访问受限，交给整轮监控处理，不能回退成单篇跳过。"""


class NavigationGate:
    """同一会话串行导航，验证期间不让其他账号继续发起请求。"""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_started = None
        self.blocked_error = None

    async def __aenter__(self):
        await self.lock.acquire()
        try:
            if self.blocked_error:
                raise self.blocked_error
            if self.last_started is not None:
                delay = NAVIGATION_INTERVAL - (time.monotonic() - self.last_started)
                if delay > 0:
                    await asyncio.sleep(delay)
            self.last_started = time.monotonic()
        except BaseException:
            self.lock.release()
            raise

    async def __aexit__(self, exc_type, exc, tb):
        if isinstance(exc, WafChallengeError):
            self.blocked_error = exc
        self.lock.release()


def parse_create_time(create_time: str):
    if not create_time:
        return 0, '未知'
    match = re.match(r'^(\d+)\s*(分钟|小时|天|月|年)(?:前)?$', create_time.strip())
    if not match:
        return 0, '未知'
    return int(match.group(1)), match.group(2)

async def get_create_time(page):
    """
    读取详情页发布时间。该字段由 JS 异步渲染，冷加载时可能先返回占位符 '--'，
    因此轮询重试：一旦拿到非 '--' 的有效相对时间（含 分钟/小时/天/月/年）即返回；
    若始终为 '--'/空，则返回最后读到的值（交由上层当作「未知」跳过该帖，继续检查下一篇）。
    """
    locator = page.locator('.feed-layout-main .author .create-time')
    last = ''
    for _ in range(7):
        try:
            txt = (await locator.text_content(timeout=1_000) or '').strip()
        except Exception:
            txt = ''
        if txt and txt != '--':
            return txt
        last = txt
        await asyncio.sleep(1.5)
    return last


async def goto_feed_page(page: Page, url: str, gate=None):
    """保留当前页面，让网站验证脚本完成后自动重新请求正文。"""
    if gate is not None:
        async with gate:
            return await goto_feed_page(page, url)

    diagnostics = ''
    restricted = False
    for attempt in range(1, PAGE_LOAD_ATTEMPTS + 1):
        response = None

        def on_response(current):
            nonlocal response
            if current.request.is_navigation_request() and current.request.frame == page.main_frame:
                response = current

        page.on('response', on_response)
        try:
            initial = await page.goto(url, wait_until='domcontentloaded', timeout=PAGE_LOAD_TIMEOUT)
            if response is None:
                response = initial
            action = initial.headers.get('x-amzn-waf-action', '') if initial else ''
            if action in ('challenge', 'captcha'):
                logger.info('网站要求浏览器验证，保持页面等待自动完成: URL=%s, WAF=%s', url, action)
            elif initial and initial.status >= 400:
                raise PlaywrightError(f'服务器返回 HTTP {initial.status}')
            await page.wait_for_selector(
                FEED_ROOT_SELECTOR,
                state='visible',
                timeout=CHALLENGE_TIMEOUT if action else FEED_VISIBLE_TIMEOUT,
            )
            if action:
                logger.info('浏览器验证完成，正文已加载: HTTP=%s, URL=%s',
                            response.status if response else '未知', page.url)
            return
        except PlaywrightError as exc:
            status = response.status if response else '无响应'
            waf_action = response.headers.get('x-amzn-waf-action', '') if response else ''
            if response is not None:
                restricted = bool(waf_action) or status in (403, 429)
            try:
                title = (await page.title()).strip() or '无标题'
            except Exception:
                title = '读取失败'
            try:
                body_text = await page.locator('body').inner_text(timeout=2_000)
                body_excerpt = re.sub(r'\s+', ' ', body_text).strip()[:300] or '空页面'
            except Exception:
                body_excerpt = '读取失败'
            diagnostics = (
                f'HTTP={status}, WAF={waf_action or "无"}, 最终URL={page.url}, 标题={title!r}, '
                f'页面摘要={body_excerpt!r}, 原因={exc}'
            )
            logger.warning('页面加载失败（第 %s/%s 次）: %s', attempt, PAGE_LOAD_ATTEMPTS, diagnostics)
        finally:
            page.remove_listener('response', on_response)
        if attempt < PAGE_LOAD_ATTEMPTS:
            await asyncio.sleep(attempt * (10 if restricted else 2))

    error_type = WafChallengeError if restricted else FeedPageLoadError
    raise error_type(f'连续 {PAGE_LOAD_ATTEMPTS} 次无法加载 Binance Square 正文: {diagnostics}')


async def get_article_text(page: Page, preview_text: str = ''):
    """读取详情页正文；详情模板不同时使用页面元数据或主页摘要回退。"""
    selector = ', '.join(ARTICLE_TEXT_SELECTORS)
    try:
        # 当前文章模板中 .article-body 包含 .richtext-container，取最后一个可避免
        # 把标题、免责声明等外层内容混入正文；若内层 class 改版则仍可命中外层。
        locator = page.locator(selector).last
        await locator.wait_for(state='visible', timeout=ARTICLE_TEXT_TIMEOUT)
        article_text = (await locator.inner_text(timeout=2_000) or '').strip()
        if article_text:
            return article_text
    except PlaywrightError as exc:
        logger.info(f'详情页正文容器未出现，尝试摘要回退: {exc}')

    fallback_candidates = [preview_text.strip()]
    for meta_selector in ('meta[name="description"]', 'meta[property="og:description"]'):
        try:
            content = await page.locator(meta_selector).get_attribute(
                'content',
                timeout=2_000,
            )
            if content and content.strip():
                fallback_candidates.append(content.strip())
        except PlaywrightError:
            continue

    article_text = max(fallback_candidates, key=len, default='')
    if article_text:
        logger.info('详情页正文容器不可用，使用主页/页面摘要继续检查关键词')
        return article_text

    try:
        title = (await page.title()).strip() or '无标题'
    except Exception:
        title = '读取失败'
    logger.info(f'详情页没有可用正文或摘要，跳过本帖: URL={page.url}, 标题={title!r}')
    return ''

@asynccontextmanager
async def browser_session():
    async with async_playwright() as playwright:
        # 使用原生 Chromium 配置，保留独立用户目录中的站点存储和缓存。
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DATA_DIR),
            channel='chromium',
            headless=True,
            locale='zh-CN',
            viewport={'width': 1280, 'height': 900},
        )
        logger.info('浏览器会话已启动，跨轮复用站点存储和缓存')
        try:
            yield context
        finally:
            await context.close()


async def binance_run(accounts, context=None):
    if context is None:
        async with browser_session() as context:
            return await binance_run(accounts, context)
    gate = NavigationGate()
    tasks = [asyncio.create_task(visit_account(context, account, gate)) for account in accounts]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def visit_account(context: BrowserContext, account: str, gate=None):
    async with sem:
        page = await context.new_page()
        try:
            url = f'https://www.binance.com/zh-CN/square/profile/{account}'
            logger.info(f'Visiting URL: {url}')
            await goto_feed_page(page, url, gate)
            # 币安已移除 .FeedList 包裹层，直接使用 .feed-card
            # 先在个人主页收集前 2 篇非置顶文章的链接，再逐篇进入详情页检查时间与关键词：
            # 找到第一篇「最近 N 分钟内 + 含关键词」的即推送并结束；若某篇无时间戳(--)或超过
            # 有效时间则继续检查下一篇，避免被异常帖卡住导致漏检。
            # 注意：必须先收集完链接再导航，否则离开主页后卡片选择器将失效。
            candidate_articles = []
            idx = 0
            while idx < 6 and len(candidate_articles) < 2:
                card_locator = page.locator('.feed-layout-main .feed-card').nth(idx)
                try:
                    await card_locator.wait_for(state='visible', timeout=5000)
                    card_text = await card_locator.text_content() or ''
                except Exception as e:
                    logger.info(f'获取第 {idx} 条卡片文本失败: {e}')
                    idx += 1
                    continue
                if card_text.strip() and '置顶' not in card_text:
                    content_locator = card_locator.locator('.feed-content-text').nth(0)
                    href = await content_locator.locator('a').nth(0).get_attribute('href')
                    if href:
                        try:
                            preview_text = (await content_locator.inner_text() or '').strip()
                        except PlaywrightError:
                            preview_text = card_text.strip()
                        candidate_articles.append((href, preview_text))
                idx += 1

            if not candidate_articles:
                logger.info('未找到候选文章，跳过')
                return

            failed_articles = 0
            for i, (rel, preview_text) in enumerate(candidate_articles):
                detail_url = f'https://www.binance.com{rel}'
                logger.info(f'检查候选 {i}: {detail_url}')
                try:
                    await goto_feed_page(page, detail_url, gate)
                except WafChallengeError:
                    raise
                except FeedPageLoadError as exc:
                    failed_articles += 1
                    logger.warning('详情页网络或渲染失败，继续检查下一条: %s', detail_url)
                    notify_error(exc)
                    continue
                await asyncio.sleep(2)
                create_time = await get_create_time(page)
                mins, ext = parse_create_time(create_time)
                logger.info(f'Article create time: {create_time}, parsed as【{mins}】【{ext}】')

                if mins <= effective_time and ext == '分钟':
                    article_text = await get_article_text(page, preview_text)
                    if not article_text:
                        logger.info('未能获取文章正文，继续检查下一条')
                        continue
                    if not check_keywords(article_text):
                        logger.info('文章内容不包含关键词，继续检查下一条')
                        continue
                    if has_sends_url.get(detail_url):
                        logger.info('该新闻已发送过通知，继续检查下一条')
                        continue
                    has_sends_url[detail_url] = True
                    logger.info('准备发送钉钉通知')
                    res = send_dingtalk_markdown('binance广场消息报警: ' + account, article_text)
                    logger.info(res)
                    break  # 本周期只推送一条，结束该账号检查
                else:
                    logger.info('新闻发布时间超过有效时间或无时间戳，继续检查下一条')
                    continue
            else:
                if failed_articles:
                    logger.info(f'本轮有 {failed_articles} 篇加载失败，其余候选未命中条件，下轮重试')
                else:
                    logger.info('候选文章均未命中条件，跳过')
        except WafChallengeError:
            raise
        except Exception as e:
            # 单个账号出错不应中断整个监控循环，捕获后统一告警
            logger.error(f'处理账号 {account} 时出错: {e}')
            notify_error(e)
        finally:
            await page.close()

def check_keywords(article: str):
    """
    检查文章内容是否包含特定关键词
    :param article: 文章内容
    :return: bool, 是否包含关键词
    """
    return any(keyword in article for keyword in keywords)

def _post_dingtalk(payload):
    """发送钉钉消息，返回响应；发送失败仅打印不影响主流程"""
    headers = {'Content-Type': 'application/json'}
    if not dingding_token:
        logger.info('未配置 dingding_token，跳过钉钉推送')
        return None
    webhook_url = f'https://oapi.dingtalk.com/robot/send?access_token={dingding_token}'
    try:
        response = requests.post(webhook_url, headers=headers, data=json.dumps(payload), timeout=10)
        return response.json()
    except Exception as e:
        logger.error('钉钉推送请求失败: %s', e)
        return None

def send_dingtalk_markdown(title, text, is_at_all=True):
    """
    发送钉钉 Markdown 格式消息

    :param title: 消息标题（显示在通知卡片上）
    :param text: Markdown 格式的消息内容
    :param is_at_all: 是否@所有人
    """
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": mark_down_template(title, text)
        },
        "at": {
            "atMobiles": [],
            "isAtAll": is_at_all
        }
    }
    return _post_dingtalk(payload)

def notify_error(exc: Exception):
    """
    捕获程序运行异常并推送钉钉告警。
    通过全局节流控制：相同/连续的运行异常最多每 10 分钟推送一次，避免刷屏。
    """
    global last_error_notify_time
    now = time.time()
    if now - last_error_notify_time < ERROR_NOTIFY_INTERVAL:
        logger.info('错误告警处于 10 分钟冷却期，本次跳过')
        return
    last_error_notify_time = now

    tb = traceback.format_exc()
    detail = tb if tb and tb.strip() and 'NoneType: None' not in tb else str(exc)
    title = "binance广场消息监控程序异常报警"
    markdown_text = (
        f"## {title}\n"
        f"> 时间：{get_current_time()}\n\n"
        f"程序运行中出现异常，最近一次报错信息如下：\n\n"
        f"```\n{detail[-1800:]}\n```"
    )
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": markdown_text},
        "at": {"atMobiles": [], "isAtAll": False},
    }
    logger.info('推送错误告警到钉钉...')
    res = _post_dingtalk(payload)
    logger.info('错误告警发送结果: %s', res)

def mark_down_template(title, text):
    return f"""
## {title}
#### {text}
#### 时间: {get_current_time()}
> author <sorry510sf@gmail.com>`
"""

def get_current_time():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

async def monitor_loop(context, accounts):
    while True:
        try:
            if len(has_sends_url) > 1000:
                has_sends_url.clear()
            logger.info('开始检查 Binance 新闻')
            await binance_run(accounts, context)
        except WafChallengeError as exc:
            logger.error('浏览器验证未完成，整轮监控暂停；保留会话，60 秒后重新检查: %s', exc)
            notify_error(exc)
        except Exception as exc:
            logger.error('检测循环发生异常: %s', exc)
            notify_error(exc)
            if not context.browser or not context.browser.is_connected():
                raise
        logger.info('等待 60 秒后进行下一轮检查')
        await asyncio.sleep(60)


async def main():
    logger.info('启动 Binance 新闻监控')
    while True:
        try:
            async with browser_session() as context:
                await monitor_loop(context, binance_accounts)
        except Exception as exc:
            logger.error('浏览器会话异常，60 秒后重建: %s', exc)
            notify_error(exc)
            await asyncio.sleep(60)


if __name__ == '__main__':
    asyncio.run(main())
