// server.js
// =====================================================================
// Nemotron Scratch Inspector · web app on port 8082
// ---------------------------------------------------------------------
// 路由：
//   GET  /                          聊天 UI（仿 dimond.top:18097 风格）
//   GET  /static/*                  前端静态资源（app.js / app.css）
//   GET  /health                    健康检查（返回 OK + 模型状态）
//   GET  /ask/claude?q=...          兼容已有接口（透传到 run_claude.js）
//   GET  /nemtron?q=&img=           划痕检测：q 是文本提示，img 是图片
//                                   base64 / dataURL / 远程 URL
//   POST /nemtron   {q, img}        同上，JSON body
//   响应：multipart/mixed，前半段 JSON，后半段 PNG（也可用 ?format=json 单独取 JSON）
// ---------------------------------------------------------------------
const http = require('http');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { URL } = require('url');

const PORT = parseInt(process.env.PORT || '8082', 10);
const WORKSPACE = process.env.WORKSPACE_DIR || '/home/agent/.claude/workspace/project';
const PYTHON_BIN = process.env.PYTHON_BIN || 'python3';
const NEMOTRON_SCRIPT = path.join(WORKSPACE, 'nemtron_worker.py');
const RUN_CLAUDE_JS = path.join(WORKSPACE, 'run_claude.js');
const TIMEOUT_MS = (parseInt(process.env.NEMOTRON_TIMEOUT_S || '600', 10)) * 1000;
const LOG_FILE = path.join(WORKSPACE, 'logs', 'run.log');
const FRONT_LOG = path.join(WORKSPACE, 'logs', 'agent_tui.log');

function logLine(...parts) {
    const line = `[${new Date().toISOString()}] ${parts.join(' ')}\n`;
    try { fs.appendFileSync(LOG_FILE, line); } catch (_) {}
    try { process.stdout.write(line); } catch (_) {}
}

// -------------------- MIME --------------------
const MIME = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon',
};

// -------------------- 工具函数 --------------------
function sendJson(res, code, obj) {
    const body = JSON.stringify(obj);
    res.writeHead(code, {
        'Content-Type': 'application/json; charset=utf-8',
        'Content-Length': Buffer.byteLength(body),
        'Cache-Control': 'no-store',
    });
    res.end(body);
}

function sendText(res, code, text, ctype) {
    res.writeHead(code, {
        'Content-Type': (ctype || 'text/plain; charset=utf-8'),
        'Content-Length': Buffer.byteLength(text),
        'Cache-Control': 'no-store',
    });
    res.end(text);
}

// 解析 GET/POST 通用入参
function readPayload(req) {
    return new Promise((resolve, reject) => {
        const url = new URL(req.url, `http://localhost:${PORT}`);
        // query 参数 — Node URL 会把 + 解析成空格，base64 还原需要 + 号
        const rawImg = url.searchParams.get('img') || '';
        const fixedImg = rawImg.replace(/ /g, '+');
        const q = {
            q: url.searchParams.get('q') || '',
            img: fixedImg,
            format: (url.searchParams.get('format') || '').toLowerCase(),
        };
        if (req.method === 'GET' || req.method === 'HEAD') return resolve({ ...q, url });

        const ctype = (req.headers['content-type'] || '').toLowerCase();
        const chunks = [];
        let size = 0;
        req.on('data', (c) => {
            size += c.length;
            if (size > 16 * 1024 * 1024) {
                req.destroy();
                return reject(new Error('Payload too large (>16MB)'));
            }
            chunks.push(c);
        });
        req.on('end', () => {
            const raw = Buffer.concat(chunks).toString('utf8');
            if (!raw) return resolve({ ...q, url });
            try {
                if (ctype.includes('application/json')) {
                    const obj = JSON.parse(raw);
                    resolve({
                        q: obj.q || q.q,
                        img: obj.img || q.img,
                        format: obj.format || q.format,
                        url,
                    });
                } else if (ctype.includes('application/x-www-form-urlencoded')) {
                    const p = new URLSearchParams(raw);
                    resolve({
                        q: p.get('q') || q.q,
                        img: p.get('img') || q.img,
                        format: p.get('format') || q.format,
                        url,
                    });
                } else {
                    // 兜底当作文本
                    resolve({ q: q.q || raw, img: q.img, format: q.format, url });
                }
            } catch (e) {
                reject(new Error('Invalid request body: ' + e.message));
            }
        });
        req.on('error', reject);
    });
}

