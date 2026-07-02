#!/usr/bin/env python3
"""
共读小屋 —— 和你的AI一起读书的网页
纯标准库实现，无第三方依赖。

功能一：纯阅读模式（翻页阅读器 + 进度记忆，讨论走TG）
功能二：批注共读模式（划线/写想法=粉色气泡，Rhys回应=蓝色气泡）

数据结构：
  /root/reading/books/<slug>/
      meta.json          {title, chapters:[...], created}
      chapters/NNN.txt   单章正文
      annotations/NNN.json  [{id, anchor, note, who, ts, replies:[{who,text,ts}]}]
  /root/reading/progress.json  {slug: {ch, page, mode, ts}}
"""
import json
import os
import re
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
BOOKS_DIR = os.path.join(ROOT, "books")
PROGRESS_FILE = os.path.join(ROOT, "progress.json")

# 所有个人化配置都在 config.json（见 config.example.json）
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as _f:
        CFG = json.load(_f)
except (OSError, json.JSONDecodeError):
    CFG = {}
PASSCODE = str(CFG.get("passcode", "0000"))
PORT = int(CFG.get("port", 8000))
SUBTITLE = CFG.get("subtitle", "two readers, one book")
LOGIN_HINT = CFG.get("login_hint", "四位数密码")
USER_NAME = CFG.get("user_name", "我")
AI_NAME = CFG.get("ai_name", "AI")
GARDENER_LOG = CFG.get("gardener_log", "")

os.makedirs(BOOKS_DIR, exist_ok=True)

# ---------------- 数据层 ----------------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def list_books():
    books = []
    for slug in sorted(os.listdir(BOOKS_DIR)):
        meta_path = os.path.join(BOOKS_DIR, slug, "meta.json")
        meta = load_json(meta_path, None)
        if meta:
            meta["slug"] = slug
            books.append(meta)
    return books


def decode_text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "gb18030", "big5"):
        try:
            text = raw.decode(enc)
            # utf-16 误判保护：解出来全是乱码时中文占比会极低
            if enc != "utf-8-sig":
                sample = text[:2000]
                cjk = sum(1 for c in sample if "一" <= c <= "鿿")
                if len(sample) > 100 and cjk < 5:
                    continue
            return text
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace")


# 多种常见章节标记，按命中数取最优
CHAPTER_PATTERNS = [
    # 第X章 / 第X回 …，以及 序章/楔子/番外 等
    r"^\s*((?:第\s*[0-9一二三四五六七八九十百千两〇零]+\s*[章节回卷部集])[^\n]{0,40}"
    r"|(?:序章|序幕|楔子|引子|尾声|终章|后记|番外)[^\n]{0,40}"
    r"|(?:Chapter|CHAPTER)\s+\d+[^\n]{0,40})\s*$",
    # 0001 01 标题 / 0087 终章 …（连载编号式）
    r"^\s*(\d{3,4}\s+[^\n]{1,40}?)\s*$",
    # 01 标题 / 1、标题 / 1.标题
    r"^\s*(\d{1,4}\s*[、.．·:：\s][^\n]{1,35}?)\s*$",
    # 一、标题
    r"^\s*([一二三四五六七八九十百]+\s*[、.．·:：][^\n]{1,35}?)\s*$",
]


def _split_by_matches(text, matches):
    chapters = []
    head = text[: matches[0].start()].strip()
    if len(head) > 200:
        chapters.append(("开篇", head))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end(): end].strip()
        title = m.group(1).strip()
        if body:
            chapters.append((title, body))
    return chapters


def _size_split(text):
    paras = [p for p in text.split("\n") if p.strip()]
    chapters, buf, size = [], [], 0
    for p in paras:
        buf.append(p)
        size += len(p)
        if size >= 8000:
            chapters.append((f"第{len(chapters)+1}部分", "\n".join(buf)))
            buf, size = [], 0
    if buf:
        chapters.append((f"第{len(chapters)+1}部分", "\n".join(buf)))
    return chapters


def ai_split(text: str):
    """DeepSeek 辅助拆章：把候选标题行发给API，让它挑出真正的章节标题行号。"""
    cfg = load_json(os.path.join(ROOT, "config.json"), {})
    key = cfg.get("deepseek_api_key", "")
    if not key:
        return None
    lines = text.split("\n")
    cands = [(i, s.strip()) for i, s in enumerate(lines)
             if s.strip() and len(s.strip()) <= 40
             and re.search(r"\d|第.{1,8}[章节回卷]|序章|楔子|尾声|番外|终章", s)]
    if not cands or len(cands) > 2000:
        return None
    listing = "\n".join(f"{i}|{s}" for i, s in cands)
    prompt = (
        "下面是一本小说里可能是章节标题的行，格式为 行号|内容。"
        "请判断哪些行是真正的章节标题（成体系、编号连续的那种），"
        "只返回JSON，格式 {\"headings\": [行号, ...]}，不要任何其他文字。\n\n" + listing)
    try:
        content = ds_chat("你是小说章节结构分析助手。", prompt,
                          task="拆章判断", detail=f"{len(cands)}个候选标题行",
                          json_mode=True, max_tokens=2000)
        nums = json.loads(content)["headings"]
        nums = sorted({int(n) for n in nums if 0 <= int(n) < len(lines)})
    except Exception:
        return None
    if len(nums) < 3:
        return None
    chapters = []
    head = "\n".join(lines[: nums[0]]).strip()
    if len(head) > 200:
        chapters.append(("开篇", head))
    for j, n in enumerate(nums):
        end = nums[j + 1] if j + 1 < len(nums) else len(lines)
        body = "\n".join(lines[n + 1: end]).strip()
        if body:
            chapters.append((lines[n].strip(), body))
    return chapters if len(chapters) >= 3 else None


