---
name: 签名失效 / APP 接口报错
about: 番茄改版导致 APP 网关签名失效，只能下载前 10 章
title: "[签名失效] "
labels: bug
assignees: ''
---

## 现象

- [ ] 界面提示「APP 接口不可用（…）」，回退网页通道后只能下载前 10 章
- [ ] 报错「章节接口返回空响应」
- [ ] 报错「正文密钥失效」
- [ ] 报错「设备注册失败」
- [ ] 其他（请在下方补充）

## 书籍信息

- 书籍 ID / 链接：
- 章节数（界面显示）：
- 实际下载到第几章：

## 报错信息

请粘贴界面提示或 `python core/appapi.py <book_id>` 的完整输出：

```text
（粘贴到这里）
```

## 运行环境

- 操作系统：
- Python 版本（`python --version`）：
- 是否重装过依赖（`pip install -r requirements.txt`）：
- 是否能正常解析书籍信息：

## 已尝试

- [ ] 等几分钟后重试
- [ ] 调大 `FANQIE_APP_INTERVAL`（例如 10）后重试
- [ ] 删除 `.fanqie_device.json` 后重试
- [ ] 换网络 / 关代理后重试

> 提示：如果是番茄改版导致签名参数过期，需要更新 `core/appapi.py` 顶部的常量。
> 如果你能抓包到新版 APP 的鉴权参数，欢迎直接提 PR。
