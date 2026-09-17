"""中文维基导游(wikivoyage) dump -> 足迹知识库文件

为什么有这个脚本：
  项目原有的 china_city_guide.txt 只覆盖 10 个城市(约 6 千字)，用户问外地城市基本答不出。
  中文维基导游 dump 覆盖 8000+ 条目 / 2200 万字，清洗切块后 549 万字 / 34313 块。
  生成的文件按 ### 分块，与 vector_store.txt_loader_by_city 的切分规则 1:1 对应，
  入库时不会再被二次切分（见 vector_store.load_document 里 "txt 已按城市切好，直接存"）。

关键约束（踩过的坑，改代码前先看）：
  1. embed-model 启动参数是 --ctx-size 512。实测单块 >=600 字时 /v1/embeddings 直接 500，
     所以 CHUNK 必须 <=300 字（中文 1 字约 1~1.5 token）。改 CHUNK 前先确认 compose 里的 ctx。
  2. dump 的 XML 带 namespace（xmlns="http://www.mediawiki.org/xml/export-0.11/"），
     按 tag == 'page' 匹配会得到 0 条，必须按 local name（去掉 {ns} 前缀）匹配。
  3. zh.wikivoyage.org 的 MediaWiki API 在当前网络下不通（SSL 被干扰），只能走 dumps 下载。
  4. 模板剥离会丢信息：地名常挂在 {{mapshape|title=[[黑龙江]]|...}} 这类模板的 title/name 参数里，
     整体删掉会让"城市"章节变成一堆 "* — 描述"。清洗时要把 title/name 参数的值捞出来。
  5. 块内若含 ### 会与文件的分块符撞车（loader 用 split("###")），导出时压成 ##。

用法：
  python scripts/build_wikivoyage_kb.py                 # 下载最新 dump 并重建
  python scripts/build_wikivoyage_kb.py --dump D.xml.bz2  # 用已有 dump
  python scripts/build_wikivoyage_kb.py --out data/xx.txt --limit 5000

数据来源与许可：中文维基导游(zh.wikivoyage.org)，CC BY-SA 4.0。
"""
import argparse
import bz2
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, os.pardir, "app", "agent", "data",
                           "china_city_guide_wikivoyage.txt")
DUMP_URL = ("https://dumps.wikimedia.org/zhwikivoyage/latest/"
            "zhwikivoyage-latest-pages-articles.xml.bz2")

CHUNK = 280        # <=300，受 embed-model 的 --ctx-size 512 限制
MIN_CHUNK = 40     # 太短的块没有检索价值

ENTITIES = [('&nbsp;', ' '), ('&mdash;', '—'), ('&ndash;', '–'), ('&lsaquo;', ''),
            ('&rsaquo;', ''), ('&amp;', '&'), ('&quot;', '"'), ('&lt;', '<'),
            ('&gt;', '>'), ('&middot;', '·'), ('&times;', '×'), ('&deg;', '°'),
            ('&hellip;', '…'), ('&#160;', ' '), ('&prime;', "'"), ('&euro;', '€'),
            ('&pound;', '£'), ('&yen;', '¥'), ('&copy;', '©'), ('&reg;', '®'),
            ('&trade;', '™'), ('&sup2;', '²'), ('&frac12;', '½'), ('&bull;', '·'),
            ('&laquo;', '«'), ('&raquo;', '»'), ('&apos;', "'"), ('&rarr;', '→')]

SKIP_NS = ('Wikivoyage', 'Template', 'Category', 'Help', 'Portal',
           'MediaWiki', 'User', 'Talk', 'File', 'Project')
META_TITLES = {'首页', 'Main Page', '会话手册', '旅行路线', '交通', '通讯', '旅行话题索引'}
HEAD_RE = re.compile(r'^(={2,4})\s*(.+?)\s*\1\s*$', re.M)


# ---------------- 1. 取 dump ----------------
def download_dump(dest: str) -> str:
    if os.path.exists(dest) and os.path.getsize(dest) > 1e6:
        print(f"[dump] 已存在, 跳过下载: {dest} ({os.path.getsize(dest)/1e6:.1f} MB)")
        return dest
    print("[dump] 下载中(约 15MB)...")
    req = urllib.request.Request(DUMP_URL, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; footprint-kb/1.0)'})
    t = time.time()
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, 'wb') as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    print("[dump] 完成 %.1f MB / %.1fs" % (os.path.getsize(dest)/1e6, time.time() - t))
    return dest