// 把 dataURL / 远程 URL 清洗成纯 base64 字符串，传给 Python 进一步解码
function cleanImgInput(s) {
    if (!s) return '';
    s = String(s).trim();
    if (s.startsWith('http://') || s.startsWith('https://')) {
        return s; // 让 Python worker 自己去下载
    }
    const i = s.indexOf('base64,');
    if (i >= 0) return s.slice(i + 7);
    return s;
}

// -------------------- Python Nemotron worker --------------------
function callNemtronWorker(payload) {
    return new Promise((resolve, reject) => {
        const child = spawn(PYTHON_BIN, [NEMOTRON_SCRIPT], {
            cwd: WORKSPACE,
            env: {
                ...process.env,
                NEMOTRON_MODEL: process.env.NEMOTRON_MODEL || 'nvidia/Nemotron-3-Embed-1B-BF16',
                NEMOTRON_DEVICE: process.env.NEMOTRON_DEVICE || 'cpu',
                HF_ENDPOINT: process.env.HF_ENDPOINT || 'https://hf-mirror.com',
            },
            stdio: ['pipe', 'pipe', 'pipe'],
        });

        let stdout = Buffer.alloc(0);
        let stderr = '';
        let killed = false;

        const timer = setTimeout(() => {
            killed = true;
            try { child.kill('SIGTERM'); } catch (_) {}
            setTimeout(() => { try { child.kill('SIGKILL'); } catch (_) {} }, 3000);
        }, TIMEOUT_MS);

        child.stdout.on('data', (d) => { stdout = Buffer.concat([stdout, d]); });
        child.stderr.on('data', (d) => { stderr += d.toString(); });

        child.on('error', (e) => {
            clearTimeout(timer);
            reject(new Error('spawn python failed: ' + e.message));
        });
        child.on('close', (code) => {
            clearTimeout(timer);
            if (killed) return reject(new Error(`Nemotron worker timeout after ${TIMEOUT_MS/1000}s`));

            // Python 输出约定：第一行是 JSON 头（base64 PNG + meta），其后是 PNG 二进制（可选）
            // 简化协议：worker 把所有结果都用 JSON 写 stdout，PNG 用 base64 嵌在 JSON 中
            // 这样 Node 端解析最稳。
            let jsonText = stdout.toString('utf8');
            // 容忍 stderr 噪声
            const jsonStart = jsonText.indexOf('{');
            if (jsonStart > 0) jsonText = jsonText.slice(jsonStart);

            try {
                const obj = JSON.parse(jsonText);
                if (code !== 0 && obj.error === undefined) {
                    obj.error = `python exit ${code}: ${stderr.split('\n').slice(-3).join(' | ')}`;
                }
                resolve(obj);
            } catch (e) {
                reject(new Error(`worker output parse failed: ${e.message}; head=${jsonText.slice(0,200)}`));
            }
        });

        // 用 stdin 传 JSON 请求（避开 ARG_MAX）
        child.stdin.write(JSON.stringify(payload));
        child.stdin.end();
    });
}

// 复用 run_claude.js 的 /ask/claude，保持项目原有能力不退化
function callRunClaude(question) {
    return new Promise((resolve, reject) => {
        const msgB64 = Buffer.from(question, 'utf8').toString('base64');
        const child = spawn('node', [RUN_CLAUDE_JS], {
            cwd: WORKSPACE,
            stdio: ['ignore', 'pipe', 'pipe'],
            env: {
                ...process.env,
                ANTHROPIC_DISABLE_PREFLIGHT: '1',
                CLAUDE_CAPTURE_STDIO: '1',
                CLAUDE_MSG: msgB64,
            },
        });
        let out = '';
        let err = '';
        child.stdout.on('data', (d) => { out += d.toString(); });
        child.stderr.on('data', (d) => { err += d.toString(); });
        child.on('close', (code) => {
            if (code === 0) resolve(out.trim());
            else reject(new Error(err || `run_claude exit ${code}`));
        });
        child.on('error', reject);
    });
}

// -------------------- 静态资源 --------------------
function serveStatic(req, res, rel) {
    const safe = path.normalize(rel).replace(/^\/+/, '');
    const fp = path.join(WORKSPACE, 'public', safe);
    if (!fp.startsWith(path.join(WORKSPACE, 'public'))) {
        return sendText(res, 403, 'Forbidden');
    }
    fs.readFile(fp, (err, data) => {
        if (err) return sendText(res, 404, 'Not Found');
        const ext = path.extname(fp).toLowerCase();
        res.writeHead(200, {
            'Content-Type': MIME[ext] || 'application/octet-stream',
            'Content-Length': data.length,
            'Cache-Control': 'no-store',
        });
        res.end(data);
    });
}

