import asyncio
from playwright.async_api import async_playwright, Playwright
import requests
import json
from datetime import datetime
from dotenv import load_dotenv
import os

# 加载 .env 文件
load_dotenv()  # 默认加载项目根目录的 .env 文件

dingding_token = os.getenv("dingding_token")


binance_accounts = [
    'binance_announcement',
    'binance_news',
    'binancezh',
]

async def binance_run(playwright: Playwright, accounts):
    chromium = playwright.chromium # or "firefox" or "webkit".
    browser = await chromium.launch(headless=True)  # Set headless=False to see the browser actions
    page = await browser.new_page()
    for account in accounts:
        url = f'https://www.binance.com/zh-CN/square/profile/{account}'
        print(f'Visiting URL: {url}')
        await page.goto(url)
        await page.wait_for_selector('.feed-layout-main', state='visible', timeout=20000)
        first_article_url = None
        index = 0
        while not first_article_url:
            cardText = await page.locator('.feed-layout-main .FeedList .feed-content-text').nth(index).text_content()  # 获取文章内容
            if '置顶' not in cardText:
                first_article_url = await page.locator('.feed-layout-main .FeedList .feed-content-text a').nth(index).get_attribute('href') # 找到第一个非置顶，进入明细页面
            index += 1
        detail_url = f'https://www.binance.com/zh-CN{first_article_url}'
        print(f'First article URL: {detail_url}')
        await page.goto(detail_url)
        await page.wait_for_selector('.feed-layout-main', state='visible', timeout=20000)
        create_time = await page.locator('.feed-layout-main .author .create-time').text_content()  # 获取文章内容
        timeArr = create_time.split(' ')  # 只保留日期部分
        if len(timeArr) < 2:
            continue
        mins = timeArr[0]  # 获取分钟数
        ext = timeArr[1]  # 获取时间单位（分钟、小时等）
        if int(mins) <= 5 and ext.startswith('分钟'):
            # 3 分钟前的新闻发送通知
            print('准备发送钉钉通知')
            article_text = await page.locator('.feed-layout-main .richtext-container').text_content()
            if not check_keywords(article_text):
                continue
            res = send_dingtalk_markdown('bn报警通知: ' + account, article_text)
            print(res)
    await browser.close()
    
def check_keywords(article: str):
    """
    检查文章内容是否包含特定关键词
    :param article: 文章内容
    :return: bool, 是否包含关键词
    """
    keywords = ['活动', '奖励', 'Alpha', '上线', '空投', '赠送', '福利', '送出', '补贴', '交易大赛', '交易竞赛']
    return any(keyword in article for keyword in keywords)
    
    
def send_dingtalk_markdown(title, text, is_at_all=True):
    """
    发送钉钉 Markdown 格式消息
    
    :param title: 消息标题（显示在通知卡片上）
    :param text: Markdown 格式的消息内容
    :param is_at_all: 是否@所有人
    """
    headers = {'Content-Type': 'application/json'}
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
    webhook_url = f'https://oapi.dingtalk.com/robot/send?access_token={dingding_token}'
    response = requests.post(webhook_url, headers=headers, data=json.dumps(payload))
    return response.json()

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
        print('Checking Binance news...')
        async with async_playwright() as playwright:
            await binance_run(playwright, binance_accounts)
        print('Waiting for 60 seconds before the next check...')
        # 每 60 秒检查一次
        await asyncio.sleep(60)
        
if __name__ == '__main__':      
    asyncio.run(main())