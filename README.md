# 🍅 番茄小说一键下载器

一个本地运行的轻量级番茄小说下载工具：粘贴分享链接 → 解析书籍信息 → 一键导出排版干净的 TXT。
无广告、**无需登录即可下载全书**（含 App 独占书与会员/SVIP 章节），无需 Node.js 构建链，双击即用。

> 仓库地址：<https://github.com/Jees996/fanqie-novel-downloader>
> · 问题反馈：<https://github.com/Jees996/fanqie-novel-downloader/issues>

## 功能特性

- **链接解析**：支持 App 分享文案、`changdunovel.com` 短链、PC 网页链接、纯书籍 ID
- **书籍信息**：封面、书名、作者、评分、字数、章节数、连载状态、简介
- **全本下载**：走番茄 APP 网关（匿名设备 + 请求签名 + AES 解密），App 独占书、会员章节也能拿到全文
- **实时进度**：SSE 推送逐章下载进度，前端进度条 + 当前章节标题
- **字体解密**：网页通道的 `awesome-font` 私有区映射（362 个字符）已内置，作为兜底通道
- **TXT 导出**：UTF-8 BOM，Windows 记事本直接打开不乱码；章节标题 + 段落缩进排版
- **纯本地**：FastAPI 单进程 + 单文件前端，Tailwind/Vue3 CDN，不依赖 npm

## 快速开始

### Windows

双击 `run.bat`。脚本会自动：检查 Python → 创建 `.venv` 虚拟环境 → 安装依赖 → 启动服务 → 打开浏览器。

### macOS / Linux

```bash
chmod +x run.sh
./run.sh
```

### 手动启动

```bash
pip install -r requirements.txt
python app.py
```

浏览器访问 <http://127.0.0.1:8000>（若未自动打开）。

## 使用说明

1. 在番茄小说 App / 网页里复制书籍分享文案或链接，例如：
   `推荐一部好书《无人清白》https://changdunovel.com/t/ZPJkJpLOb-M/`
2. 粘贴到输入框，点击 **解析书籍**
3. 确认书籍信息后点击 **开始下载**
4. 下载完成后点击 **下载 TXT**，文件保存在 `downloads/` 目录

首次下载会自动注册一台匿名设备（缓存到 `.fanqie_device.json`），之后复用，不会每次都注册。

## 清理 / 卸载

一键清理脚本会把项目恢复到初始状态（只删运行时生成的内容，不动源码）：

- **Windows**：双击 `clean.bat`，输入 `Y` 确认
- **macOS / Linux**：`./clean.sh`，输入 `Y` 确认

清理内容：

| 内容 | 说明 |
| --- | --- |
| `.venv` / `venv` | 虚拟环境 |
| `__pycache__` / `*.pyc` | Python 字节码缓存 |
| `downloads/*.txt` | 已导出的 TXT 文件 |
| `.DS_Store` / `Thumbs.db` | 系统临时文件 |
| `.fanqie_device.json` | 匿名设备 / 正文密钥缓存 |

**完全清理**：清理完成后关闭窗口，直接删除整个项目文件夹即可。本工具不写注册表、不安装系统服务、不修改全局 Python，删除文件夹后系统无任何残留。

## 项目结构

```
fanqie-novel-downloader/
├── app.py                 # FastAPI 服务：解析 / SSE 进度 / 文件下载 / 静态页面
├── core/
│   ├── __init__.py
│   ├── parser.py          # 分享文案、短链、PC 链接 → book_id
│   ├── appapi.py          # APP 网关客户端：匿名设备 + 签名 + 正文解密（主力通道）
│   ├── decryptor.py       # awesome-font 私有区字体解密（网页兜底通道用）
│   └── crawler.py         # 元数据、目录、正文抓取与 TXT 导出
├── static/
│   └── index.html         # 单文件前端（Tailwind CDN + Vue3 CDN）
├── downloads/             # 导出的 TXT 文件
├── requirements.txt
├── run.bat                # Windows 一键启动
├── run.sh                 # Linux / macOS 一键启动
├── clean.bat              # Windows 一键清理
├── clean.sh               # Linux / macOS 一键清理
└── README.md
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/parse` | `{"url": "分享文案或链接"}` → 书籍元信息 |
| GET | `/api/download/progress?book_id=` | SSE 下载进度（`start` → `downloading`* → `done` / `error`） |
| GET | `/api/download/file/{filename}` | 下载导出的 TXT |
| GET | `/api/health` | 健康检查 |

