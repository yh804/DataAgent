# 部署说明

本项目的「模型连接」支持两种模式,优先级固定为 **请求头 > 服务端 .env**:

| 模式 | 适用场景 | 密钥存放位置 |
|---|---|---|
| BYOK(使用者自带) | 公网 / 多人使用 | 浏览器 `localStorage`,服务端不落盘 |
| 自托管(服务端 .env) | 本机 / 内网单用户 | 服务端 `.env`,启动时读入内存 |

> 前端「⚙️ 模型设置」里填写的 Key / Base URL / Model 会随每个请求通过
> `X-LLM-Key` / `X-LLM-Base` / `X-LLM-Model` 请求头发送;服务端只写 ContextVar,
> 请求结束立即重置,不写日志、不落盘。`.env` 只在请求头缺省时作为兜底。

---

## 本地运行

```bash
cp .env.example .env      # 填入 LLM_API_KEY(自托管默认值)
python web/run.py         # http://127.0.0.1:8000
```

- 只想用自己的 Key:`.env` 里可以留空 `LLM_API_KEY`,启动后在页面「⚙️ 模型设置」里填写,
  点「测试连接」确认可用后再提问。
- 页面启动时会请求 `GET /api/health`:若本机未填 Key 且服务端也没有 Key,
  顶部会出现黄色横幅「请先在 ⚙️ 模型设置里填入 API Key」,发送按钮同时禁用。

---

## VPS / 云主机(Docker Compose)

沙箱依赖宿主机的 Docker,容器内必须挂载 Docker socket:

```yaml
services:
  app:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./outputs:/app/outputs
      - ./data:/app/data
    env_file:
      - .env
```

> **风险提示**:挂载 `/var/run/docker.sock` 等于把宿主机 Docker 的 root 权限交给容器。
> 容器内的进程可以创建特权容器、挂载宿主机根目录。仅在可信环境这样做;
> 公网部署建议把沙箱后端切到 E2B,而不是暴露 Docker socket。

沙箱后端切换:

```bash
SANDBOX_BACKEND=e2b
E2B_API_KEY=e2b_xxx
```

---

## 平台限制

| 平台 | 能否运行 | 说明 |
|---|---|---|
| 本机 / VPS | ✅ | 需要 Docker,可用 Docker 沙箱 |
| Render / Railway | ❌ 完整功能 | 不提供 Docker-in-Docker,沙箱无法启动;只能看到静态页面,提问会失败 |
| GitHub Pages | ❌ | 纯静态托管,没有后端 API,无法运行 Agent |
| 支持 E2B 的容器平台 | ✅(切后端) | 设置 `SANDBOX_BACKEND=e2b`,把代码执行交给 E2B |

> 在 Render / Railway 上如果只需要展示界面,可以只部署 `web/static/` 的静态页,
> 但对话、上传、分析都不可用。

---

## 公网部署建议

1. **强制 BYOK**:清空 `.env` 里的 `LLM_API_KEY`,让每个使用者填写自己的 Key。
   否则任何人打开页面都能消耗服务端额度。
2. **限流**:在反向代理(nginx / Caddy)或应用层按 IP 限流,例如每分钟 N 次,
   防止服务端 Key 或 E2B 额度被刷。
3. **不要暴露 Docker socket**:公网环境优先 E2B;必须用 Docker 时,限制容器权限并把
   Agent 放在内网,仅通过反向代理暴露 Web 端口。
4. **关闭不需要的写盘**:`outputs/` 会保存运行归档与会话记录;公网部署应定期清理,
   并确认 `sessions.jsonl` / `traces/` 中不会出现密钥(本项目已在写入前统一脱敏)。

---

## 密钥安全清单

- 浏览器:Key 存在 `localStorage.llmSettings`,点「清除」即删除。
- 服务端:只从请求头读入 `ContextVar`,请求结束重置;不写 `.env`、不写 `outputs/`、不写日志。
- 日志 / trace / `index.jsonl` / `sessions.jsonl` / `approvals.jsonl` 落盘前统一用
  `sk-[A-Za-z0-9]{8,}` → `sk-***` 脱敏。
- `POST /api/llm/test` 失败只返回可读原因,不回显 Key。