def split_chapters(text: str):
    """返回 [(title, body), ...]。本地多模式优先，DeepSeek兜底，最后按字数切。"""
    best = None
    for pat in CHAPTER_PATTERNS:
        matches = list(re.finditer(pat, text, re.MULTILINE))
        # 模式按精确度排序，靠后的宽松模式要多命中15%以上才能取代
        if len(matches) >= 3 and (best is None or len(matches) > len(best) * 1.15):
            best = matches
    if best:
        chapters = _split_by_matches(text, best)
        # 平均章节太小说明误匹配（比如把对话行当标题），弃用
        if chapters and sum(len(b) for _, b in chapters) / len(chapters) > 500:
            return chapters
    chapters = ai_split(text)
    if chapters:
        return chapters
    return _size_split(text)


def save_book(filename: str, raw: bytes):
    title = re.sub(r"\.(txt|text)$", "", filename, flags=re.I).strip() or "未命名"
    slug = re.sub(r"[^\w一-鿿-]+", "-", title).strip("-") or f"book-{int(time.time())}"
    text = decode_text(raw)
    chapters = split_chapters(text)
    if not chapters:
        raise ValueError("empty book")
    bdir = os.path.join(BOOKS_DIR, slug)
    os.makedirs(os.path.join(bdir, "chapters"), exist_ok=True)
    os.makedirs(os.path.join(bdir, "annotations"), exist_ok=True)
    for i, (_t, body) in enumerate(chapters):
        with open(os.path.join(bdir, "chapters", f"{i:03d}.txt"), "w", encoding="utf-8") as f:
            f.write(body)
    meta = {
        "title": title,
        "chapters": [t for t, _ in chapters],
        "created": time.strftime("%Y-%m-%d %H:%M"),
    }
    save_json(os.path.join(bdir, "meta.json"), meta)
    return slug, meta


def get_chapter(slug, idx):
    meta = load_json(os.path.join(BOOKS_DIR, slug, "meta.json"), None)
    if not meta or not 0 <= idx < len(meta["chapters"]):
        return None
    path = os.path.join(BOOKS_DIR, slug, "chapters", f"{idx:03d}.txt")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    return {
        "title": meta["chapters"][idx],
        "book": meta["title"],
        "index": idx,
        "total": len(meta["chapters"]),
        "chapters": meta["chapters"],
        "text": text,
    }


def anno_path(slug, idx):
    return os.path.join(BOOKS_DIR, slug, "annotations", f"{idx:03d}.json")


def note_path(slug, idx):
    return os.path.join(BOOKS_DIR, slug, "notes", f"{idx:03d}.md")


DS_LOG = os.path.join(ROOT, "ds_log.json")
_ds_log_lock = threading.Lock()


def ds_log_add(entry):
    with _ds_log_lock:
        log = load_json(DS_LOG, [])
        entry["ts"] = time.strftime("%Y-%m-%d %H:%M")
        log.append(entry)
        save_json(DS_LOG, log[-500:])


def ds_chat(system, user, task="", detail="", json_mode=False, max_tokens=600):
    key = load_json(os.path.join(ROOT, "config.json"), {}).get("deepseek_api_key", "")
    if not key:
        return None
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.3 if not json_mode else 0,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.load(r)
    except Exception as e:
        ds_log_add({"task": task or "调用", "detail": detail,
                    "ok": False, "error": str(e)[:100]})
        raise
    usage = resp.get("usage", {})
    ds_log_add({"task": task or "调用", "detail": detail, "ok": True,
                "tokens_in": usage.get("prompt_tokens"),
                "tokens_out": usage.get("completion_tokens"),
                "secs": round(time.time() - t0, 1)})
    return resp["choices"][0]["message"]["content"].strip()


NOTE_PROMPT = (
    "你是共读助手，为AI伙伴生成剧情笔记，帮它快速恢复上下文而不必重读全文。"
    "请为这一章写150-250字笔记，包含：出场人物及关系、主要事件、关键伏笔或细节、章末状态。"
    "直接输出笔记正文，不要标题。")