## 环境变量（可选）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8000` | 服务端口 |
| `FANQIE_NO_BROWSER` | 未设置 | 设为 `1` 时不自动打开浏览器 |
| `FANQIE_COOKIE` | 未设置 | 番茄网页登录 Cookie（仅网页兜底通道使用，APP 通道不需要） |
| `FANQIE_APP_INTERVAL` | `6.0` | APP 通道相邻两批章节的间隔秒数，调小更快但更容易被限流 |
| `FANQIE_NO_APP_API` | 未设置 | 设为 `1` 时禁用 APP 通道，只走网页接口（只能拿到前 10 章，用于排查问题） |

## 技术原理

**主通道：番茄 APP 网关（免登录下载全书）**

1. `POST i.snssdk.com/service/2/device_register` 注册匿名设备：请求体经 TTEncrypt
   （gzip + SHA512 + AES-CBC）加密，拿到 `device_id` / `install_id`
2. `POST /reading/crypt/registerkey` 用静态密钥换正文解密密钥（AES-CBC）
3. `GET /reading/reader/batch_full/v` 批量取章节（每批最多 30 章），请求头带
   `x-argus` / `x-ladon` / `x-khronos` 签名：
   - `x-argus`：protobuf → SM3 派生 Simon-4-72 密钥加密 → 异或/倒序 → AES-CBC
   - `x-ladon`：SPECK-64 加密 `{ts}-{license_id}-{aid}`
4. 正文是 AES-CBC 加密的 XHTML，解密后即为**明文段落**（无字体混淆）

**兜底通道：网页接口**

1. 落地页接口返回书籍元数据 + 前 10 章明文正文
2. 目录接口拿到全书章节标题与 `itemId`
3. PC 阅读页正文被 `awesome-font` 把高频字重映射到 Unicode 私有区（PUA），
   `core/decryptor.py` 用预计算并逐字校验过的 362 项映射表还原明文
4. TXT 排版：书名/作者/评分/字数/状态/简介 + `【章节标题】` + 全角缩进段落

**限速策略**：APP 网关对同一设备有频率限制（连续请求会只回 1 章甚至空响应），
所以按 30 章/批、批间隔 6 秒串行请求；设备被作废时自动换新设备重试。

## 已知限制

- **APP 网关限速**：全本下载速度约 30 章 / 6 秒（350 章约 1 分钟），过快会被限流；
  若设备被作废会自动换新设备，连续失败时请稍后重试
- **匿名设备注册**：极少数网络环境可能注册失败，此时自动回退网页通道（只能拿到前 10 章），
  界面会明确提示原因
- **风控验证码**：网页兜底通道短时间大量请求会触发验证码中间页，工具会中止并保留已导出章节，
  等待几分钟后点击完成面板里的「重试失败章节」即可继续
- **字体更新**：若番茄更换网页字体导致新 PUA 字符无法识别，解密器会报告未覆盖字符，
  可通过 `register_font_map()` 增量补充映射（APP 通道不受影响）
- **APP 改版**：`x-argus` / `x-ladon` 签名与设备注册字段依赖番茄 APP 当前版本参数，
  番茄改版后可能失效；失效表现与处理见下一节

## 签名失效怎么办（重要）

APP 接口的签名算法（`x-argus` / `x-ladon` / TTEncrypt 设备注册）依赖番茄 APP 当前版本的固定参数，
**番茄改版后可能失效**。典型表现：

- 界面提示「APP 接口不可用（…）」并回退网页通道（只能拿到前 10 章）
- 报错「章节接口返回空响应」「正文密钥失效」「设备注册失败」
- 之前能下全本，突然只能下 10 章

处理步骤：

1. 先确认是不是临时风控：等几分钟重试，或把 `FANQIE_APP_INTERVAL` 调大（例如 `10`）再试
2. 仍失败则是签名参数过期，请到 GitHub 仓库提交 issue：
   <https://github.com/Jees996/fanqie-novel-downloader/issues/new/choose>
3. 提交时请附上（方便快速定位）：
   - 书籍 ID / 分享链接
   - 完整报错信息（界面提示或 `python core/appapi.py <book_id>` 的输出）
   - 运行环境：系统、Python 版本、是否重装过依赖
4. 如果你能抓包到新版 APP 的鉴权参数，欢迎直接提 PR 更新 `core/appapi.py` 顶部的常量

## 致谢

APP 网关签名与加解密算法参考了开源项目
[fanqie-assistant](https://github.com/naiyQAQ/fanqie-assistant) 的公开实现，在此致谢。

## 免责声明

本项目仅供个人学习与技术研究使用，请勿用于商业用途或大规模抓取。下载内容版权归原作者及平台所有，请支持正版。
