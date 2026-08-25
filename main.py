import asyncio
import re
import time
import traceback
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

# 自定义浏览器指纹，避免被币安 CloudFront WAF 识别为爬虫（默认 UA 会触发 403 拦截）
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

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
            txt = (await locator.text_content() or '').strip()
        except Exception:
            txt = ''
        if txt and txt != '--':
            return txt
        last = txt
        await asyncio.sleep(1.5)
    return last


async def goto_feed_page(page: Page, url: str):
    """打开 Binance Square 页面，等待正文区域出现；瞬时失败时重试。"""
    diagnostics = ''
    for attempt in range(1, PAGE_LOAD_ATTEMPTS + 1):
        response = None
        try:
            response = await page.goto(
                url,
                wait_until='domcontentloaded',
                timeout=PAGE_LOAD_TIMEOUT,
            )
            await page.wait_for_selector(
                FEED_ROOT_SELECTOR,
                state='visible',
                timeout=FEED_VISIBLE_TIMEOUT,
            )
            return
        except PlaywrightError as exc:
            status = response.status if response else '无响应'
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
                f'HTTP={status}, 最终URL={page.url}, 标题={title!r}, '
                f'页面摘要={body_excerpt!r}, 原因={exc}'
            )
            print(
                f'页面加载失败（第 {attempt}/{PAGE_LOAD_ATTEMPTS} 次）: '
                f'{diagnostics}'
            )
            if attempt < PAGE_LOAD_ATTEMPTS:
                await asyncio.sleep(attempt * 2)

    raise RuntimeError(
        f'连续 {PAGE_LOAD_ATTEMPTS} 次无法加载 Binance Square 正文: '
        f'{diagnostics}'
    )


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
        print(f'详情页正文容器未出现，尝试摘要回退: {exc}')

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
        print('详情页正文容器不可用，使用主页/页面摘要继续检查关键词')
        return article_text

    try:
        title = (await page.title()).strip() or '无标题'
    except Exception:
        title = '读取失败'
    print(f'详情页没有可用正文或摘要，跳过本帖: URL={page.url}, 标题={title!r}')
    return ''

async def binance_run(accounts):
    async with async_playwright() as playwright:
        # 关闭 AutomationControlled 特征，降低被反爬识别的概率
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="zh-CN",
            viewport={"width": 1280, "height": 900},
        )
        # 隐藏 webdriver 标志
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        try:
            await asyncio.gather(
                *(visit_account(context, account) for account in accounts)
            )
        finally:
            await browser.close()

async def visit_account(context: BrowserContext, account: str):
    async with sem:
        page = await context.new_page()
        try:
            url = f'https://www.binance.com/zh-CN/square/profile/{account}'
            print(f'Visiting URL: {url}')
            await goto_feed_page(page, url)
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
                    print(f'获取第 {idx} 条卡片文本失败: {e}')
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
                print('未找到候选文章，跳过')
                return

            for i, (rel, preview_text) in enumerate(candidate_articles):
                detail_url = f'https://www.binance.com{rel}'
                print(f'检查候选 {i}: {detail_url}')
                await goto_feed_page(page, detail_url)
                # 发布时间由 JS 异步渲染，冷加载可能先返回 '--'，原地轮询等待真实值
                await asyncio.sleep(2)
                create_time = await get_create_time(page)  # 14分钟 或 14 分钟前
                mins, ext = parse_create_time(create_time)
                print(f'Article create time: {create_time}, parsed as【{mins}】【{ext}】')

                if mins <= effective_time and ext == '分钟':
                    article_text = await get_article_text(page, preview_text)
                    if not article_text:
                        print('未能获取文章正文，继续检查下一条')
                        continue
                    if not check_keywords(article_text):
                        print('文章内容不包含关键词，继续检查下一条')
                        continue
                    if has_sends_url.get(detail_url):
                        print('该新闻已发送过通知，继续检查下一条')
                        continue
                    has_sends_url[detail_url] = True
                    print('准备发送钉钉通知')
                    res = send_dingtalk_markdown('binance广场消息报警: ' + account, article_text)
                    print(res)
                    break  # 本周期只推送一条，结束该账号检查
                else:
                    print('新闻发布时间超过有效时间或无时间戳，继续检查下一条')
                    continue
            else:
                print('候选文章均未命中条件，跳过')
        except Exception as e:
            # 单个账号出错不应中断整个监控循环，捕获后统一告警
            print(f'处理账号 {account} 时出错: {e}')
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
        print('未配置 dingding_token，跳过钉钉推送')
        return None
    webhook_url = f'https://oapi.dingtalk.com/robot/send?access_token={dingding_token}'
    try:
        response = requests.post(webhook_url, headers=headers, data=json.dumps(payload), timeout=10)
        return response.json()
    except Exception as e:
        print('钉钉推送请求失败:', e)
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
        print('错误告警处于 10 分钟冷却期，本次跳过')
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
    print('推送错误告警到钉钉...')
    res = _post_dingtalk(payload)
    print('错误告警发送结果:', res)

def mark_down_template(title, text):
    return f"""
## {title}
#### {text}
#### 时间: {get_current_time()}
> author <sorry510sf@gmail.com>`
"""

def get_current_time():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

async def main():
    print('Starting Binance news monitoring...')
    while True:
        try:
            if len(has_sends_url) > 1000:
                has_sends_url.clear()  # 清理已发送记录，防止内存占用过高
            print('Checking Binance news...')
            await binance_run(binance_accounts)
        except Exception as e:
            # 顶层兜底：浏览器启动/网络等致命错误也会触发告警
            print(f'检测循环发生异常: {e}')
            notify_error(e)
        print('Waiting for 60 seconds before the next check...')
        # 每 60 秒检查一次
        await asyncio.sleep(60)

if __name__ == '__main__':
    asyncio.run(main())
