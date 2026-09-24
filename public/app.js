// public/app.js
// Nemotron Scratch Inspector 前端
// - 文本 + 图片 → POST /nemtron
// - 返回 JSON 里含 png_base64（标注后 PNG），前端直接渲染 + 列区域 JSON
(function () {
    'use strict';

    const chatEl   = document.getElementById('chat');
    const inputEl  = document.getElementById('input');
    const sendBtn  = document.getElementById('btnSend');
    const uploadBtn= document.getElementById('btnUpload');
    const fileInput= document.getElementById('filePicker');
    const previewEl= document.getElementById('attachPreview');
    const thumbEl  = document.getElementById('attachThumb');
    const nameEl   = document.getElementById('attachName');
    const rmBtn    = document.getElementById('btnRemoveAttach');
    const clearBtn = document.getElementById('btnClear');
    const sampleBtn= document.getElementById('btnSample');
    const statusEl = document.getElementById('status');

    const LS_KEY = 'nemtron_history_v1';
    const MAX_HIST = 30;
    const MAX_IMG_BYTES = 8 * 1024 * 1024;
    const DEFAULT_PROMPT = '请检测图片中的划痕';

    // 前端去重（同 text 3s 内不重发）
    const _dedupeMap = new Map();
    function dedupeHit(text, ttlMs) {
        const last = _dedupeMap.get(text);
        return last != null && (Date.now() - last) < ttlMs;
    }
    function markSent(text) { _dedupeMap.set(text, Date.now()); }

    let history = [];
    let pendingAttach = null;
    let inFlight = false;

    function setStatus(msg, kind) {
        statusEl.textContent = msg || '';
        statusEl.classList.remove('error', 'ok');
        if (kind === 'err') statusEl.classList.add('error');
        else if (kind === 'ok') statusEl.classList.add('ok');
    }

    function loadHistory() {
        try {
            const arr = JSON.parse(localStorage.getItem(LS_KEY) || '[]');
            return Array.isArray(arr) ? arr : [];
        } catch (_) { return []; }
    }
    function saveHistory() {
        try {
            localStorage.setItem(LS_KEY, JSON.stringify(history.slice(-MAX_HIST)));
        } catch (_) {}
    }

    function readFileAsDataURL(file) {
        return new Promise((resolve, reject) => {
            const fr = new FileReader();
            fr.onload = () => resolve(String(fr.result || ''));
            fr.onerror = () => reject(fr.error || new Error('FileReader error'));
            fr.readAsDataURL(file);
        });
    }

    async function setAttachment(dataUrl, name) {
        if (!dataUrl) {
            pendingAttach = null;
            previewEl.hidden = true;
            thumbEl.src = '';
            nameEl.textContent = '';
            return;
        }
        const c = dataUrl.indexOf(',');
        const approxBytes = Math.ceil((dataUrl.length - c - 1) * 0.75);
        if (approxBytes > MAX_IMG_BYTES) {
            setStatus('图片过大（>8MB），请压缩后再试', 'err');
            return;
        }
        pendingAttach = { dataUrl, name: name || 'pasted' };
        thumbEl.src = dataUrl;
        nameEl.textContent = name || 'pasted';
        previewEl.hidden = false;
        setStatus('');
    }

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
    }

    function renderEmpty() {
        chatEl.innerHTML = `
            <div class="empty-hint">
              <div class="hint-title">开始与 Nemotron Inspector 对话</div>
              <div>输入文字描述并附上图片，AI 会判断检测意图并标注图中的疑似缺陷。</div>
              <ul>
                <li>回车发送，Shift+Enter 换行</li>
                <li>支持粘贴图片 / 点击 📎 上传 / 点击 🖼 演示图</li>
                <li>返回 JSON 含检测区域与标注图（base64 PNG）</li>
              </ul>
            </div>`;
    }

    function renderAll() {
        if (!history.length) { renderEmpty(); return; }
        chatEl.innerHTML = '';
        for (const m of history) appendMsgToDOM(m);
        scrollToBottom();
    }

    function appendMsgToDOM(m) {
        const div = document.createElement('div');
        div.className = 'msg ' + (m.role === 'user' ? 'user' : 'assistant');
        const avatar = m.role === 'user' ? '你' : '🔬';
        const imagesHtml = (m.images && m.images.length)
            ? `<div class="images">${m.images.map(s => `<img src="${s}" alt="img">`).join('')}</div>` : '';
        const text = m.text ? escapeHtml(m.text) : '';
        const resultImg = m.resultImg ? `<img class="result-img" src="${m.resultImg}" alt="annotated">` : '';
        const regionsHtml = (m.regions && m.regions.length)
            ? `<pre class="regions">${escapeHtml(JSON.stringify(m.regions, null, 2))}</pre>` : '';
        const summary = m.summary
            ? `<div class="summary">${escapeHtml(m.summary)}</div>` : '';
        div.innerHTML = `
            <div class="avatar" aria-hidden="true">${avatar}</div>
            <div>
              <div class="bubble">${text}${imagesHtml}</div>
              ${resultImg}
              ${summary}
              ${regionsHtml}
            </div>`;
        chatEl.appendChild(div);
        return div;
    }

    function scrollToBottom() {
        requestAnimationFrame(() => { chatEl.scrollTop = chatEl.scrollHeight; });
    }

    async function send() {
        if (inFlight) return;
        const text = (inputEl.value || '').trim();
        const imgDataUrl = pendingAttach ? pendingAttach.dataUrl : null;
        const effectiveText = text || DEFAULT_PROMPT;
        if (!text && !imgDataUrl) {
            setStatus('请输入文字或附上图片再发送…', 'err');
            return;
        }

        if (dedupeHit(effectiveText + '|' + (imgDataUrl ? imgDataUrl.length : 0), 3000)) {
            setStatus('3s 内已发过同条请求，已忽略…', 'err');
            return;
        }
        markSent(effectiveText + '|' + (imgDataUrl ? imgDataUrl.length : 0));

        inFlight = true;
        sendBtn.disabled = true;
        uploadBtn.disabled = true;

        const userMsg = {
            role: 'user',
            text: effectiveText,
            images: imgDataUrl ? [imgDataUrl] : [],
            ts: Date.now(),
        };
        history.push(userMsg);
        appendMsgToDOM(userMsg);

        const pendingBot = { role: 'assistant', text: 'Nemotron 正在分析…', ts: Date.now() };
        const pendingNode = appendMsgToDOM(pendingBot);
        pendingNode.classList.add('pending');

        inputEl.value = '';
        await setAttachment(null);
        scrollToBottom();
        saveHistory();

        let imgBase64 = null;
        if (imgDataUrl) {
            const c = imgDataUrl.indexOf(',');
            imgBase64 = c >= 0 ? imgDataUrl.slice(c + 1) : imgDataUrl;
        }

        try {
            setStatus('请求中… Nemotron CPU 推理可能需要几秒到十几秒');
            const t0 = performance.now();
            const r = await fetch('/nemtron', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
                body: JSON.stringify({ q: effectiveText, img: imgBase64 }),
            });
            const data = await r.json().catch(() => ({}));
            const dt = ((performance.now() - t0) / 1000).toFixed(1);
            if (!r.ok || !data.ok) {
                throw new Error(data && data.error ? data.error : ('HTTP ' + r.status));
            }
            pendingNode.remove();

            const intent = data.intent || 'unknown';
            const score = (data.score || 0).toFixed(2);
            const total = (data.summary && data.summary.total) || (data.regions || []).length;
            const sumTpl = `intent=<b>${escapeHtml(intent)}</b> · text-score=${score} · regions=${total} · ${dt}s`;
            const resultImg = data.png_base64 ? `data:image/png;base64,${data.png_base64}` : '';
            const regions = data.regions || [];

            const botMsg = {
                role: 'assistant',
                text: `✓ 检测完成 · 模型：${escapeHtml(data.embed_model || '')}\n` +
                      `意图=${intent}  文本置信度=${score}  区域=${total}`,
                resultImg,
                regions,
                summary: sumTpl,
                ts: Date.now(),
            };
            history.push(botMsg);
            appendMsgToDOM(botMsg);
            scrollToBottom();
            saveHistory();
            setStatus(`完成，${dt}s · ${total} 个候选区域`, 'ok');
        } catch (e) {
            pendingNode.remove();
            const errMsg = '⚠ ' + (e && e.message ? e.message : String(e));
            const div = document.createElement('div');
            div.className = 'msg assistant error';
            div.innerHTML = `<div class="avatar" aria-hidden="true">🔬</div>
                <div><div class="bubble">${escapeHtml(errMsg)}</div></div>`;
            chatEl.appendChild(div);
            history.push({ role: 'assistant', text: errMsg, ts: Date.now() });
            saveHistory();
            scrollToBottom();
            setStatus('请求失败：' + (e && e.message ? e.message : e), 'err');
        } finally {
            inFlight = false;
            sendBtn.disabled = false;
            uploadBtn.disabled = false;
        }
    }

    function clearAll() {
        if (!confirm('清空当前对话历史？')) return;
        history = [];
        try { localStorage.removeItem(LS_KEY); } catch (_) {}
        renderEmpty();
        setStatus('已清空', 'ok');
    }

    // 生成一张带划痕的演示图（200x150 灰度 + 几条对角线）
    async function loadSampleImage() {
        const c = document.createElement('canvas');
        c.width = 480; c.height = 320;
        const ctx = c.getContext('2d');
        // 底色：浅灰金属感
        const grd = ctx.createLinearGradient(0, 0, 480, 320);
        grd.addColorStop(0, '#cbd5e1'); grd.addColorStop(1, '#94a3b8');
        ctx.fillStyle = grd; ctx.fillRect(0, 0, 480, 320);
        // 加一些噪点
        for (let i = 0; i < 600; i++) {
            ctx.fillStyle = `rgba(15,23,42,${Math.random() * 0.04})`;
            ctx.fillRect(Math.random() * 480, Math.random() * 320, 1, 1);
        }
        // 划痕（白色细线）
        ctx.strokeStyle = '#f8fafc';
        ctx.lineWidth = 2;
        const lines = [
            [60, 80, 220, 110],
            [180, 60, 320, 200],
            [90, 220, 280, 240],
            [350, 90, 420, 180],
        ];
        for (const [x1, y1, x2, y2] of lines) {
            ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
        }
        // 阴影描黑模拟凹痕
        ctx.strokeStyle = 'rgba(15,23,42,0.55)';
        ctx.lineWidth = 1;
        for (const [x1, y1, x2, y2] of lines) {
            ctx.beginPath();
            ctx.moveTo(x1 + 1, y1 + 1); ctx.lineTo(x2 + 1, y2 + 1);
            ctx.stroke();
        }
        // 一个圆形凹坑
        ctx.fillStyle = 'rgba(15,23,42,0.18)';
        ctx.beginPath(); ctx.arc(380, 260, 22, 0, Math.PI * 2); ctx.fill();
        ctx.strokeStyle = 'rgba(15,23,42,0.5)'; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(380, 260, 22, 0, Math.PI * 2); ctx.stroke();

        const dataUrl = c.toDataURL('image/png');
        await setAttachment(dataUrl, 'sample.png');
        if (!inputEl.value.trim()) inputEl.value = DEFAULT_PROMPT;
        setStatus('已加载演示图，点「检测 ⏎」', 'ok');
    }

    // 事件
    sendBtn.addEventListener('click', send);
    inputEl.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
            e.preventDefault();
            send();
        }
    });
    uploadBtn.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', async () => {
        const f = fileInput.files && fileInput.files[0];
        if (!f) return;
        try {
            const dataUrl = await readFileAsDataURL(f);
            await setAttachment(dataUrl, f.name);
        } catch (e) {
            setStatus('读取图片失败：' + (e && e.message ? e.message : e), 'err');
        }
        fileInput.value = '';
    });
    rmBtn.addEventListener('click', () => setAttachment(null));
    clearBtn.addEventListener('click', clearAll);
    sampleBtn.addEventListener('click', loadSampleImage);

    inputEl.addEventListener('paste', async e => {
        const items = e.clipboardData && e.clipboardData.items;
        if (!items) return;
        for (const it of items) {
            if (it.kind === 'file' && it.type && it.type.startsWith('image/')) {
                const file = it.getAsFile();
                if (!file) continue;
                e.preventDefault();
                try {
                    const dataUrl = await readFileAsDataURL(file);
                    await setAttachment(dataUrl, 'pasted');
                } catch (err) {
                    setStatus('粘贴图片失败：' + (err && err.message ? err.message : err), 'err');
                }
                return;
            }
        }
    });

    document.addEventListener('DOMContentLoaded', () => {
        history = loadHistory();
        renderAll();
    });
})();
