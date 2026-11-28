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
    """获取金价。返回 (最新价, 来源, meta) ，meta含昨收/最高/最低/日期，用于波动与趋势分析。"""
    headers = {"Referer": "https://finance.sina.com.cn"}
    try:
        r = requests.get("https://hq.sinajs.cn/list=gds_AUTD", headers=headers, timeout=TIMEOUT, verify=CA)
        r.encoding = "gbk"
        p = r.text.split('"')[1].split(",")
        price = float(p[0])
        if price > 0:
            meta = {"prev": float(p[3]), "high": float(p[4]), "low": float(p[5]), "date": p[12]}
            return price, "Au(T+D)", meta
    except Exception:
        pass
    try:
        r1 = requests.get("https://hq.sinajs.cn/list=hf_XAU", headers=headers, timeout=TIMEOUT, verify=CA)
        r1.encoding = "gbk"
        p1 = r1.text.split('"')[1].split(",")
        xau = float(p1[0])
        r2 = requests.get("https://hq.sinajs.cn/list=fx_susdcny", headers=headers, timeout=TIMEOUT, verify=CA)
        r2.encoding = "gbk"
        usdcny = float(r2.text.split('"')[1].split(",")[9])
        price = round(xau * usdcny / 31.1035, 2)
        if price > 0:
            meta = {"prev": round(float(p1[1]) * usdcny / 31.1035, 2),
                    "high": round(float(p1[4]) * usdcny / 31.1035, 2),
                    "low": round(float(p1[5]) * usdcny / 31.1035, 2),
                    "date": p1[12]}
            return price, "伦敦金折算", meta
    except Exception:
        pass
    return None, None, None

def push_wechat(title, desp):
    r = requests.post(
        f"https://sctapi.ftqq.com/{SENDKEY}.send",
        data={"title": title[:32], "desp": desp}, timeout=TIMEOUT, verify=CA)
    return r.json().get("code") == 0

def build_advisory(price, meta, st):
    """根据当日振幅与近几日走势，生成策略调整提示。每个交易日每类最多提醒一次。"""
    if not meta or not meta.get("prev"):
        return None
    qdate = str(meta.get("date", ""))
    notes = st.get("notes", {})
    if notes.get("date") != qdate:
        notes = {"date": qdate, "vol": False, "trend": False}
        st["notes"] = notes

    # 记录每日收盘价（每日首次运行时记录当日价）
    closes = st.get("closes", [])
    if not closes or closes[-1]["d"] != qdate:
        closes.append({"d": qdate, "c": price})
        st["closes"] = closes[-10:]

    st_changed = notes != st.get("notes") or st.get("closes") != st.get("closes")

    # 1) 波动加剧: 当日振幅 >= 3%
    amp = (meta["high"] - meta["low"]) / meta["prev"] * 100
    vol_msg = None
    if amp >= 3.0 and not notes.get("vol"):
        notes["vol"] = True
        vol_msg = (f"今日振幅已达 {amp:.1f}%，波动显著放大。建议："
                   "①固定档位补仓暂缓执行，等波动收敛后再按 762/752 档行动；"
                   "②已有持仓不追加、不追单；"
                   "③严守 750/795 防守位。")

    # 2) 趋势转变: 最近3个交易日同方向且累计变动 >= 1.5%
    trend_msg = None
    if len(st.get("closes", [])) >= 4 and not notes.get("trend"):
        chg = [(st["closes"][i]["c"] / st["closes"][i-1]["c"] - 1) * 100
               for i in range(len(st["closes"]) - 3, len(st["closes"]))]
        if all(c > 0.2 for c in chg) and sum(chg) >= 1.5:
            notes["trend"] = True
            trend_msg = (f"近3个交易日连续上涨、累计 +{sum(chg):.1f}%，短期趋势转强。建议："
                         "①762/752 补仓档大概率不触发，耐心等回调、勿追高；"
                         "②若有效突破 795 并站稳，可用 2 万在回踩 795~798 时顺势买入；"
                         "③803 止盈参考不变，到位分批减仓。")
        elif all(c < -0.2 for c in chg) and sum(chg) <= -1.5:
            notes["trend"] = True
            trend_msg = (f"近3个交易日连续下跌、累计 {sum(chg):.1f}%，短期趋势转弱。建议："
                         "①补仓档位整体下移一档执行（762→756、752→746），避免接飞刀；"
                         "②跌破 750 严格执行减仓 1/3~1/2 纪律；"
                         "③已有仓位等反弹至保本价 783.7 上方再考虑减仓换手。")

    if vol_msg or trend_msg:
        return "；".join(x for x in (vol_msg, trend_msg) if x)
    return None

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

    price, source, meta = get_gold_price()
    if price is None:
        log("SILENT (price unavailable)")
        return

    st = load_state()

    # ---- 波动/趋势自适应策略提示（每交易日每类最多提醒一次） ----
    advisory = build_advisory(price, meta, st)

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
        if advisory:
            desp += "\n\n---\n\n**📋 策略提示:** " + advisory
    elif extras:
        title = f"金价提醒:{extras[0]} 现价{price:g}"
        desp = (f"**当前金价:** {price:g} 元/克（{source}）\n\n**时间:** {now.strftime('%H:%M')}\n\n"
                + "\n\n".join(f"**{e}**" for e in extras))
        if advisory:
            desp += "\n\n---\n\n**📋 策略提示:** " + advisory
    elif advisory:
        title = f"金价策略提醒:{'波动加剧' if '振幅' in advisory else '趋势转变'}"
        desp = (f"**当前金价:** {price:g} 元/克（{source}）\n\n**时间:** {now.strftime('%H:%M')}\n\n"
                f"**📋 策略提示:** {advisory}")
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
