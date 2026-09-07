## 本地部署
### 安装依赖
```commandline
pip install -r requirements.txt
```

### 安装chromium插件
```commandline
playwright install chromium
```

监控使用完整 Chromium 的无头模式，需要执行上述安装命令；仅安装 `--only-shell` 不够。

## 运行
```commandline
python main.py
```

## uv

### 安装
```
uv venv 
uv pip install -r requirements.txt
```

### 运行

```
uv run main.py
```

## 浏览器会话与日志

浏览器在各轮检查之间保持运行，使用项目目录下的 `.browser-data/` 保存站点存储和缓存。该目录已加入 Git 忽略；同一目录只能由一个监控进程使用。

遇到 `HTTP 202 / WAF challenge` 时，保留当前页面，让网站自带的 JavaScript 验证完成并自动重新请求正文。同一会话的页面导航串行执行，开始时间至少间隔 3 秒。验证持续失败或收到访问限制时，整轮监控暂停并告警，保留会话等待 60 秒后重新检查，不用摘要冒充详情页读取成功。

所有程序日志统一输出到标准输出，每行包含运行机器的当前本地时间、时区偏移和日志级别，多行异常也逐行加前缀：

```text
[2026-09-06 20:31:17 +0800] [INFO] 浏览器验证完成，正文已加载: HTTP=200, URL=...
```

更新后重启监控进程；使用仓库 PM2 配置部署时可执行 `pm2 restart coin_news`。

## 回归测试

```commandline
dingding_token= python -m unittest -q test_main
```

测试覆盖验证完成后的正文读取、持续风控的整轮处理、会话复用、导航间隔和多行日志时间格式，不发送钉钉消息。