def gen_notes_async(slug):
    """后台线程：用DeepSeek为整本书逐章生成剧情笔记，已有的跳过。"""
    def work():
        meta = load_json(os.path.join(BOOKS_DIR, slug, "meta.json"), None)
        if not meta:
            return
        ndir = os.path.join(BOOKS_DIR, slug, "notes")
        os.makedirs(ndir, exist_ok=True)
        for i in range(len(meta["chapters"])):
            np = note_path(slug, i)
            if os.path.exists(np):
                continue
            ch = get_chapter(slug, i)
            if not ch:
                continue
            try:
                note = ds_chat(NOTE_PROMPT, f"《{meta['title']}》{ch['title']}\n\n{ch['text'][:8000]}",
                               task="剧情笔记", detail=f"{meta['title']}·{ch['title'][:20]}")
            except Exception:
                time.sleep(5)
                continue
            if note:
                with open(np, "w", encoding="utf-8") as f:
                    f.write(f"# {ch['title']}\n\n{note}\n")
            time.sleep(0.5)
    threading.Thread(target=work, daemon=True).start()


# ---------------- 页面模板 ----------------

BASE_CSS = """
:root{--bg:#faf6ef;--card:#fffdf8;--ink:#3d3630;--sub:#9b8f80;--accent:#c96f4a;
--pink:#fdeef0;--pink-line:#e8a0ac;--blue:#e8f1fa;--blue-line:#7fa8d0;--mark:#f9e3c8}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:var(--ink);font-family:'Noto Serif SC',Georgia,serif;
font-size:17px;line-height:1.9}
a{color:var(--accent);text-decoration:none}
.wrap{max-width:640px;margin:0 auto;padding:16px}
button{font-family:inherit;cursor:pointer;border:none;border-radius:10px}
"""

LOGIN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>共读小屋</title><style>__CSS__
.gate{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:20px}
input{font-size:22px;letter-spacing:8px;text-align:center;width:180px;padding:10px;
border:2px solid var(--pink-line);border-radius:12px;background:var(--card);color:var(--ink);outline:none}
.hint{color:var(--sub);font-size:14px}
</style></head><body><div class="gate">
<div style="font-size:40px">📖</div><div>共读小屋</div>
<input id="pc" type="tel" maxlength="4" placeholder="····" autofocus>
<div class="hint">__HINT__</div>
</div><script>
const pc=document.getElementById('pc');
pc.addEventListener('input',()=>{if(pc.value.length===4){
document.cookie='rk='+pc.value+';path=/;max-age=31536000';location.reload();}});
</script></body></html>"""

HOME_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>共读小屋</title><style>__CSS__
h1{font-size:22px;padding:18px 0 4px;text-align:center}
.sub{text-align:center;color:var(--sub);font-size:13px;margin-bottom:20px}
.card{background:var(--card);border-radius:16px;padding:16px;margin-bottom:14px;
box-shadow:0 1px 6px rgba(120,90,60,.08)}
.bt{font-size:18px;font-weight:600;margin-bottom:2px}
.bm{color:var(--sub);font-size:13px;margin-bottom:12px}
.modes{display:flex;gap:10px}
.modes button{flex:1;padding:12px 6px;font-size:15px}
.m1{background:var(--mark);color:var(--ink)}
.m2{background:var(--pink);color:#b05a68;border:1px solid var(--pink-line)}
.cont{margin-top:10px;font-size:13px;color:var(--accent)}
.up{border:2px dashed var(--pink-line);background:none;text-align:center;padding:26px;
border-radius:16px;color:var(--sub);width:100%;font-size:15px}
.empty{text-align:center;color:var(--sub);padding:30px 0}
#st{text-align:center;color:var(--accent);font-size:14px;min-height:20px;margin-top:8px}
</style></head><body><div class="wrap">
<h1>📖 共读小屋</h1><div class="sub">__SUB__</div>
<div id="books"></div>
<input type="file" id="f" accept=".txt" hidden>
<button class="up" onclick="document.getElementById('f').click()">＋ 传一本新书（txt）</button>
<div id="st"></div>
<div style="display:flex;gap:10px;margin-top:16px">
<button style="flex:1;padding:12px;background:var(--blue);border:1px solid var(--blue-line);color:#4a6f96;font-size:14px" onclick="location.href='/ds'">DeepSeek工作台🖥️</button>
<button style="flex:1;padding:12px;background:var(--mark);color:var(--ink);font-size:14px" onclick="location.href='/gardener'">🌙 记忆园丁</button>
</div>
</div><script>
async function load(){
 const [books,prog]=await Promise.all([
   fetch('/api/books').then(r=>r.json()),
   fetch('/api/progress').then(r=>r.json())]);
 const el=document.getElementById('books');
 if(!books.length){el.innerHTML='<div class="empty">书架还空着，传一本书开始吧</div>';return;}
 el.innerHTML=books.map(b=>{
  const p=prog[b.slug];
  const cont=p?`<div class="cont" onclick="go('${b.slug}',${p.ch},${p.mode})">▸ 继续读：${b.chapters[p.ch]}（${p.mode===2?'批注模式':'阅读模式'}）</div>`:'';
  return `<div class="card"><div class="bt">${b.title}</div>
  <div class="bm">${b.chapters.length} 章 · ${b.created}</div>
  <div class="modes">
   <button class="m1" onclick="go('${b.slug}',${p?p.ch:0},1)">功能一 · 纯阅读</button>
   <button class="m2" onclick="go('${b.slug}',${p?p.ch:0},2)">功能二 · 批注共读</button>
  </div>${cont}</div>`;}).join('');
}
function go(s,c,m){location.href='/read/'+encodeURIComponent(s)+'/'+c+'?mode='+m;}
document.getElementById('f').addEventListener('change',async e=>{
 const file=e.target.files[0];if(!file)return;
 const st=document.getElementById('st');st.textContent='上传中…';
 const r=await fetch('/api/upload',{method:'POST',
  headers:{'X-Filename':encodeURIComponent(file.name)},body:file});
 const j=await r.json();
 st.textContent=j.ok?('✓ 已入库：'+j.title+'（'+j.count+' 章）'):('✗ '+j.error);
 load();
});
load();
</script></body></html>"""

