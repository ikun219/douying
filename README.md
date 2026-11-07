# 汐缘 (XiYuan) — 抖音私信机器人

抖音网页版私信自动化机器人，支持 AI 聊天、视频理解、语音回复、表情包管理。

## 功能

- **私信 AI 回复**：基于大语言模型的智能对话
- **多模态理解**：图片/视频内容识别与回复
- **语音消息**：TTS 语音合成 → 视频私信发送
- **表情包管理**：自动采集、去重、入库
- **风控处理**：自动识别并解除抖音风控验证

## 架构

```
reply_bot.py          # 主程序：私信监听 + 多线程回复
core/                 # 核心模块
  router.py           # 消息路由与回复逻辑
  tools.py            # 工具函数（日志、消息发送、视频/语音等）
  memory.py           # 对话记忆管理
  media.py            # 媒体处理
  poll.py             # 轮询相关
  ml_util.py          # ML 工具
  text_util.py        # 文本工具
dy_apis/              # 抖音 API 接口
  douyin_api.py       # 接口汇总
  douyin_recv_msg.py  # 私信接收
  login_api.py        # 登录相关
  _http.py            # HTTP 请求层
api/                  # 高层 API 封装
builder/              # 请求构造器（签名/Header/Protobuf）
utils/                # 通用工具
```

## 环境要求

- Python 3.10+
- Playwright（Chromium 浏览器自动化）
- FFmpeg（语音合成视频编码）
- [AstrBot](https://github.com/Soulter/AstrBot)（可选，AI 对话后端）

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/ikun219/douying.git
cd douying

# 2. 安装依赖
pip install -r requirements.txt
playwright install chromium

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 API 密钥

# 4. 配置抖音 Cookie
# 需要先通过浏览器登录抖音，获取 Cookie

# 5. 启动
python main.py
```

## 配置

### 环境变量 (.env)

| 变量 | 说明 |
|------|------|
| `DEEPSEEK_KEY` | DeepSeek API 密钥 |
| `DASHSCOPE_KEY` | 阿里百炼 API 密钥 |
| `SILICONFLOW_KEY` | SiliconFlow API 密钥 |
| `FFMPEG` | FFmpeg 可执行文件路径（可选，默认自动查找） |

### 其他配置文件

- `mood.json` — 心情词条（每行一条）
- `persona.txt` — 人设文本
- `share_targets.json` — 分享目标白名单

## 许可证

MIT License