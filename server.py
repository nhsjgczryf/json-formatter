#!/usr/bin/env python3
"""
json-formatter API: read-only 本机 JSON/JSONL 文件采样服务。

GET  /api/stat?path=/abs/path       → {ok, format, size, lines?, mtime}
POST /api/sample  {path, mode, n, k, k2, seed, filter?}
                                     → {ok, records, scanned, matched}

只监听 127.0.0.1:8802，由 nginx 反代 /api/。
用户就是登录 shell 里的 claudecode，读文件权限 = 该用户。
"""
import json
import os
import random
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = 8803
BIND = "127.0.0.1"

MAX_RECORDS = 20000            # 单次响应最多条数
MAX_JSON_FILE = 512 * 1024 * 1024   # 单文件 .json 上限 (无法流式)
MAX_STAT_LINE_SCAN = 8 * 1024 * 1024 * 1024  # stat 时逐行计数的字节上限


def detect_format(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".jsonl", ".ndjson"):
        return "jsonl"
    if ext == ".json":
        return "json"
    # sniff: 读前几 KB
    try:
        with path.open("rb") as f:
            head = f.read(4096)
    except Exception:
        return "unknown"
    s = head.lstrip().decode("utf-8", errors="replace")
    if s.startswith("[") or s.startswith("{"):
        # could still be jsonl of objects
        # heuristic: if there's an object closer followed by \n{ within first 4KB, treat as jsonl
        if re.search(r"[}\]]\s*\n\s*[\[{]", s):
            return "jsonl"
        return "json"
    return "jsonl"


def count_lines(path: Path) -> int:
    n = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            n += chunk.count(b"\n")
    return n


def parse_kv_conditions(expr: str):
    conds = []
    for c in re.split(r"\s*,\s*|\s+&&\s+", expr):
        c = c.strip()
        if not c:
            continue
        m = re.match(r"^(.+?)\s*(!~=|~=|!=|>=|<=|=|>|<)\s*(.*)$", c)
        if not m:
            raise ValueError(f"无法解析条件: {c}")
        path, op, val = m.group(1).strip(), m.group(2), m.group(3).strip()
        try:
            parsed = json.loads(val) if val else ""
        except Exception:
            parsed = val
        conds.append((path, op, parsed))
    return conds


def get_dotted(obj, path):
    # Support foo.bar[0].baz
    parts = re.sub(r"\[(-?\d+)\]", r".\1", path).lstrip(".").split(".")
    cur = obj
    for p in parts:
        if p == "":
            continue
        if cur is None:
            return None
        try:
            if re.fullmatch(r"-?\d+", p):
                idx = int(p)
                cur = cur[idx]
            else:
                cur = cur.get(p) if isinstance(cur, dict) else None
        except Exception:
            return None
    return cur


def eval_kv(rec, conds):
    for path, op, cv in conds:
        v = get_dotted(rec, path)
        ok = False
        try:
            if op == "=":
                ok = (v == cv) or (str(v) == str(cv))
            elif op == "!=":
                ok = not ((v == cv) or (str(v) == str(cv)))
            elif op == "~=":
                ok = bool(re.search(str(cv), "" if v is None else str(v)))
            elif op == "!~=":
                ok = not bool(re.search(str(cv), "" if v is None else str(v)))
            elif op == ">":
                ok = float(v) > float(cv)
            elif op == "<":
                ok = float(v) < float(cv)
            elif op == ">=":
                ok = float(v) >= float(cv)
            elif op == "<=":
                ok = float(v) <= float(cv)
        except Exception:
            ok = False
        if not ok:
            return False
    return True