// -------------------- 路由 --------------------
const server = http.createServer(async (req, res) => {
    let url;
    try { url = new URL(req.url, `http://localhost:${PORT}`); }
    catch (_) { return sendText(res, 400, 'Bad URL'); }

    const pathname = url.pathname;

    try {
        // 健康检查
        if (req.method === 'GET' && pathname === '/health') {
            return sendJson(res, 200, {
                ok: true,
                port: PORT,
                service: 'nemotron-scratch-inspector',
                model: process.env.NEMOTRON_MODEL || 'nvidia/Nemotron-3-Embed-1B-BF16',
                device: process.env.NEMOTRON_DEVICE || 'cpu',
                endpoints: ['/', '/health', '/ask/claude', '/nemtron'],
                time: new Date().toISOString(),
            });
        }

        // 兼容 /ask/claude（透传到 run_claude.js）
        if ((req.method === 'GET' || req.method === 'POST') && pathname === '/ask/claude') {
            const p = await readPayload(req);
            const q = (p.q || '').trim();
            if (!q) return sendText(res, 400, 'Missing q parameter');
            try {
                const text = await callRunClaude(q);
                return sendText(res, 200, text);
            } catch (e) {
                return sendText(res, 500, e.message);
            }
        }

        // 主路由：/nemtron
        if ((req.method === 'GET' || req.method === 'POST') && pathname === '/nemtron') {
            const p = await readPayload(req);
            const q = (p.q || '').trim();
            const img = cleanImgInput(p.img);
            if (!q) return sendJson(res, 400, { error: 'Missing q (text prompt)' });
            if (!img) return sendJson(res, 400, { error: 'Missing img (image base64 or URL)' });

            logLine('NEMTRON', 'q.len=' + q.length, 'img.len=' + img.length);

            const payload = { q, img };
            const result = await callNemtronWorker(payload);
            if (!result.ok) {
                return sendJson(res, 500, { ok: false, error: result.error || 'unknown', meta: result.meta || null });
            }

            // 三种返回格式：
            //   POST               → JSON
            //   GET ?format=json   → JSON
            //   GET 默认           → multipart/mixed（JSON + PNG）
            const accept = (req.headers.accept || '').toLowerCase();
            const fmt = (p.format || '').toLowerCase();
            const wantJson = (fmt === 'json') || (req.method === 'POST') || accept.includes('application/json');
            if (wantJson) {
                return sendJson(res, 200, result);
            }
            if (result.png_base64) {
                const png = Buffer.from(result.png_base64, 'base64');
                const js = Buffer.from(JSON.stringify(result), 'utf8');
                const boundary = '----NemtronBoundary' + Date.now().toString(36);
                const parts = [
                    Buffer.from(`--${boundary}\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: ${js.length}\r\n\r\n`),
                    js,
                    Buffer.from(`\r\n--${boundary}\r\nContent-Type: image/png\r\nContent-Length: ${png.length}\r\n\r\n`),
                    png,
                    Buffer.from(`\r\n--${boundary}--\r\n`),
                ];
                res.writeHead(200, {
                    'Content-Type': `multipart/mixed; boundary=${boundary}`,
                    'Content-Length': parts.reduce((a, b) => a + b.length, 0),
                    'Cache-Control': 'no-store',
                });
                for (const p of parts) res.write(p);
                res.end();
                return;
            }
            return sendJson(res, 200, result);
        }

        // 聊天 UI 首页
        if (req.method === 'GET' && (pathname === '/' || pathname === '/index.html')) {
            return serveStatic(req, res, 'index.html');
        }

        // 静态资源
        if (req.method === 'GET' && pathname.startsWith('/static/')) {
            return serveStatic(req, res, pathname.replace('/static/', ''));
        }

        return sendText(res, 404, 'Not Found');
    } catch (e) {
        logLine('ERROR', e.stack || e.message);
        if (!res.headersSent) return sendJson(res, 500, { ok: false, error: e.message });
    }
});

// 多客户端断开时回收 child 进程
server.on('connection', (sock) => {
    sock.on('close', () => {});
});

server.listen(PORT, '0.0.0.0', () => {
    logLine(`Nemotron Scratch Inspector listening on http://0.0.0.0:${PORT}`);
});
