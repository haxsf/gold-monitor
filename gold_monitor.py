#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
金价监测脚本（每3分钟，触发才提醒，其余静默）
- 数据源: 新浪财经 上海黄金交易所 Au(T+D)（元/克），备源伦敦金折算
- 日间(5:00~22:00): 金价 > 795 或 < 760 → 推微信(Server酱)
- 晚间(22:00~次日5:00): 金价 > 795 或 < 750 → 推微信(Server酱)
- 补仓档位: 跌破 762 / 752 → 推"补仓提醒"（每档只推一次，回升3元以上自动重置）
- 止盈参考: 升破 803 → 推"止盈提醒"（只推一次，回落3元以上自动重置）
- 未触发/获取失败: 静默退出, 不发任何消息
用法: python3 gold_monitor.py [--test]   # --test 强制发送一条测试推送验证通道
环境变量 GOLD_SENDKEY 可覆盖默认SendKey(GitHub Actions使用)
"""
import sys, os, json, datetime, traceback
import requests

# ===== 配置 =====
SENDKEY = os.environ.get("GOLD_SENDKEY", "SCT482805TemOddyb9es5f4OOyqrXF3q5u")
HIGH_LIMIT = 795.0  # 统一上限
LOW_DAY = 760.0     # 日间下限
LOW_NIGHT = 750.0   # 晚间下限
BUY_TIERS = [762.0, 752.0]  # 补仓档位（先到先报，从高到低）
TP_LEVEL = 803.0    # 止盈参考位
DAY_START, DAY_END = 5, 22  # 日间: [5:00, 22:00)
BASE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE, "gold_monitor.log")
STATE_FILE = os.path.join(BASE, "gold_state.json")
CA = "/etc/ssl/certs/ca-certificates.crt" if os.path.exists("/etc/ssl/certs/ca-certificates.crt") else True
TIMEOUT = 15
RESET_GAP = 3.0  # 档位提醒重置的幅度

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception:
        pass

def get_gold_price():
    """获取金价(元/克)。主源: 上金所 Au(T+D); 备源: 伦敦金XAU折算。"""
    headers = {"Referer": "https://finance.sina.com.cn"}
    try:
        r = requests.get("https://hq.sinajs.cn/list=gds_AUTD", headers=headers, timeout=TIMEOUT, verify=CA)
        r.encoding = "gbk"
        price = float(r.text.split('"')[1].split(",")[0])
        if price > 0:
            return price, "Au(T+D)"
    except Exception:
        pass
    try:
        r1 = requests.get("https://hq.sinajs.cn/list=hf_XAU", headers=headers, timeout=TIMEOUT, verify=CA)
        r1.encoding = "gbk"
        xau = float(r1.text.split('"')[1].split(",")[0])
        r2 = requests.get("https://hq.sinajs.cn/list=fx_susdcny", headers=headers, timeout=TIMEOUT, verify=CA)
        r2.encoding = "gbk"
        usdcny = float(r2.text.split('"')[1].split(",")[9])
        price = xau * usdcny / 31.1035
        if price > 0:
            return round(price, 2), "伦敦金折算"
    except Exception:
        pass
    return None, None

def push_wechat(title, desp):
    r = requests.post(
        f"https://sctapi.ftqq.com/{SENDKEY}.send",
        data={"title": title[:32], "desp": desp}, timeout=TIMEOUT, verify=CA)
    return r.json().get("code") == 0

def main():
    test = "--test" in sys.argv
    now = datetime.datetime.now()
    is_day = DAY_START <= now.hour < DAY_END
    low = LOW_DAY if is_day else LOW_NIGHT
    period = "日间" if is_day else "晚间"

    if test:
        ok = push_wechat("金价监测·通道测试", "✅ 推送通道正常")
        log(f"TEST push {'OK' if ok else 'FAILED'}")
        print("test push:", "OK" if ok else "FAILED")
        return

    price, source = get_gold_price()
    if price is None:
        log("SILENT (price unavailable)")
        return

    st = load_state()
    tiers = st.get("tiers", {})
    tp_done = st.get("tp", False)
    changed = False

    # ---- 补仓档位: 跌破即提醒一次, 回升3元以上重置 ----
    extras = []
    for lvl in BUY_TIERS:
        key = str(int(lvl))
        if price <= lvl and not tiers.get(key):
            extras.append(f"补仓档位{lvl:g}已触及")
            tiers[key] = True
            changed = True
        elif price > lvl + RESET_GAP and tiers.get(key):
            tiers[key] = False
            changed = True

    # ---- 止盈参考位: 升破即提醒一次, 回落3元以上重置 ----
    if price >= TP_LEVEL and not tp_done:
        extras.append(f"止盈参考位{TP_LEVEL:g}已升破")
        tp_done = True
        changed = True
    elif price < TP_LEVEL - RESET_GAP and tp_done:
        tp_done = False
        changed = True

    st["tiers"], st["tp"] = tiers, tp_done
    save_state(st)

    # ---- 阈值判断 ----
    threshold_hit = price > HIGH_LIMIT or price < low
    trig = (f"已超过上限 {HIGH_LIMIT:g} 元/克" if price > HIGH_LIMIT
            else (f"已跌破{period}下限 {low:g} 元/克" if price < low else None))

    if threshold_hit:
        title = f"金价提醒:{price:g}元/克 {trig}"
        desp = f"**当前金价:** {price:g} 元/克（{source}）\n\n**时间:** {now.strftime('%H:%M')}\n\n**触发:** {trig}"
        if extras:
            title += "(含档位)"
            desp += "\n\n**同时触及:** " + "；".join(extras)
    elif extras:
        title = f"金价提醒:{extras[0]} 现价{price:g}"
        desp = (f"**当前金价:** {price:g} 元/克（{source}）\n\n**时间:** {now.strftime('%H:%M')}\n\n"
                + "\n\n".join(f"**{e}**" for e in extras))
    else:
        log(f"SILENT ({period}) price={price} source={source}")
        return

    ok = push_wechat(title, desp)
    log(f"ALERT ({period}) price={price} trig={trig} extras={extras} push={'OK' if ok else 'FAILED'}")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("ERROR " + traceback.format_exc().replace("\n", " | "))