READER_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>阅读</title><style>__CSS__
body{overflow:hidden}
#top{position:fixed;top:0;left:0;right:0;background:var(--bg);z-index:5;
display:flex;align-items:center;gap:8px;padding:10px 14px;font-size:13px;color:var(--sub)}
#top a{font-size:15px}
#ct{flex:1;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#page{position:fixed;top:44px;bottom:52px;left:0;right:0;overflow-y:auto;
padding:8px 22px 20px;max-width:680px;margin:0 auto}
#page p{text-indent:2em;margin-bottom:.9em}
mark{background:var(--mark);border-bottom:2px solid var(--accent);padding:1px 0;cursor:pointer}
mark .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-left:2px;vertical-align:super}
mark .du{background:var(--pink-line)}mark .dr{background:var(--blue-line)}
#bot{position:fixed;bottom:0;left:0;right:0;background:var(--bg);z-index:5;
display:flex;align-items:center;justify-content:space-between;padding:8px 18px;
font-size:14px;color:var(--sub)}
#bot button{background:none;font-size:15px;color:var(--accent);padding:6px 14px}
#tool{position:fixed;display:none;z-index:20;background:var(--ink);border-radius:10px;
padding:6px;gap:6px}
#tool button{background:none;color:#fff;font-size:14px;padding:6px 10px}
.sheet{position:fixed;left:0;right:0;bottom:0;z-index:30;background:var(--card);
border-radius:18px 18px 0 0;box-shadow:0 -4px 20px rgba(0,0,0,.15);padding:16px;
max-height:70vh;overflow-y:auto;display:none}
.sheet.on{display:block}
.quote{font-size:13px;color:var(--sub);border-left:3px solid var(--pink-line);
padding-left:8px;margin-bottom:10px}
.bub{border-radius:12px;padding:9px 12px;margin-bottom:8px;font-size:15px;max-width:88%}
.bu{background:var(--pink);border:1px solid var(--pink-line);margin-left:auto}
.br{background:var(--blue);border:1px solid var(--blue-line)}
.who{font-size:11px;color:var(--sub);margin-bottom:2px}
.sheet textarea{width:100%;border:1px solid var(--pink-line);border-radius:10px;
padding:9px;font-family:inherit;font-size:15px;background:var(--bg);color:var(--ink);
resize:none;height:70px;outline:none}
.srow{display:flex;gap:10px;margin-top:8px}
.srow button{flex:1;padding:10px;font-size:15px}
.ok{background:var(--pink);color:#b05a68;border:1px solid var(--pink-line)}
.cc{background:none;color:var(--sub)}
.del{background:none;color:var(--sub);font-size:12px;margin-top:4px}
#mask{position:fixed;inset:0;z-index:25;display:none;background:rgba(0,0,0,.2)}
#mask.on{display:block}
#alist{position:fixed;top:44px;bottom:52px;right:0;width:82%;max-width:340px;z-index:26;
background:var(--card);box-shadow:-3px 0 16px rgba(0,0,0,.12);padding:14px;
overflow-y:auto;display:none}
#alist.on{display:block}
.ai{border-bottom:1px solid #eee2d4;padding:10px 0;font-size:14px;cursor:pointer}
.ai .q{color:var(--sub);font-size:12px}
.ai.cur{color:var(--accent);font-weight:600}
</style></head><body>
<div id="top"><a href="/">〈 书架</a><div id="ct"></div>
<span id="tbtn" style="cursor:pointer;margin-right:12px;font-size:17px">☰</span>
<span id="abtn" style="display:none;cursor:pointer">💬<span id="acnt"></span></span></div>
<div id="page"></div>
<div id="bot"><button onclick="nav(-1)">‹ 上一页</button>
<span id="pg"></span><button onclick="nav(1)">下一页 ›</button></div>
<div id="tool"><button onclick="mk('')">🖊 划线</button><button onclick="mk(null)">💭 写想法</button></div>
<div id="mask" onclick="closeAll()"></div>
<div id="alist"></div>
<div class="sheet" id="sh"></div>
<script>
const SLUG=__SLUG__,CH=__CH__,MODE=__MODE__;
let pages=[],cur=0,annos=[],data=null,pendAnchor=null;
const $=id=>document.getElementById(id);
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

async function init(){
 data=await fetch('/api/chapter/'+SLUG+'/'+CH).then(r=>r.json());
 if(MODE===2){annos=await fetch('/api/annotations/'+SLUG+'/'+CH).then(r=>r.json());
  $('abtn').style.display='inline';}
 const paras=data.text.split('\\n').map(s=>s.trim()).filter(Boolean);
 let buf=[],n=0;pages=[];
 for(const p of paras){buf.push(p);n+=p.length;
  if(n>=1100){pages.push(buf);buf=[];n=0;}}
 if(buf.length)pages.push(buf);
 if(!pages.length)pages=[['（本章为空）']];
 $('ct').textContent=data.title;
 const prog=await fetch('/api/progress').then(r=>r.json());
 const p=prog[SLUG];
 if(p&&p.ch===CH&&p.page<pages.length)cur=p.page;
 render();
}
function render(){
 const el=$('page');
 el.innerHTML=pages[cur].map(p=>'<p>'+deco(p)+'</p>').join('');
 el.scrollTop=0;
 $('pg').textContent=(cur+1)+' / '+pages.length;
 $('acnt').textContent=annos.length?annos.length:'';
 fetch('/api/progress',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({slug:SLUG,ch:CH,page:cur,mode:MODE})});
}
function deco(para){
 if(MODE!==2)return esc(para);
 let hits=annos.filter(a=>para.includes(a.anchor));
 if(!hits.length)return esc(para);
 const a=hits[0];
 const i=para.indexOf(a.anchor);
 const dot=a.replies&&a.replies.length?'dr':'du';
 return esc(para.slice(0,i))+'<mark data-id="'+a.id+'">'+esc(a.anchor)+
  '<span class="dot '+dot+'"></span></mark>'+deco(para.slice(i+a.anchor.length));
}
function nav(d){
 const c=cur+d;
 if(c<0){if(CH>0)location.href='/read/'+SLUG+'/'+(CH-1)+'?mode='+MODE;return;}
 if(c>=pages.length){
  if(CH+1<data.total)location.href='/read/'+SLUG+'/'+(CH+1)+'?mode='+MODE;
  return;}
 cur=c;render();
}
document.addEventListener('keydown',e=>{
 if(e.key==='ArrowLeft')nav(-1);if(e.key==='ArrowRight')nav(1);});