def parse_dump(path: str):
    """解析 MediaWiki dump。必须按 local name 匹配(带 namespace)。"""
    def local(tag):
        return tag.rsplit('}', 1)[-1]

    pages = []
    with bz2.open(path, 'rb') as f:
        for _, el in ET.iterparse(f, events=('end',)):
            if local(el.tag) == 'page':
                title = nsv = txt = None
                for ch in list(el):
                    ln = local(ch.tag)
                    if ln == 'title':
                        title = ch.text
                    elif ln == 'ns':
                        nsv = ch.text
                    elif ln == 'revision':
                        for rc in list(ch):
                            if local(rc.tag) == 'text':
                                txt = rc.text or ''
                if nsv == '0' and (txt or '').strip():
                    pages.append([(title or '').strip(), txt])
                el.clear()
    print(f"[dump] 解析出主命名空间条目 {len(pages)} 条")
    return pages


# ---------------- 2. 清洗 ----------------
def _unwrap_links(v: str) -> str:
    v = re.sub(r'\[\[([^\]|]*)\|([^\]]*)\]\]', r'\2', v)
    v = re.sub(r'\[\[([^\]]*)\]\]', r'\1', v)
    v = re.sub(r'\[\[([^\]|]*)\|([^\]]*)\]', r'\2', v)   # 残缺内链 [[A|B]
    v = re.sub(r'\[\[([^\]]*)\]', r'\1', v)
    return v


def _tmpl_repl(m):
    """模板整体是元数据；但 title/name 参数常挂着真实地名，捞出来保留"""
    inner = m.group(1)
    for key in ('title', 'name'):
        mm = re.search(r'(?:^|\|)\s*' + key + r'\s*=\s*([^|}]+)', inner)
        if mm:
            v = _unwrap_links(mm.group(1).strip())
            v = re.sub(r'[#<>\[\]{}]', '', v).strip()
            if v:
                return v + ' '
    return ''


def clean(txt: str) -> str:
    txt = re.sub(r'<!--.*?-->', '', txt, flags=re.S)
    txt = re.sub(r'<ref[^>]*>.*?</ref>', '', txt, flags=re.S)
    txt = re.sub(r'<ref[^>]*/>', '', txt)
    txt = re.sub(r'-\{[^{}]*\}-', '', txt)             # 语言变体 -{H|zh-cn:..}-
    txt = re.sub(r'<[^>]{0,300}?>', '', txt)
    txt = re.sub(r'__[A-Z_]+__', '', txt)
    txt = re.sub(r'\{\|.*?\|\}', '', txt, flags=re.S)  # 表格
    for _ in range(6):                                  # 多轮剥嵌套模板
        new = re.sub(r'\{\{([^{}]*)\}\}', _tmpl_repl, txt)
        if new == txt:
            break
        txt = new
    txt = re.sub(r'\{\{[^}]*$', '', txt, flags=re.M)
    txt = re.sub(r'^\s*[|}!].*$', '', txt, flags=re.M)
    txt = re.sub(r'\[\[(?:File|Image|文件|图像|Category|分类)\s*:[^\]]*\]\]', '', txt, flags=re.I)
    txt = txt.replace('}}}', '').replace('}}', '')
    txt = _unwrap_links(txt)
    txt = re.sub(r'\[https?://\S+\s+([^\]]*)\]', r'\1', txt)
    txt = re.sub(r'\[https?://\S+\]', '', txt)
    txt = txt.replace(']]', '')
    txt = txt.replace("'''", '').replace("''", '')
    for a, b in ENTITIES:
        txt = txt.replace(a, b)
    txt = re.sub(r'\b\d+px\b\s*\|?[^\s]*', '', txt)
    txt = re.sub(r'[ \t]{2,}', ' ', txt)
    txt = re.sub(r'\n{3,}', '\n\n', txt)
    return txt.strip()


def is_meta(title: str) -> bool:
    """元页面(首页/沙盒/消歧义/纯 ASCII 缩写页) —— 不是内容，不入库"""
    return (title in META_TITLES or '消歧义' in title
            or '沙盒' in title or title.isascii())


