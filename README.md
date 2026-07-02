# json-formatter

一个单进程的 JSON / JSONL 查看器：Python 标准库写的 HTTP 服务，同时 serve 前端页面和采样 API，开一个端口就能用。

## 功能

- **前端 (`index.html`)**：粘贴 / 打开本地 `.json` `.jsonl` `.ndjson` 文件的可视化查看器
  - 展开 / 折叠、复制、全屏、明暗主题
  - 大 JSONL 分页浏览（首条 / 上下条 / 跳转）
  - 服务器路径加载 + 采样（head / tail / nth / range / random / all）
  - kv 过滤表达式：`a.b = 1, c ~= foo`
- **后端 (`server.py`)**：读服务进程有权限的本机文件，做流式采样
  - 只走标准库，无外部依赖
  - `/api/*` 路径必须是绝对路径；`.json` 单文件上限 512 MiB，`.jsonl` 逐行流式

## 运行

```bash
python3 server.py
# → 监听 0.0.0.0:8803，浏览器打开 http://<host>:8803/
```

用环境变量覆盖：

```bash
PORT=9000 python3 server.py
BIND=127.0.0.1 python3 server.py   # 只本机可达
```

需要长期跑，可以自己包一个 systemd unit（本仓库不带）。

## API

| Method | Path | 说明 |
|---|---|---|
| `GET`  | `/`               | 前端页面 |
| `GET`  | `/api/health`     | `{ok: true}` |
| `GET`  | `/api/stat?path=/abs/path` | `{ok, format, size, lines?, mtime}` |
| `POST` | `/api/sample`     | 采样，body 见下 |

`POST /api/sample` 请求体：

```json
{
  "path": "/abs/path/to/file.jsonl",
  "mode": "random",         // all | random | head | tail | nth | range
  "n": 20,                  // random/head/tail 条数
  "k": 1,                   // nth 的序号；range 起点
  "k2": 10,                 // range 终点
  "seed": "",               // random 可选种子
  "filter": {               // 可选
    "type": "kv",
    "expr": "a.b = 1, c ~= foo"
  }
}
```

响应：

```json
{ "ok": true, "records": [...], "scanned": 12345, "matched": 200, "format": "jsonl" }
```

kv 过滤支持 `= != > < >= <= ~= !~=`（`~=` 是正则），路径用 `foo.bar[0].baz`。

## 安全提示

服务读取的是**进程用户能读到的任意绝对路径**。默认绑 `0.0.0.0`，只在信任的网络里跑；要暴露公网请自己在前面加鉴权/反代。