/* -------- 批注 -------- */
function selInfo(){
 const s=window.getSelection();
 if(!s||s.isCollapsed)return null;
 const t=s.toString().trim();
 if(!t||t.length<2||t.length>300)return null;
 if(!$('page').contains(s.anchorNode))return null;
 return {text:t,rect:s.getRangeAt(0).getBoundingClientRect()};
}
if(MODE===2){
 document.addEventListener('selectionchange',()=>{setTimeout(showTool,180);});
 $('page').addEventListener('click',e=>{
  const m=e.target.closest('mark');
  if(m)openAnno(m.dataset.id);});
}
function showTool(){
 const info=selInfo(),t=$('tool');
 if(!info){t.style.display='none';return;}
 pendAnchor=info.text;
 t.style.display='flex';
 t.style.left=Math.max(8,Math.min(info.rect.left,window.innerWidth-150))+'px';
 let top=info.rect.bottom+14;
 if(top>window.innerHeight-110)top=Math.max(50,info.rect.top-46);
 t.style.top=top+'px';
}
function mk(note){
 $('tool').style.display='none';
 const anchor=pendAnchor;if(!anchor)return;
 window.getSelection().removeAllRanges();
 if(note===null){openEditor(anchor);return;}
 saveAnno(anchor,'');
}
function openEditor(anchor){
 const sh=$('sh');
 sh.innerHTML='<div class="quote">'+esc(anchor)+'</div>'+
  '<textarea id="nt" placeholder="想说什么…"></textarea>'+
  '<div class="srow"><button class="cc" onclick="closeAll()">算了</button>'+
  '<button class="ok" onclick="saveAnno(pendCache,document.getElementById(\\'nt\\').value)">存下来 ♡</button></div>';
 window.pendCache=anchor;
 sh.classList.add('on');$('mask').classList.add('on');
 setTimeout(()=>$('nt').focus(),100);
}
async function saveAnno(anchor,note){
 await fetch('/api/annotations/'+SLUG+'/'+CH,{method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({anchor:anchor,note:note,who:'user'})});
 annos=await fetch('/api/annotations/'+SLUG+'/'+CH).then(r=>r.json());
 closeAll();render();
}
function openAnno(id){
 const a0=annos.find(x=>x.id===id);if(!a0)return;
 const group=annos.filter(x=>x.anchor===a0.anchor);
 const sh=$('sh');
 let h='<div class="quote">'+esc(a0.anchor)+'</div>';
 let anyReply=false;
 for(const a of group){
  if(a.note)h+='<div class="bub bu"><div class="who">__UNAME__</div>'+esc(a.note)+'</div>';
  else h+='<div class="bub bu"><div class="who">__UNAME__</div>🖊 划了线</div>';
  for(const r of (a.replies||[])){anyReply=true;
   h+='<div class="bub br"><div class="who">__ANAME__</div>'+esc(r.text)+'</div>';}
  h+='<button class="del" onclick="delAnno(\\''+a.id+'\\')">删除这条</button>';
 }
 if(!anyReply)
  h+='<div style="font-size:12px;color:var(--sub)">__ANAME__ 还没看到，聊天里戳他一下～</div>';
 sh.innerHTML=h;sh.classList.add('on');$('mask').classList.add('on');
}
async function delAnno(id){
 await fetch('/api/annotations/'+SLUG+'/'+CH+'?id='+id,{method:'DELETE'});
 annos=await fetch('/api/annotations/'+SLUG+'/'+CH).then(r=>r.json());
 closeAll();render();
}
$('tbtn').onclick=()=>{
 const el=$('alist');
 el.innerHTML='<div style="font-weight:600;margin-bottom:6px">目录</div>'+
  data.chapters.map((t,i)=>'<div class="ai toc'+(i===CH?' cur':'')+'" data-i="'+i+'">'+esc(t)+'</div>').join('');
 el.querySelectorAll('.toc').forEach(d=>{d.onclick=()=>{
  location.href='/read/'+encodeURIComponent(SLUG)+'/'+d.dataset.i+'?mode='+MODE;};});
 el.classList.add('on');$('mask').classList.add('on');
 const c=el.querySelector('.cur');if(c)c.scrollIntoView({block:'center'});
};
$('abtn').onclick=()=>{
 const el=$('alist');
 el.innerHTML=annos.length?annos.map(a=>
  '<div class="ai" onclick="closeAll();openAnno(\\''+a.id+'\\')">'+
  '<div class="q">'+esc(a.anchor.slice(0,40))+'</div>'+
  esc(a.note||'🖊 划线')+(a.replies&&a.replies.length?' <span style="color:var(--blue-line)">· __ANAME__回了</span>':'')+
  '</div>').join(''):'<div style="color:var(--sub);text-align:center;padding:20px">这一章还没有批注</div>';
 el.classList.add('on');$('mask').classList.add('on');
};
function closeAll(){
 $('sh').classList.remove('on');$('alist').classList.remove('on');
 $('mask').classList.remove('on');$('tool').style.display='none';
}
init();
</script></body></html>"""


DS_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DeepSeek工作台</title><style>__CSS__
h1{font-size:20px;padding:14px 0;text-align:center}
.stat{display:flex;gap:10px;margin-bottom:14px}
.stat div{flex:1;background:var(--card);border-radius:12px;padding:10px;text-align:center}
.stat b{display:block;font-size:20px;color:var(--accent)}
.stat span{font-size:12px;color:var(--sub)}
.item{background:var(--card);border-radius:12px;padding:10px 12px;margin-bottom:8px;font-size:14px}
.item .t{display:flex;justify-content:space-between;color:var(--sub);font-size:12px}
.tag{display:inline-block;padding:1px 8px;border-radius:8px;font-size:12px;margin-right:6px}
.tg1{background:var(--mark)}.tg2{background:var(--pink);color:#b05a68}
.tg3{background:var(--blue);color:#4a6f96}.tgx{background:#f3d6d6;color:#a04040}
.sec{font-size:15px;font-weight:600;margin:18px 0 8px;color:var(--sub)}
.note-item{cursor:pointer}
#nview{background:var(--card);border-radius:12px;padding:14px;font-size:14px;
white-space:pre-wrap;display:none;margin-bottom:10px;border:1px solid var(--blue-line)}
a.back{display:block;padding:12px 0}
</style></head><body><div class="wrap">
<a class="back" href="/">〈 书架</a>
<h1>🤖 DeepSeek 工作台</h1>
<div class="stat" id="stat"></div>
<div class="sec">📖 剧情笔记（点章节查看）</div>
<div id="notes"></div>
<div id="nview"></div>
<div class="sec">📋 调用日志（最近在前）</div>
<div id="log"></div>
</div><script>
const tagCls={'拆章判断':'tg1','剧情笔记':'tg2','调用':'tg3'};
async function load(){
 const [log,books]=await Promise.all([
  fetch('/api/dslog').then(r=>r.json()),
  fetch('/api/books').then(r=>r.json())]);
 const ok=log.filter(e=>e.ok);
 const tin=ok.reduce((s,e)=>s+(e.tokens_in||0),0), tout=ok.reduce((s,e)=>s+(e.tokens_out||0),0);
 const cost=(tin/1e6*2+tout/1e6*8).toFixed(3);
 document.getElementById('stat').innerHTML=
  `<div><b>${log.length}</b><span>总调用</span></div>
   <div><b>${((tin+tout)/1000).toFixed(1)}k</b><span>总tokens</span></div>
   <div><b>¥${cost}</b><span>估算花费</span></div>`;
 let nh='';
 for(const b of books){
  const st=await fetch('/api/noteslist/'+encodeURIComponent(b.slug)).then(r=>r.json());
  nh+=`<div class="item"><b>${b.title}</b> <span style="color:var(--sub);font-size:12px">笔记 ${st.have}/${b.chapters.length} 章</span>
   <div style="margin-top:6px">`+
   b.chapters.map((t,i)=>st.list.includes(i)?
    `<span class="tag tg2 note-item" onclick="showNote('${b.slug}',${i})">${i}</span>`:'').join(' ')+
   `</div></div>`;
 }
 document.getElementById('notes').innerHTML=nh||'<div class="item">还没有笔记</div>';
 document.getElementById('log').innerHTML=log.slice().reverse().map(e=>
  `<div class="item"><span class="tag ${e.ok?(tagCls[e.task]||'tg3'):'tgx'}">${e.task}${e.ok?'':' ✗'}</span>${e.detail||''}
   <div class="t"><span>${e.ok?((e.tokens_in||0)+'+'+(e.tokens_out||0)+' tok · '+(e.secs||'?')+'s'):(e.error||'失败')}</span><span>${e.ts}</span></div></div>`
 ).join('')||'<div class="item">暂无记录</div>';
}
async function showNote(slug,i){
 const d=await fetch('/api/note/'+encodeURIComponent(slug)+'/'+i).then(r=>r.json());
 const v=document.getElementById('nview');
 v.textContent=d.note||'（无）';v.style.display='block';
 v.scrollIntoView({behavior:'smooth'});
}
load();
</script></body></html>"""

