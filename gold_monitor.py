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
QUIET_START, QUIET_END = 23, 5  # 深夜静默窗口 [23:00, 次日5:00): 只发操作类提醒(越限/补仓/止盈), 策略类不发
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
    """根据当日振幅、盘中变动与近几日走势，生成策略调整提示。
    提醒次数不设上限：同一交易日内出现"新的重大变动"会再次提醒——
    ①振幅再扩大 >= 1 个百分点；②趋势累计涨跌再扩大 >= 1 个百分点或方向反转；
    ③自上次提醒后价格再变动 >= 1%。小波动则静默。"""
    if not meta or not meta.get("prev"):
        return None
    qdate = str(meta.get("date", ""))
    notes = st.get("notes", {})
    if notes.get("date") != qdate:
        # 换日重置振幅与盘中基准价, 但保留趋势记忆(避免持续趋势每天重复提醒)
        notes = {"date": qdate, "vol_amp": 0.0, "last_price": price,
                 "trend_dir": notes.get("trend_dir", ""), "trend_cum": notes.get("trend_cum", 0.0)}
        st["notes"] = notes

    closes = st.get("closes", [])
    amp = (meta["high"] - meta["low"]) / meta["prev"] * 100
    if not closes or closes[-1]["d"] != qdate:
        closes.append({"d": qdate, "c": price, "amp": round(amp, 2)})
        st["closes"] = closes[-10:]
    else:
        closes[-1]["c"] = price          # 当日最新价(近似收盘价, 供趋势/区间计算)
        closes[-1]["amp"] = round(amp, 2)

    # 1) 波动加剧: 当日振幅 >= 3%; 振幅较上次已提醒水平再扩大 >= 1 个百分点 → 再次提醒
    vol_msg = None
    if amp >= 3.0 and amp - notes.get("vol_amp", 0.0) >= 1.0:
        notes["vol_amp"] = amp
        vol_msg = f"今日振幅{amp:.1f}%，波动大。操作：暂停补仓，持仓观望，守住上下限再动。"

    # 2) 趋势转变: 近3个交易日同方向且累计 >= 1.5%; 方向反转或累计再扩大 >= 1 个百分点 → 再次提醒
    trend_msg = None
    if len(closes) >= 4:
        chg = [(closes[i]["c"] / closes[i-1]["c"] - 1) * 100
               for i in range(len(closes) - 3, len(closes))]
        s = sum(chg)
        if all(c > 0.2 for c in chg) and s >= 1.5:
            if notes.get("trend_dir") != "up" or s - notes.get("trend_cum", 0.0) >= 1.0:
                notes["trend_dir"], notes["trend_cum"] = "up", s
                trend_msg = (f"近3日连涨{s:.1f}%，趋势向上。操作：不追高；"
                             "突破795站稳后回踩795~798买2万；到803减1/3。")
        elif all(c < -0.2 for c in chg) and s <= -1.5:
            if notes.get("trend_dir") != "down" or notes.get("trend_cum", 0.0) - s >= 1.0:
                notes["trend_dir"], notes["trend_cum"] = "down", s
                trend_msg = (f"近3日连跌{s:.1f}%，趋势向下。操作："
                             "补仓档下移到756/746再接；跌破750减仓1/3~1/2；反弹过783.7再卖。")
        elif notes.get("trend_dir"):
            # 趋势信号消失 → 重置, 下次新趋势形成时立即提醒
            notes["trend_dir"], notes["trend_cum"] = "", 0.0

    # 3) 盘中大变动: 自上次策略提醒后价格再变动 >= 1% → 再次提醒（不限次数）
    big_msg = None
    lp = notes.get("last_price")
    if lp and lp > 0 and abs(price - lp) / lp * 100 >= 1.0:
        pct = (price - lp) / lp * 100
        notes["last_price"] = price
        big_msg = (f"金价快速{'涨' if pct > 0 else '跌'}{abs(pct):.1f}%（{lp:g}→{price:g}）。"
                   "操作：先观察30分钟再动；跌看补仓档，涨看上限/止盈。")

    msgs = [m for m in (vol_msg, trend_msg, big_msg) if m]
    if msgs:
        return "；".join(msgs)
    return None