def iter_records(path: Path, fmt: str):
    """Yield (index, record) starting at index 0. Skips blank/bad JSONL lines."""
    if fmt == "jsonl":
        with path.open("r", encoding="utf-8", errors="replace") as f:
            i = 0
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    yield i, json.loads(s)
                    i += 1
                except Exception:
                    i += 1
                    continue
    else:  # json
        with path.open("r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        if isinstance(data, list):
            for i, r in enumerate(data):
                yield i, r
        else:
            yield 0, data


def sample_from_file(path: Path, fmt: str, mode: str, n: int, k: int, k2: int,
                     seed: str, filter_spec):
    """Return (records, scanned, matched)."""
    conds = None
    if filter_spec and filter_spec.get("type") == "kv":
        conds = parse_kv_conditions(filter_spec.get("expr", ""))

    rng = random.Random(seed) if seed else random.Random()
    n = max(1, min(int(n or 20), MAX_RECORDS))

    if mode == "all":
        out, scanned, matched = [], 0, 0
        for _, rec in iter_records(path, fmt):
            scanned += 1
            if conds and not eval_kv(rec, conds):
                continue
            matched += 1
            if len(out) < MAX_RECORDS:
                out.append(rec)
        return out, scanned, matched

    if mode == "head":
        out, scanned, matched = [], 0, 0
        for _, rec in iter_records(path, fmt):
            scanned += 1
            if conds and not eval_kv(rec, conds):
                continue
            matched += 1
            out.append(rec)
            if len(out) >= n:
                break
        return out, scanned, matched

    if mode == "tail":
        from collections import deque
        buf = deque(maxlen=n)
        scanned, matched = 0, 0
        for _, rec in iter_records(path, fmt):
            scanned += 1
            if conds and not eval_kv(rec, conds):
                continue
            matched += 1
            buf.append(rec)
        return list(buf), scanned, matched

    if mode == "nth":
        target = max(1, int(k or 1))
        scanned, matched = 0, 0
        for _, rec in iter_records(path, fmt):
            scanned += 1
            if conds and not eval_kv(rec, conds):
                continue
            matched += 1
            if matched == target:
                return [rec], scanned, matched
        return [], scanned, matched

    if mode == "range":
        a = max(1, int(k or 1))
        b = max(a, int(k2 or a))
        out, scanned, matched = [], 0, 0
        for _, rec in iter_records(path, fmt):
            scanned += 1
            if conds and not eval_kv(rec, conds):
                continue
            matched += 1
            if a <= matched <= b:
                out.append(rec)
                if matched >= b and len(out) >= MAX_RECORDS:
                    break
            if matched > b:
                break
        return out, scanned, matched

    # random: reservoir sampling
    reservoir = []
    scanned, matched = 0, 0
    for _, rec in iter_records(path, fmt):
        scanned += 1
        if conds and not eval_kv(rec, conds):
            continue
        matched += 1
        if len(reservoir) < n:
            reservoir.append(rec)
        else:
            j = rng.randint(0, matched - 1)
            if j < n:
                reservoir[j] = rec
    return reservoir, scanned, matched


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s - %s\n" % (self.log_date_time_string(), self.address_string(), fmt % args))

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, status=400):
        self._send_json(status, {"ok": False, "error": msg})

    def _resolve_path(self, raw):
        raw = (raw or "").strip()
        if not raw:
            return None, "empty path"
        if not raw.startswith("/"):
            return None, "path must be absolute"
        p = Path(raw).expanduser()
        try:
            p = p.resolve(strict=True)
        except FileNotFoundError:
            return None, "file not found"
        except Exception as e:
            return None, f"resolve failed: {e}"
        if not p.is_file():
            return None, "not a regular file"
        # Read permission check
        if not os.access(p, os.R_OK):
            return None, "not readable"
        return p, None

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/stat":
            qs = parse_qs(u.query)
            raw = qs.get("path", [""])[0]
            p, err = self._resolve_path(raw)
            if err:
                return self._err(err)
            try:
                st = p.stat()
            except Exception as e:
                return self._err(str(e))
            fmt = detect_format(p)
            lines = None
            if fmt == "jsonl" and st.st_size <= MAX_STAT_LINE_SCAN:
                try:
                    lines = count_lines(p)
                except Exception:
                    lines = None
            return self._send_json(200, {
                "ok": True,
                "format": fmt,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "lines": lines,
            })
        if u.path == "/api/health":
            return self._send_json(200, {"ok": True})
        return self._err("not found", 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/sample":
            return self._err("not found", 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b"{}"
            req = json.loads(body.decode("utf-8"))
        except Exception as e:
            return self._err(f"bad json: {e}")

        p, err = self._resolve_path(req.get("path"))
        if err:
            return self._err(err)
        fmt = detect_format(p)
        if fmt == "json" and p.stat().st_size > MAX_JSON_FILE:
            return self._err(f".json 文件过大 ({p.stat().st_size} B > {MAX_JSON_FILE})，请转成 .jsonl")
        mode = req.get("mode", "random")
        if mode not in ("all", "random", "head", "tail", "nth", "range"):
            return self._err(f"bad mode: {mode}")

        try:
            recs, scanned, matched = sample_from_file(
                p, fmt, mode,
                req.get("n", 20), req.get("k", 1), req.get("k2", 10),
                req.get("seed", ""), req.get("filter"),
            )
        except ValueError as e:
            return self._err(str(e))
        except MemoryError:
            return self._err("out of memory (文件太大)")
        except Exception as e:
            return self._err(f"sample failed: {e}")

        return self._send_json(200, {
            "ok": True,
            "records": recs,
            "scanned": scanned,
            "matched": matched,
            "format": fmt,
        })


def main():
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    sys.stderr.write(f"[json-formatter-api] listening on {BIND}:{PORT}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