GARDENER_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>记忆园丁</title><style>__CSS__
h1{font-size:20px;padding:14px 0;text-align:center}
.card{background:var(--card);border-radius:14px;padding:12px 14px;margin-bottom:10px;font-size:14px}
.day{color:var(--accent);font-weight:600;margin-bottom:6px}
.act{padding:4px 0;border-bottom:1px dashed #eee2d4}
.act:last-child{border-bottom:none}
.meta{color:var(--sub);font-size:12px;margin-top:6px}
a.back{display:block;padding:12px 0}
.info{background:var(--mark);border-radius:12px;padding:10px 14px;font-size:13px;margin-bottom:14px}
</style></head><body><div class="wrap">
<a class="back" href="/">〈 书架</a>
<h1>🌙 记忆园丁</h1>
<div class="info">每天早上5:30自动整理OB记忆：过期一次性事件沉底、重复桶留新不留旧。沉底≠删除，关键词仍可检索，dashboard可恢复。每次干完活会发TG报告。</div>
<div id="runs"></div>
</div><script>
fetch('/api/gardener').then(r=>r.json()).then(runs=>{
 document.getElementById('runs').innerHTML=runs.length?runs.slice().reverse().map(r=>
  `<div class="card"><div class="day">${r.ts}</div>`+
  (r.actions.length?r.actions.map(a=>`<div class="act">${a}</div>`).join(''):'<div class="act" style="color:var(--sub)">检查了一遍，没有需要整理的</div>')+
  `<div class="meta">候选桶 ${r.candidates}${r.tokens_in?' · DeepSeek '+r.tokens_in+'+'+(r.tokens_out||0)+' tok':''}${r.manual?' · 手动执行':''}</div></div>`
 ).join(''):'<div class="card">园丁还没干过活，今晚5:30第一次上岗</div>';
});
</script></body></html>"""


def render(tpl, **kw):
    html = tpl.replace("__CSS__", BASE_CSS)
    # 全局个人化占位符（来自 config.json）
    html = (html.replace("__SUB__", SUBTITLE).replace("__HINT__", LOGIN_HINT)
                .replace("__UNAME__", USER_NAME).replace("__ANAME__", AI_NAME))
    for k, v in kw.items():
        html = html.replace(f"__{k}__", v)
    return html


# ---------------- HTTP ----------------

# 密码尝试限速：同IP 10分钟内错5次 → 封30分钟（4位数密码必须防暴力枚举）
_fails = {}
_fails_lock = threading.Lock()


def _client_blocked(ip):
    with _fails_lock:
        rec = _fails.get(ip)
        if not rec:
            return False
        tries, until = rec
        if until and time.time() < until:
            return True
        if until and time.time() >= until:
            del _fails[ip]
        return False


def _record_fail(ip):
    with _fails_lock:
        tries, until = _fails.get(ip, (0, 0))
        tries += 1
        if tries >= 5:
            _fails[ip] = (0, time.time() + 1800)
        else:
            _fails[ip] = (tries, 0)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # -- helpers --
    def send_html(self, html, code=200):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        ip = self.client_address[0]
        if ip != "127.0.0.1" and _client_blocked(ip):
            return False
        cookies = self.headers.get("Cookie", "")
        if f"rk={PASSCODE}" in cookies:
            return True
        # 带了错误密码才算一次尝试（无cookie的新访客不算）
        m = re.search(r"rk=(\d+)", cookies)
        if m and m.group(1) != PASSCODE:
            _record_fail(ip)
        return False

    def body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    # -- routes --
    def do_GET(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        qs = dict(p.split("=", 1) for p in u.query.split("&") if "=" in p)

        if not self.authed():
            if path.startswith("/api/"):
                return self.send_json({"error": "unauthorized"}, 401)
            return self.send_html(render(LOGIN_HTML))

        if path == "/":
            return self.send_html(render(HOME_HTML))

        if path == "/ds":
            return self.send_html(render(DS_HTML))

        if path == "/gardener":
            return self.send_html(render(GARDENER_HTML))

        if path == "/api/dslog":
            return self.send_json(load_json(DS_LOG, []))

        if path == "/api/gardener":
            return self.send_json(load_json(GARDENER_LOG, []) if GARDENER_LOG else [])

        m = re.match(r"^/api/noteslist/([^/]+)$", path)
        if m:
            ndir = os.path.join(BOOKS_DIR, m.group(1), "notes")
            lst = sorted(int(f[:3]) for f in os.listdir(ndir)
                         if f.endswith(".md")) if os.path.isdir(ndir) else []
            return self.send_json({"have": len(lst), "list": lst})

        m = re.match(r"^/read/([^/]+)/(\d+)$", path)
        if m:
            slug, idx = m.group(1), int(m.group(2))
            mode = 2 if qs.get("mode") == "2" else 1
            if not get_chapter(slug, idx):
                return self.send_html("<h3>没找到这一章</h3>", 404)
            return self.send_html(render(
                READER_HTML,
                SLUG=json.dumps(slug, ensure_ascii=False),
                CH=str(idx), MODE=str(mode)))

        if path == "/api/books":
            return self.send_json(list_books())

        if path == "/api/progress":
            return self.send_json(load_json(PROGRESS_FILE, {}))

        m = re.match(r"^/api/chapter/([^/]+)/(\d+)$", path)
        if m:
            ch = get_chapter(m.group(1), int(m.group(2)))
            return self.send_json(ch) if ch else self.send_json({"error": "not found"}, 404)

        m = re.match(r"^/api/annotations/([^/]+)/(\d+)$", path)
        if m:
            return self.send_json(load_json(anno_path(m.group(1), int(m.group(2))), []))

        # 章节剧情笔记（DeepSeek预读生成，供AI伴读快速恢复上下文）
        m = re.match(r"^/api/note/([^/]+)/(\d+)$", path)
        if m:
            np = note_path(m.group(1), int(m.group(2)))
            if os.path.exists(np):
                with open(np, encoding="utf-8") as f:
                    return self.send_json({"note": f.read()})
            return self.send_json({"note": None}, 404)

        # Rhys 专用：列出所有还没回应的批注
        if path == "/api/pending":
            out = []
            for b in list_books():
                adir = os.path.join(BOOKS_DIR, b["slug"], "annotations")
                if not os.path.isdir(adir):
                    continue
                for fn in sorted(os.listdir(adir)):
                    for a in load_json(os.path.join(adir, fn), []):
                        if not a.get("replies"):
                            out.append({"book": b["slug"], "chapter": int(fn[:3]), **a})
            return self.send_json(out)

        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        if not self.authed():
            return self.send_json({"error": "unauthorized"}, 401)

        if path == "/api/upload":
            raw = self.body()
            name = unquote(self.headers.get("X-Filename", "book.txt"))
            if not raw:
                return self.send_json({"ok": False, "error": "空文件"})
            try:
                slug, meta = save_book(name, raw)
            except Exception as e:
                return self.send_json({"ok": False, "error": str(e)})
            gen_notes_async(slug)
            return self.send_json({"ok": True, "slug": slug,
                                   "title": meta["title"], "count": len(meta["chapters"])})

        if path == "/api/progress":
            d = json.loads(self.body() or b"{}")
            prog = load_json(PROGRESS_FILE, {})
            prog[d["slug"]] = {"ch": d["ch"], "page": d["page"],
                               "mode": d["mode"], "ts": int(time.time())}
            save_json(PROGRESS_FILE, prog)
            return self.send_json({"ok": True})

        m = re.match(r"^/api/annotations/([^/]+)/(\d+)$", path)
        if m:
            slug, idx = m.group(1), int(m.group(2))
            d = json.loads(self.body() or b"{}")
            annos = load_json(anno_path(slug, idx), [])
            annos.append({
                "id": uuid.uuid4().hex[:8],
                "anchor": d.get("anchor", "")[:300],
                "note": d.get("note", "")[:2000],
                "who": "user",
                "ts": time.strftime("%Y-%m-%d %H:%M"),
                "replies": [],
            })
            save_json(anno_path(slug, idx), annos)
            return self.send_json({"ok": True})

        self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        qs = dict(p.split("=", 1) for p in u.query.split("&") if "=" in p)
        if not self.authed():
            return self.send_json({"error": "unauthorized"}, 401)
        m = re.match(r"^/api/annotations/([^/]+)/(\d+)$", path)
        if m:
            slug, idx = m.group(1), int(m.group(2))
            annos = load_json(anno_path(slug, idx), [])
            annos = [a for a in annos if a["id"] != qs.get("id")]
            save_json(anno_path(slug, idx), annos)
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"共读小屋 running on :{PORT}")
    server.serve_forever()