def compute_ranges(st):
    """根据近3日趋势与平均振幅, 自适应调整监测区间。
    规则: 趋势上行→上限/止盈上移5元, 补仓档上移6元; 趋势下行→下限下移5元, 补仓档下移6元;
    近3日平均振幅>=2.5%(高波动)→上下限外扩5元, 补仓档再下移3元(等更好价格)。
    返回 (有效区间dict, 区间调整通知或None)。区间变化时通知会推送一次。"""
    closes = st.get("closes", [])
    trend, s = "", 0.0
    if len(closes) >= 4:
        chg = [(closes[i]["c"] / closes[i-1]["c"] - 1) * 100
               for i in range(len(closes) - 3, len(closes))]
        s = sum(chg)
        if all(c > 0.2 for c in chg) and s >= 1.5:
            trend = "up"
        elif all(c < -0.2 for c in chg) and s <= -1.5:
            trend = "down"
    amps = [c.get("amp") for c in closes[-3:] if c.get("amp") is not None]
    avg_amp = sum(amps) / len(amps) if amps else 0.0
    high_vol = len(amps) >= 2 and avg_amp >= 2.5

    d_up = 5.0 if trend == "up" else 0.0
    d_down = 5.0 if trend == "down" else 0.0
    d_vol = 5.0 if high_vol else 0.0
    tier_shift = (-6.0 if trend == "down" else 6.0 if trend == "up" else 0.0) \
                 + (-3.0 if high_vol else 0.0)

    eff = {
        "high": HIGH_LIMIT + d_up + d_vol,
        "low_day": LOW_DAY - d_down - d_vol,
        "low_night": LOW_NIGHT - d_down - d_vol,
        "tiers": [round(t + tier_shift, 1) for t in BUY_TIERS],
        "tp": TP_LEVEL + d_up,
    }
    regime = (trend or "flat") + ("+highvol" if high_vol else "")

    notice = None
    if st.get("regime") and regime != st["regime"] and (trend or high_vol):
        # 仅在形成明确的新状态(趋势/高波动)时通知; 回归平静(基准区间)则静默重置
        reasons = []
        if trend == "up":
            reasons.append(f"近3个交易日累计上涨{s:+.1f}%，趋势转强")
        elif trend == "down":
            reasons.append(f"近3个交易日累计下跌{s:+.1f}%，趋势转弱")
        if high_vol:
            reasons.append(f"近3日平均振幅{avg_amp:.1f}%，波动放大")
        tiers_txt = "/".join(f"{t:g}" for t in eff["tiers"])
        notice = (f"监测区间已自适应调整（{'；'.join(reasons)}）。新区间: "
                  f"上限 {eff['high']:g}、日间下限 {eff['low_day']:g}、晚间下限 {eff['low_night']:g}、"
                  f"补仓档 {tiers_txt}、止盈参考 {eff['tp']:g}。")
    st["regime"] = regime
    return eff, notice

def main():
    test = "--test" in sys.argv
    now = datetime.datetime.now()
    is_day = DAY_START <= now.hour < DAY_END
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

    # ---- 波动/趋势自适应策略提示（出现新的大变动即再次提醒，小波动静默） ----
    advisory = build_advisory(price, meta, st)

    # ---- 区间自适应: 根据近3日趋势/振幅动态调整监测区间 ----
    eff, notice = compute_ranges(st)

    # ---- 深夜静默窗口(23:00~次日5:00): 状态照常记录, 但策略/区间类消息不发 ----
    in_quiet = (now.hour >= QUIET_START or now.hour < QUIET_END)
    if in_quiet:
        advisory = notice = None
    low = eff["low_day"] if is_day else eff["low_night"]

    # 区间档位变化时, 重置档位/止盈的"已提醒"状态(在新档位重新生效)
    if st.get("tiers_cfg") != eff["tiers"]:
        st["tiers"] = {}
        st["tiers_cfg"] = eff["tiers"]
    if st.get("tp_cfg") != eff["tp"]:
        st["tp"] = False
        st["tp_cfg"] = eff["tp"]

    tiers = st.get("tiers", {})
    tp_done = st.get("tp", False)
    changed = False

    # ---- 补仓档位: 跌破即提醒一次, 回升3元以上重置 ----
    extras = []
    for lvl in eff["tiers"]:
        key = str(int(lvl))
        if price <= lvl and not tiers.get(key):
            extras.append(f"补仓档位{lvl:g}已触及")
            tiers[key] = True
            changed = True
        elif price > lvl + RESET_GAP and tiers.get(key):
            tiers[key] = False
            changed = True

    # ---- 止盈参考位: 升破即提醒一次, 回落3元以上重置 ----
    if price >= eff["tp"] and not tp_done:
        extras.append(f"止盈参考位{eff['tp']:g}已升破")
        tp_done = True
        changed = True
    elif price < eff["tp"] - RESET_GAP and tp_done:
        tp_done = False
        changed = True

    st["tiers"], st["tp"] = tiers, tp_done
    save_state(st)

    # ---- 阈值判断(使用自适应后的区间) ----
    threshold_hit = price > eff["high"] or price < low
    trig = (f"已超过上限 {eff['high']:g} 元/克" if price > eff["high"]
            else (f"已跌破{period}下限 {low:g} 元/克" if price < low else None))

    # 附加内容: 策略提示 + 区间调整通知
    tail = ""
    if advisory:
        tail += "\n\n---\n\n**📋 策略提示:** " + advisory
    if notice:
        tail += "\n\n---\n\n**📊 区间调整:** " + notice

    if threshold_hit:
        title = f"金价提醒:{price:g}元/克 {trig}"
        desp = f"**当前金价:** {price:g} 元/克（{source}）\n\n**时间:** {now.strftime('%H:%M')}\n\n**触发:** {trig}"
        if extras:
            title += "(含档位)"
            desp += "\n\n**同时触及:** " + "；".join(extras)
        desp += tail
    elif extras:
        title = f"金价提醒:{extras[0]} 现价{price:g}"
        desp = (f"**当前金价:** {price:g} 元/克（{source}）\n\n**时间:** {now.strftime('%H:%M')}\n\n"
                + "\n\n".join(f"**{e}**" for e in extras)) + tail
    elif advisory or notice:
        if notice and not advisory:
            title = "金价区间调整提醒"
        elif advisory:
            if "振幅" in advisory:
                kind = "波动加剧"
            elif "趋势" in advisory:
                kind = "趋势转变"
            else:
                kind = "盘中大变动"
            title = f"金价策略提醒:{kind}"
        desp = (f"**当前金价:** {price:g} 元/克（{source}）\n\n**时间:** {now.strftime('%H:%M')}\n\n") + tail
    else:
        log(f"SILENT ({period}) price={price} source={source}")
        return

    ok = push_wechat(title, desp)
    log(f"ALERT ({period}) price={price} trig={trig} extras={extras} "
        f"range={eff['high']:g}/{low:g} push={'OK' if ok else 'FAILED'}")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("ERROR " + traceback.format_exc().replace("\n", " | "))