# ---------------- 3. 切块 ----------------
def split_blocks(title: str, txt: str):
    """先按二级标题切 section，再按段落聚合到 <= CHUNK 字"""
    matches = list(HEAD_RE.finditer(txt))
    segs = []
    if not matches:
        segs.append(('', txt))
    else:
        if matches[0].start() > 0:
            segs.append(('', txt[:matches[0].start()]))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(txt)
            segs.append((m.group(2), txt[m.end():end]))

    parts = []
    for sec, body in segs:
        body = body.strip()
        if not body:
            continue
        sec = sec.strip()
        if sec and not re.search(r'[\u4e00-\u9fa5]', sec):
            sec = ''
        buf = ''
        for para in re.split(r'\n\s*\n', body):
            para = re.sub(r'\s*\n\s*', ' ', para).strip()
            if not para:
                continue
            while len(para) > CHUNK:
                cut = para.rfind('。', 0, CHUNK)
                cut = cut + 1 if cut > MIN_CHUNK else CHUNK
                piece, para = para[:cut], para[cut:]
                if buf:
                    piece, buf = buf + piece, ''
                parts.append((sec, piece))
            if len(buf) + len(para) + 1 <= CHUNK:
                buf = (buf + ' ' + para).strip() if buf else para
            else:
                if buf:
                    parts.append((sec, buf))
                buf = para
        if buf:
            parts.append((sec, buf))
    return parts


# ---------------- 4. 主流程 ----------------
def build(pages, out_path: str, limit: int = 0) -> int:
    t0 = time.time()
    blocks = []
    for title, raw in pages:
        if title.startswith(SKIP_NS) or is_meta(title):
            continue
        txt = clean(raw)
        if not txt:
            continue
        for sec, body in split_blocks(title, txt):
            if len(body) < MIN_CHUNK:
                continue
            text = ('%s · %s：%s' % (title, sec, body)) if sec else ('%s：%s' % (title, body))
            blocks.append(text[:CHUNK])
            if limit and len(blocks) >= limit:
                break
        if limit and len(blocks) >= limit:
            break

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8', newline='\n') as f:
        for text in blocks:
            # 块内 ### 会与分块符撞车 -> 压成 ##
            body = re.sub(r'#{2,}', '##', text).replace('\n', ' ').strip()
            f.write('### ' + body + '\n\n')

    # 自检：loader 用 split("###")，段数必须等于块数
    parts = [p for p in open(out_path, encoding='utf-8').read().split('###') if p.strip()]
    ok = len(parts) == len(blocks)
    over = sum(1 for p in parts if len(p) > CHUNK + 20)
    print('[build] 条目 %d -> 块 %d, 共 %.1f 万字, 耗时 %.1fs'
          % (len({t for t, _ in pages}), len(blocks),
             sum(len(b) for b in blocks) / 1e4, time.time() - t0))
    print('[build] 输出 %s (%.1f MB)' % (out_path, os.path.getsize(out_path) / 1e6))
    print('[build] 自检: 切分段数 %d vs 块数 %d -> %s'
          % (len(parts), len(blocks), 'OK' if ok else '不匹配!'))
    print('[build] 超出 %d 字的块: %d (embed ctx-size 512, 必须为 0)' % (CHUNK + 20, over))
    return len(blocks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dump', default=os.path.join(HERE, 'zhwikivoyage-latest-pages-articles.xml.bz2'))
    ap.add_argument('--out', default=DEFAULT_OUT)
    ap.add_argument('--limit', type=int, default=0, help='只取前 N 块(调试用)')
    ap.add_argument('--no-download', action='store_true')
    a = ap.parse_args()

    if not os.path.exists(a.dump):
        if a.no_download:
            raise SystemExit(f'dump 不存在且指定不下载: {a.dump}')
        download_dump(a.dump)
    pages = parse_dump(a.dump)
    n = build(pages, a.out, a.limit)
    stats_path = os.path.join(os.path.dirname(os.path.abspath(a.out)), os.pardir,
                              os.pardir, os.pardir, 'train_data', 'kb_stats.json')
    try:
        json.dump({'n_blocks': n, 'source': 'zh.wikivoyage.org', 'license': 'CC BY-SA 4.0'},
                  open(os.path.normpath(stats_path), 'w'), ensure_ascii=False, indent=1)
    except Exception:
        pass
    print('\n下一步: 重建后端容器让 load_knowledge.py 入库')
    print('  docker compose --profile ai up -d --build backend')


if __name__ == '__main__':
    main()
