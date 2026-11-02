r"""抓取平台官方规则原文（用于核对）。当前实现：京东规则中心（官方接口）。

数据来源（京东规则中心前端真实调用，逆向自 rule.jd.com 前端 bundle）：
  - 规则搜索：POST https://knowledge-search.jd.com/api/search/search
  - 规则正文：GET  https://rule-api.jd.com/rule/ruleDetail?id=<ruleId>
              （返回 title / content(HTML) / pdfFileUrl(平台官方PDF) / activeTime 等）

用法：
  python scripts/fetch_platform_rules.py --keywords "售后,退货,退款,赔付,价保,交易纠纷" --limit 10
输出目录（默认 E:\agent\政策原文\京东）：
  <ruleId>_<标题>.html   官方正文
  <ruleId>_<标题>.pdf    平台官方 PDF（仅当该规则提供 pdfFileUrl）
  _index.csv / _index.json
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import time

import httpx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Referer": "https://rule.jd.com/",
    "Origin": "https://rule.jd.com",
}
SEARCH = "https://knowledge-search.jd.com/api/search/search"
DETAIL = "https://rule-api.jd.com/rule/ruleDetail"


def safe(name: str, limit: int = 80) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name or "untitled")
    return name.strip("_")[:limit]


def get_json(client: httpx.Client, url: str, *, params=None, json_body=None, tries: int = 3) -> dict:
    last = None
    for i in range(tries):
        try:
            r = (client.post(url, json=json_body, headers=HEADERS) if json_body is not None
                 else client.get(url, params=params, headers=HEADERS))
            if r.status_code == 200 and r.text.strip():
                return r.json()
            last = f"HTTP {r.status_code} len={len(r.text)}"
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:50]}"
        time.sleep(0.6 * (i + 1))
    raise RuntimeError(last or "unknown")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keywords", default="售后,退货,退款,赔付,价保,交易纠纷,发货,7天无理由,换货,维修,拒收")
    ap.add_argument("--limit", type=int, default=10, help="每个关键词取多少条")
    ap.add_argument("--out", default=r"E:\agent\政策原文")
    a = ap.parse_args()
    out = pathlib.Path(a.out) / "京东"
    out.mkdir(parents=True, exist_ok=True)

    found: dict[str, dict] = {}
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for kw in [k.strip() for k in a.keywords.split(",") if k.strip()]:
            try:
                j = get_json(client, SEARCH, json_body={"key": kw, "knowledgeTypes": 2, "pageSize": a.limit,
                                                        "pageNum": 1, "source": "vender_karma"})
            except Exception as e:
                print(f"[search] {kw}: ERR {e}")
                continue
            items = ((j or {}).get("data") or {}).get("searchResultVos") or []
            print(f"[search] {kw}: {len(items)} hits")
            for it in items:
                rid = str(it.get("bizId"))
                if rid and rid != "None":
                    found.setdefault(rid, {"keyword": kw, "url": it.get("url")})
        print(f"[total] unique rules = {len(found)}")

        index, pdf_ok, detail_fail = [], 0, 0
        for rid, meta in found.items():
            try:
                payload = get_json(client, DETAIL, params={"id": rid}) or {}
                d = payload.get("data") or {}
            except Exception as e:
                detail_fail += 1
                print(f"  [detail] {rid} SKIP ({e})")
                index.append({"rule_id": rid, "title": "", "keyword": meta["keyword"], "status": f"DETAIL_FAIL:{e}"})
                time.sleep(0.25)
                continue
            title = (d.get("title") or "").strip()
            content = d.get("content") or ""
            pdf_url = (d.get("pdfFileUrl") or "").strip()
            if pdf_url.startswith("//"):
                pdf_url = "https:" + pdf_url
            base = f"{rid}_{safe(title)}"
            if content:
                (out / f"{base}.html").write_text(
                    f"<!-- source: https://rule.jd.com/rule/ruleDetail.action?ruleId={rid} -->\n"
                    f"<!-- fetched_by: scripts/fetch_platform_rules.py  (official JD rule-center API) -->\n"
                    f"<html><head><meta charset='utf-8'><title>{title}</title></head><body>"
                    f"<h1>{title}</h1>{content}</body></html>", encoding="utf-8")
            saved = ""
            if pdf_url:
                try:
                    pr = client.get(pdf_url, headers={"User-Agent": HEADERS["User-Agent"]})
                    if pr.status_code == 200 and pr.content[:4] == b"%PDF":
                        (out / f"{base}.pdf").write_bytes(pr.content)
                        saved, pdf_ok = f"{base}.pdf", pdf_ok + 1
                    else:
                        saved = f"NOT_PDF({pr.status_code})"
                except Exception as e:
                    saved = f"ERR:{type(e).__name__}"
            index.append({"rule_id": rid, "title": title, "keyword": meta["keyword"],
                          "content_chars": len(content), "active_time": d.get("activeTime") or "",
                          "pdf_url": pdf_url, "pdf_saved": saved,
                          "html_saved": f"{base}.html" if content else "",
                          "page_url": f"https://rule.jd.com/rule/ruleDetail.action?ruleId={rid}"})
            print(f"  [{rid}] chars={len(content):>6} pdf={'Y' if saved.endswith('.pdf') else '-'} :: {title[:44]}")
            time.sleep(0.25)

    with (out / "_index.csv").open("w", encoding="utf-8-sig", newline="") as f:
        cols = ["rule_id", "title", "keyword", "content_chars", "active_time", "pdf_url", "pdf_saved", "html_saved", "page_url"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(index)
    (out / "_index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDONE: rules={len(index)} detail_fail={detail_fail} pdf_downloaded={pdf_ok} -> {out}")


if __name__ == "__main__":
    main()